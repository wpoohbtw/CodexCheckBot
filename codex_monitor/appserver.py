from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import LimitReading, WindowKind, utc_timestamp

logger = logging.getLogger(__name__)


class AppServerError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


class AuthorizationError(AppServerError):
    pass


@dataclass(frozen=True, slots=True)
class DeviceLogin:
    login_id: str
    verification_url: str
    user_code: str


class AppServerSession:
    def __init__(self, executable: str, codex_home: Path, timeout_seconds: int):
        self.executable = executable
        self.codex_home = codex_home
        self.timeout_seconds = timeout_seconds
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.codex_home.mkdir(parents=True, exist_ok=True)
        try:
            self.codex_home.chmod(0o700)
        except OSError:
            logger.warning("Не удалось ограничить права каталога CODEX_HOME")

        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.codex_home)
        env.pop("OPENAI_API_KEY", None)
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.executable,
                "app-server",
                "--stdio",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                **kwargs,
            )
        except OSError as exc:
            raise AppServerError("Не удалось запустить Codex App Server") from exc

        self._reader_task = asyncio.create_task(self._read_stdout(), name="codex-appserver-reader")
        self._stderr_task = asyncio.create_task(self._drain_stderr(), name="codex-appserver-stderr")
        result = await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-limits-monitor",
                    "title": "Codex Limits Monitor",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": False},
            },
        )
        returned_home = Path(result.get("codexHome", "")).resolve()
        if returned_home != self.codex_home.resolve():
            await self.close()
            raise AppServerError("App Server использует неожиданный CODEX_HOME")
        await self.notify("initialized", {})

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.process is None or self.process.stdin is None:
            raise AppServerError("Codex App Server не запущен")
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            payload["params"] = params
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write(payload)
        try:
            return await asyncio.wait_for(future, timeout=self.timeout_seconds)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise AppServerError(f"Таймаут операции App Server: {method}") from exc

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        await self._write(payload)

    async def start_device_login(self) -> DeviceLogin:
        result = await self.request("account/login/start", {"type": "chatgptDeviceCode"})
        if result.get("type") != "chatgptDeviceCode":
            raise AppServerError("App Server вернул неподдерживаемый тип авторизации")
        try:
            return DeviceLogin(
                login_id=result["loginId"],
                verification_url=result["verificationUrl"],
                user_code=result["userCode"],
            )
        except (KeyError, TypeError) as exc:
            raise AppServerError("Неполный ответ Device Code авторизации") from exc

    async def wait_login_completed(self, login_id: str, timeout_seconds: int) -> None:
        async def wait() -> None:
            while True:
                message = await self._notifications.get()
                if message.get("method") != "account/login/completed":
                    continue
                params = message.get("params") or {}
                received_id = params.get("loginId")
                if received_id is not None and received_id != login_id:
                    continue
                if params.get("success") is True:
                    return
                raise AuthorizationError("Авторизация не завершена или отклонена")

        try:
            await asyncio.wait_for(wait(), timeout=timeout_seconds)
        except TimeoutError as exc:
            await self.cancel_login(login_id)
            raise AuthorizationError("Срок действия кода авторизации истёк") from exc

    async def cancel_login(self, login_id: str) -> None:
        try:
            await self.request("account/login/cancel", {"loginId": login_id})
        except AppServerError:
            logger.info("Сессия авторизации уже завершена или недоступна")

    async def read_account(self, refresh_token: bool = False) -> dict[str, Any]:
        result = await self.request("account/read", {"refreshToken": refresh_token})
        account = result.get("account")
        if not isinstance(account, dict) or account.get("type") != "chatgpt":
            raise AuthorizationError("ChatGPT-авторизация отсутствует или недействительна")
        return account

    async def read_rate_limits(self) -> dict[WindowKind, LimitReading]:
        result = await self.request("account/rateLimits/read")
        return parse_rate_limits(result)

    async def logout(self) -> None:
        await self.request("account/logout")

    async def close(self) -> None:
        process = self.process
        self.process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        current = asyncio.current_task()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and task is not current:
                task.cancel()
        self._fail_pending(AppServerError("Codex App Server остановлен"))

    async def _write(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise AppServerError("Codex App Server не запущен")
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
        self.process.stdin.write(line.encode("utf-8"))
        try:
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AppServerError("Соединение с Codex App Server прервано") from exc

    async def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("App Server вернул некорректную строку JSON")
                    continue
                request_id = message.get("id")
                if request_id is not None and request_id in self._pending:
                    future = self._pending.pop(request_id)
                    error = message.get("error")
                    if error:
                        code = error.get("code") if isinstance(error, dict) else None
                        future.set_exception(AppServerError("Ошибка запроса Codex App Server", code=code))
                    else:
                        result = message.get("result")
                        future.set_result(result if isinstance(result, dict) else {})
                elif "method" in message:
                    await self._notifications.put(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой чтения вывода App Server")
        finally:
            self._fail_pending(AppServerError("Codex App Server неожиданно завершился"))

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            while await self.process.stderr.readline():
                logger.debug("Codex App Server записал диагностическую строку в stderr")
        except asyncio.CancelledError:
            raise

    def _fail_pending(self, error: AppServerError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()


def parse_rate_limits(payload: dict[str, Any], *, checked_at: int | None = None) -> dict[WindowKind, LimitReading]:
    """Extract only confidently classified Codex windows from a protocol response."""
    snapshot: dict[str, Any] | None = None
    by_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_id, dict) and isinstance(by_id.get("codex"), dict):
        snapshot = by_id["codex"]
    elif isinstance(payload.get("rateLimits"), dict):
        snapshot = payload["rateLimits"]
    elif isinstance(by_id, dict) and len(by_id) == 1:
        only = next(iter(by_id.values()))
        snapshot = only if isinstance(only, dict) else None
    if snapshot is None:
        return {}

    timestamp = checked_at if checked_at is not None else utc_timestamp()
    readings: dict[WindowKind, LimitReading] = {}
    ambiguous: set[WindowKind] = set()
    for field in ("primary", "secondary"):
        raw = snapshot.get(field)
        if not isinstance(raw, dict):
            continue
        duration = raw.get("windowDurationMins")
        used = raw.get("usedPercent")
        if not isinstance(duration, int) or not isinstance(used, int) or not 0 <= used <= 100:
            continue
        kind = classify_window(duration)
        if kind is None:
            continue
        if kind in readings:
            ambiguous.add(kind)
            continue
        reset = raw.get("resetsAt")
        readings[kind] = LimitReading(
            kind=kind,
            used_percent=used,
            window_duration_mins=duration,
            resets_at=reset if isinstance(reset, int) else None,
            checked_at=timestamp,
        )
    for kind in ambiguous:
        readings.pop(kind, None)
    return readings


def classify_window(duration_mins: int) -> WindowKind | None:
    if 240 <= duration_mins <= 360:
        return WindowKind.FIVE_HOUR
    if 9_000 <= duration_mins <= 11_000:
        return WindowKind.WEEKLY
    return None


async def read_account_and_limits(
    executable: str,
    codex_home: Path,
    timeout_seconds: int,
) -> tuple[dict[str, Any], dict[WindowKind, LimitReading]]:
    session = AppServerSession(executable, codex_home, timeout_seconds)
    try:
        await session.start()
        account = await session.read_account()
        limits = await session.read_rate_limits()
        return account, limits
    finally:
        await session.close()


async def logout_account(executable: str, codex_home: Path, timeout_seconds: int) -> None:
    session = AppServerSession(executable, codex_home, timeout_seconds)
    try:
        await session.start()
        await session.logout()
    finally:
        await session.close()
