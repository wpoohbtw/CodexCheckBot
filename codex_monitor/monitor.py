from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .appserver import AppServerError, AuthorizationError, read_account_and_limits
from .config import Settings
from .db import Database
from .detector import ResetDetector
from .models import Account, ResetEvent, ResetType, WindowKind

logger = logging.getLogger(__name__)


class MonitorService:
    def __init__(self, bot: Bot, db: Database, settings: Settings):
        self.bot = bot
        self.db = db
        self.settings = settings
        self.detector = ResetDetector()
        self._stop = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._semaphore = asyncio.Semaphore(settings.monitor_max_concurrency)
        self._account_locks: dict[str, asyncio.Lock] = {}

    def start(self) -> None:
        if self._runner is None:
            self._runner = asyncio.create_task(self._run(), name="limits-monitor")

    async def stop(self) -> None:
        self._stop.set()
        if self._runner:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)

    async def trigger(self, account: Account, *, force: bool = False) -> None:
        await self._check_guarded(account, force=force)

    async def _run(self) -> None:
        logger.info("Фоновый мониторинг запущен")
        try:
            while not self._stop.is_set():
                accounts = await self.db.list_accounts(active_only=True)
                await asyncio.gather(
                    *(self._check_guarded(account) for account in accounts),
                    return_exceptions=True,
                )
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.settings.check_interval_seconds
                    )
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        finally:
            logger.info("Фоновый мониторинг остановлен")

    async def _check_guarded(self, account: Account, *, force: bool = False) -> None:
        lock = self._account_locks.setdefault(account.id, asyncio.Lock())
        if lock.locked():
            return
        async with lock, self._semaphore:
            latest = await self.db.get_account(account.id)
            if latest is None or (not latest.monitoring_enabled and not force):
                return
            await self._check_with_retries(latest)

    async def _check_with_retries(self, account: Account) -> None:
        last_error: Exception | None = None
        for attempt in range(self.settings.monitor_retry_attempts):
            try:
                limits = await self._fetch(account)
                await self._process_limits(account, limits)
                recovered = await self.db.mark_check_success(account.id)
                if recovered:
                    await self._send_if_errors_enabled(
                        account, f"{account.display_name}: мониторинг лимитов восстановлен"
                    )
                return
            except (AppServerError, OSError) as exc:
                last_error = exc
                if attempt + 1 < self.settings.monitor_retry_attempts:
                    await asyncio.sleep(min(2**attempt, 8))

        safe_error = self._safe_error(last_error)
        if isinstance(last_error, AuthorizationError):
            await self.db.set_auth_status(account.id, "invalid")
        failures, notify = await self.db.mark_check_failure(
            account.id, safe_error, self.settings.error_notification_threshold
        )
        logger.error(
            "Проверка лимитов не удалась account=%s failures=%s error=%s",
            account.id,
            failures,
            safe_error,
        )
        if notify:
            await self._send_if_errors_enabled(
                account,
                f"{account.display_name}: мониторинг Codex временно недоступен. "
                "Проверьте авторизацию в карточке аккаунта.",
            )

    async def _fetch(self, account: Account):
        home = self.settings.accounts_storage_path / account.codex_home_key
        _, limits = await read_account_and_limits(
            self.settings.codex_executable,
            home,
            self.settings.app_server_timeout_seconds,
        )
        if not limits:
            raise AppServerError("Codex не вернул распознаваемые окна лимитов")
        logger.info(
            "Лимиты получены account=%s values=%s",
            account.id,
            {kind.value: value.used_percent for kind, value in limits.items()},
        )
        return limits

    async def _process_limits(self, account: Account, limits) -> None:
        candidates: list[WindowKind] = []
        for kind, reading in limits.items():
            state = await self.db.load_detector_state(account.id, kind)
            result = self.detector.evaluate(state, reading)
            inserted = await self.db.save_detection(account.id, result.state, result.event)
            if result.candidate_started:
                candidates.append(kind)
                logger.warning("Обнаружен кандидат на сброс account=%s window=%s", account.id, kind.value)
            if result.event and inserted:
                await self._notify_reset(account, result.event)

        if candidates:
            await asyncio.sleep(self.settings.reset_confirm_delay_seconds)
            latest = await self.db.get_account(account.id)
            if latest is None or not latest.monitoring_enabled:
                return
            confirmed = await self._fetch(account)
            for kind in candidates:
                reading = confirmed.get(kind)
                if reading is None:
                    continue
                state = await self.db.load_detector_state(account.id, kind)
                result = self.detector.evaluate(state, reading)
                inserted = await self.db.save_detection(account.id, result.state, result.event)
                if result.event and inserted:
                    logger.warning(
                        "Сброс подтверждён account=%s window=%s type=%s",
                        account.id,
                        kind.value,
                        result.event.reset_type.value,
                    )
                    await self._notify_reset(account, result.event)
        for event in await self.db.list_unnotified_events(account.id):
            await self._notify_reset(account, event)

    async def _notify_reset(self, account: Account, event: ResetEvent) -> None:
        settings = await self.db.get_notification_settings(account.id)
        kind_enabled = (
            settings.reset_five_hour
            if event.kind is WindowKind.FIVE_HOUR
            else settings.reset_weekly
        )
        type_enabled = (
            settings.early_resets
            if event.reset_type is ResetType.EARLY
            else settings.planned_resets
        )
        if not kind_enabled or not type_enabled:
            await self.db.mark_event_notified(account.id, event.fingerprint)
            return
        suffix = "5ч лимита" if event.kind is WindowKind.FIVE_HOUR else "недельного лимита"
        try:
            await self.bot.send_message(account.owner_id, f"{account.display_name} произошел сброс - {suffix}")
        except TelegramAPIError:
            logger.exception("Не удалось отправить уведомление account=%s", account.id)
            return
        await self.db.mark_event_notified(account.id, event.fingerprint)
        logger.info("Уведомление о сбросе отправлено account=%s window=%s", account.id, event.kind.value)

    async def _send_if_errors_enabled(self, account: Account, text: str) -> None:
        settings = await self.db.get_notification_settings(account.id)
        if not settings.errors:
            return
        try:
            await self.bot.send_message(account.owner_id, text)
        except TelegramAPIError:
            logger.exception("Не удалось отправить уведомление об ошибке account=%s", account.id)

    @staticmethod
    def _safe_error(error: Exception | None) -> str:
        if isinstance(error, AuthorizationError):
            return "ChatGPT-авторизация недействительна"
        if isinstance(error, AppServerError):
            return str(error)
        return "Неизвестная ошибка мониторинга"
