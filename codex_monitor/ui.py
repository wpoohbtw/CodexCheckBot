from __future__ import annotations

import asyncio
import html
import logging
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .appserver import (
    AppServerError,
    AppServerSession,
    AuthorizationError,
    DeviceLogin,
    logout_account,
    read_account_and_limits,
)
from .config import Settings
from .db import Database
from .models import Account, DetectorState, NotificationSettings, WindowKind
from .monitor import MonitorService

logger = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")
MIN_NAME_LENGTH = 1
MAX_NAME_LENGTH = 40


class InputState(StatesGroup):
    add_name = State()
    rename_account = State()


@dataclass(slots=True)
class AccountDraft:
    owner_id: int
    home_key: str
    name: str | None = None
    status: str = "new"
    session: AppServerSession | None = None
    login: DeviceLogin | None = None
    wait_task: asyncio.Task[None] | None = None
    error: str | None = None


class DraftStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.items: dict[int, AccountDraft] = {}

    def get_or_create(self, owner_id: int) -> AccountDraft:
        draft = self.items.get(owner_id)
        if draft is None:
            draft = AccountDraft(owner_id=owner_id, home_key=uuid.uuid4().hex)
            self.items[owner_id] = draft
        return draft

    async def replace_for_login(self, owner_id: int) -> AccountDraft:
        old = self.items.get(owner_id)
        name = old.name if old else None
        await self.cancel(owner_id)
        draft = AccountDraft(owner_id=owner_id, home_key=uuid.uuid4().hex, name=name)
        self.items[owner_id] = draft
        return draft

    def take_saved(self, owner_id: int) -> AccountDraft | None:
        return self.items.pop(owner_id, None)

    async def cancel(self, owner_id: int) -> None:
        draft = self.items.pop(owner_id, None)
        if draft is None:
            return
        current = asyncio.current_task()
        if draft.wait_task and draft.wait_task is not current:
            draft.wait_task.cancel()
        if draft.session:
            if draft.login:
                await draft.session.cancel_login(draft.login.login_id)
            await draft.session.close()
        remove_home(self.settings.accounts_storage_path, draft.home_key)

    async def close(self) -> None:
        for owner_id in list(self.items):
            await self.cancel(owner_id)


class MainView:
    def __init__(self, bot: Bot, db: Database):
        self.bot = bot
        self.db = db
        self._locks: dict[int, asyncio.Lock] = {}

    def lock(self, owner_id: int) -> asyncio.Lock:
        return self._locks.setdefault(owner_id, asyncio.Lock())

    async def create(self, owner_id: int, chat_id: int, text: str, markup: InlineKeyboardMarkup) -> None:
        message = await self.bot.send_message(chat_id, text, reply_markup=markup)
        await self.db.set_main_message(owner_id, chat_id, message.message_id)

    async def edit(self, owner_id: int, text: str, markup: InlineKeyboardMarkup) -> None:
        active = await self.db.get_main_message(owner_id)
        if active is None:
            await self.create(owner_id, owner_id, text, markup)
            return
        chat_id, message_id = active
        try:
            await self.bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup
            )
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.warning("Главное сообщение недоступно, создаётся новое user=%s", owner_id)
            await self.create(owner_id, chat_id, text, markup)
        except TelegramForbiddenError:
            logger.warning("Бот не может восстановить главное сообщение user=%s", owner_id)

    async def is_current(self, callback: CallbackQuery) -> bool:
        if callback.message is None:
            return False
        active = await self.db.get_main_message(callback.from_user.id)
        if active is None:
            return False
        return (callback.message.chat.id, callback.message.message_id) == active


class TelegramUI:
    def __init__(
        self,
        bot: Bot,
        db: Database,
        settings: Settings,
        monitor: MonitorService,
    ):
        self.bot = bot
        self.db = db
        self.settings = settings
        self.monitor = monitor
        self.view = MainView(bot, db)
        self.drafts = DraftStore(settings)
        self.router = Router(name="telegram-ui")
        self._register_handlers()

    async def close(self) -> None:
        await self.drafts.close()

    def _register_handlers(self) -> None:
        self.router.message.register(self.start, CommandStart())
        self.router.message.register(self.receive_add_name, InputState.add_name)
        self.router.message.register(self.receive_rename, InputState.rename_account)
        self.router.callback_query.register(self.open_main, F.data == "main")
        self.router.callback_query.register(self.open_add, F.data == "main:add")
        self.router.callback_query.register(self.begin_auth, F.data == "add:auth")
        self.router.callback_query.register(self.ask_add_name, F.data == "add:name")
        self.router.callback_query.register(self.save_account, F.data == "add:save")
        self.router.callback_query.register(self.cancel_add, F.data == "add:cancel")
        self.router.callback_query.register(self.list_accounts, F.data == "main:accounts")
        self.router.callback_query.register(self.account_card, F.data.startswith("acct:view:"))
        self.router.callback_query.register(self.refresh_account, F.data.startswith("acct:refresh:"))
        self.router.callback_query.register(self.toggle_monitoring, F.data.startswith("acct:monitor:"))
        self.router.callback_query.register(self.ask_rename, F.data.startswith("acct:rename:"))
        self.router.callback_query.register(self.confirm_delete, F.data.startswith("acct:deleteq:"))
        self.router.callback_query.register(self.delete_account, F.data.startswith("acct:delete:"))
        self.router.callback_query.register(self.notification_accounts, F.data == "main:notifs")
        self.router.callback_query.register(self.notification_settings, F.data.startswith("notif:view:"))
        self.router.callback_query.register(self.toggle_notification, F.data.startswith("notif:toggle:"))
        self.router.callback_query.register(self.stale_callback)

    async def start(self, message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        owner_id = message.from_user.id
        await state.clear()
        await self.drafts.cancel(owner_id)
        await self.db.upsert_user(owner_id, message.chat.id)
        await self.view.create(owner_id, message.chat.id, *main_screen())
        logger.info("Пользователь открыл интерфейс user=%s", owner_id)

    async def _accept(self, callback: CallbackQuery) -> bool:
        if not await self.view.is_current(callback):
            await callback.answer("Это меню устарело. Используйте /start.", show_alert=True)
            return False
        await callback.answer()
        return True

    async def open_main(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._accept(callback):
            return
        async with self.view.lock(callback.from_user.id):
            await state.clear()
            await self.view.edit(callback.from_user.id, *main_screen())

    async def open_add(self, callback: CallbackQuery) -> None:
        if not await self._accept(callback):
            return
        async with self.view.lock(callback.from_user.id):
            draft = self.drafts.get_or_create(callback.from_user.id)
            await self._render_add(draft)

    async def begin_auth(self, callback: CallbackQuery) -> None:
        if not await self._accept(callback):
            return
        owner_id = callback.from_user.id
        async with self.view.lock(owner_id):
            draft = await self.drafts.replace_for_login(owner_id)
            draft.status = "starting"
            await self._render_add(draft)
            session = AppServerSession(
                self.settings.codex_executable,
                self.settings.accounts_storage_path / draft.home_key,
                self.settings.app_server_timeout_seconds,
            )
            draft.session = session
            try:
                await session.start()
                draft.login = await session.start_device_login()
            except AppServerError as exc:
                draft.status = "error"
                draft.error = str(exc)
                await session.close()
                draft.session = None
                await self._render_add(draft)
                logger.error("Ошибка запуска авторизации user=%s", owner_id)
                return
            draft.status = "pending"
            await self._render_add(draft)
            draft.wait_task = asyncio.create_task(
                self._wait_for_auth(draft), name=f"auth-{owner_id}"
            )
            logger.info("Авторизация начата user=%s", owner_id)

    async def _wait_for_auth(self, draft: AccountDraft) -> None:
        assert draft.session is not None and draft.login is not None
        try:
            await draft.session.wait_login_completed(
                draft.login.login_id, self.settings.auth_timeout_seconds
            )
            await draft.session.read_account()
            await draft.session.read_rate_limits()
            if self.drafts.items.get(draft.owner_id) is not draft:
                return
            draft.status = "authorized"
            draft.error = None
            logger.info("Авторизация завершена user=%s", draft.owner_id)
        except asyncio.CancelledError:
            raise
        except (AppServerError, AuthorizationError) as exc:
            if self.drafts.items.get(draft.owner_id) is draft:
                draft.status = "error"
                draft.error = str(exc)
            logger.error("Авторизация не завершена user=%s", draft.owner_id)
        finally:
            await draft.session.close()
            draft.session = None
            if self.drafts.items.get(draft.owner_id) is draft:
                async with self.view.lock(draft.owner_id):
                    await self._render_add(draft)

    async def ask_add_name(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._accept(callback):
            return
        prompt = await self.bot.send_message(
            callback.from_user.id,
            f"Введите название аккаунта (до {MAX_NAME_LENGTH} символов):",
        )
        await state.set_state(InputState.add_name)
        await state.update_data(prompt_id=prompt.message_id)

    async def receive_add_name(self, message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        name = normalize_name(message.text)
        data = await state.get_data()
        await delete_temporary(self.bot, message.chat.id, data.get("prompt_id"), message.message_id)
        draft = self.drafts.get_or_create(message.from_user.id)
        if name is None:
            prompt = await self.bot.send_message(
                message.chat.id,
                f"Название должно содержать от {MIN_NAME_LENGTH} до {MAX_NAME_LENGTH} символов.",
            )
            await state.update_data(prompt_id=prompt.message_id)
            return
        draft.name = name
        await state.clear()
        async with self.view.lock(message.from_user.id):
            await self._render_add(draft)

    async def save_account(self, callback: CallbackQuery) -> None:
        if not await self._accept(callback):
            return
        owner_id = callback.from_user.id
        async with self.view.lock(owner_id):
            draft = self.drafts.items.get(owner_id)
            if draft is None or draft.status != "authorized" or not draft.name:
                if draft:
                    draft.error = "Сначала завершите авторизацию и укажите имя."
                    await self._render_add(draft)
                return
            draft.status = "checking"
            await self._render_add(draft)
            home = self.settings.accounts_storage_path / draft.home_key
            try:
                _, limits = await read_account_and_limits(
                    self.settings.codex_executable,
                    home,
                    self.settings.app_server_timeout_seconds,
                )
                if not limits:
                    raise AppServerError("Codex не вернул распознаваемые лимиты")
            except AppServerError as exc:
                draft.status = "error"
                draft.error = f"Проверка доступа не пройдена: {exc}"
                await self._render_add(draft)
                return
            account = Account(
                id=uuid.uuid4().hex,
                owner_id=owner_id,
                display_name=draft.name,
                codex_home_key=draft.home_key,
                auth_status="authorized",
                monitoring_enabled=True,
                last_success_at=None,
                last_error=None,
                consecutive_failures=0,
                failure_notified=False,
            )
            await self.db.create_account(account)
            self.drafts.take_saved(owner_id)
            await self._render_accounts(owner_id)
            asyncio.create_task(self.monitor.trigger(account), name=f"initial-check-{account.id}")
            logger.info("Аккаунт создан account=%s user=%s", account.id, owner_id)

    async def cancel_add(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._accept(callback):
            return
        owner_id = callback.from_user.id
        async with self.view.lock(owner_id):
            await state.clear()
            await self.drafts.cancel(owner_id)
            await self.view.edit(owner_id, *main_screen())

    async def list_accounts(self, callback: CallbackQuery) -> None:
        if not await self._accept(callback):
            return
        async with self.view.lock(callback.from_user.id):
            await self._render_accounts(callback.from_user.id)

    async def account_card(self, callback: CallbackQuery) -> None:
        if not await self._accept(callback):
            return
        account_id = callback.data.rsplit(":", 1)[-1]
        async with self.view.lock(callback.from_user.id):
            await self._render_account(callback.from_user.id, account_id)

    async def refresh_account(self, callback: CallbackQuery) -> None:
        if not await self._accept(callback):
            return
        owner_id = callback.from_user.id
        account_id = callback.data.rsplit(":", 1)[-1]
        account = await self.db.get_account(account_id, owner_id)
        if account is None:
            await self._render_accounts(owner_id)
            return
        await self.monitor.trigger(account, force=True)
        async with self.view.lock(owner_id):
            await self._render_account(owner_id, account_id)

    async def toggle_monitoring(self, callback: CallbackQuery) -> None:
        if not await self._accept(callback):
            return
        owner_id = callback.from_user.id
        account_id = callback.data.rsplit(":", 1)[-1]
        account = await self.db.get_account(account_id, owner_id)
        if account is None:
            return
        await self.db.set_monitoring(account_id, owner_id, not account.monitoring_enabled)
        async with self.view.lock(owner_id):
            await self._render_account(owner_id, account_id)

    async def ask_rename(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._accept(callback):
            return
        owner_id = callback.from_user.id
        account_id = callback.data.rsplit(":", 1)[-1]
        if await self.db.get_account(account_id, owner_id) is None:
            return
        prompt = await self.bot.send_message(
            owner_id, f"Введите новое название аккаунта (до {MAX_NAME_LENGTH} символов):"
        )
        await state.set_state(InputState.rename_account)
        await state.update_data(prompt_id=prompt.message_id, account_id=account_id)

    async def receive_rename(self, message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        data = await state.get_data()
        await delete_temporary(self.bot, message.chat.id, data.get("prompt_id"), message.message_id)
        name = normalize_name(message.text)
        if name is None:
            prompt = await self.bot.send_message(
                message.chat.id,
                f"Название должно содержать от {MIN_NAME_LENGTH} до {MAX_NAME_LENGTH} символов.",
            )
            await state.update_data(prompt_id=prompt.message_id)
            return
        account_id = data.get("account_id")
        if isinstance(account_id, str):
            await self.db.rename_account(account_id, message.from_user.id, name)
        await state.clear()
        async with self.view.lock(message.from_user.id):
            await self._render_account(message.from_user.id, account_id)

    async def confirm_delete(self, callback: CallbackQuery) -> None:
        if not await self._accept(callback):
            return
        owner_id = callback.from_user.id
        account_id = callback.data.rsplit(":", 1)[-1]
        account = await self.db.get_account(account_id, owner_id)
        if account is None:
            return
        builder = InlineKeyboardBuilder()
        builder.button(text="Удалить безвозвратно", callback_data=f"acct:delete:{account.id}")
        builder.button(text="← Назад", callback_data=f"acct:view:{account.id}")
        builder.adjust(1)
        async with self.view.lock(owner_id):
            await self.view.edit(
                owner_id,
                f"Удалить аккаунт <b>{html.escape(account.display_name)}</b>?\n\n"
                "Мониторинг будет остановлен, а сохранённая авторизация удалена с сервера.",
                builder.as_markup(),
            )

    async def delete_account(self, callback: CallbackQuery) -> None:
        if not await self._accept(callback):
            return
        owner_id = callback.from_user.id
        account_id = callback.data.rsplit(":", 1)[-1]
        account = await self.db.get_account(account_id, owner_id)
        if account is None:
            return
        await self.db.set_monitoring(account.id, owner_id, False)
        home = self.settings.accounts_storage_path / account.codex_home_key
        try:
            await logout_account(
                self.settings.codex_executable,
                home,
                self.settings.app_server_timeout_seconds,
            )
        except AppServerError:
            logger.warning("Logout не выполнен перед удалением account=%s", account.id)
        await self.db.delete_account(account.id, owner_id)
        remove_home(self.settings.accounts_storage_path, account.codex_home_key)
        logger.info("Аккаунт удалён account=%s user=%s", account.id, owner_id)
        async with self.view.lock(owner_id):
            await self._render_accounts(owner_id)

    async def notification_accounts(self, callback: CallbackQuery) -> None:
        if not await self._accept(callback):
            return
        async with self.view.lock(callback.from_user.id):
            await self._render_notification_accounts(callback.from_user.id)

    async def notification_settings(self, callback: CallbackQuery) -> None:
        if not await self._accept(callback):
            return
        account_id = callback.data.rsplit(":", 1)[-1]
        async with self.view.lock(callback.from_user.id):
            await self._render_notification_settings(callback.from_user.id, account_id)

    async def toggle_notification(self, callback: CallbackQuery) -> None:
        if not await self._accept(callback):
            return
        parts = callback.data.split(":")
        if len(parts) != 4:
            return
        _, _, account_id, field = parts
        await self.db.toggle_notification(account_id, callback.from_user.id, field)
        async with self.view.lock(callback.from_user.id):
            await self._render_notification_settings(callback.from_user.id, account_id)

    async def stale_callback(self, callback: CallbackQuery) -> None:
        await callback.answer("Действие больше недоступно. Используйте /start.", show_alert=True)

    async def _render_add(self, draft: AccountDraft) -> None:
        status = "✅" if draft.status == "authorized" else "❌"
        status_text = {
            "starting": "\n\nЗапускаю безопасную сессию авторизации…",
            "pending": "\n\nЗавершите авторизацию по ссылке ниже.",
            "checking": "\n\nПроверяю доступ к аккаунту…",
            "error": f"\n\nОшибка: {html.escape(draft.error or 'неизвестная ошибка')}",
        }.get(draft.status, "")
        text = (
            "<b>Авторизация и настройка аккаунта Codex:</b>\n\n"
            "Подключая аккаунт, вы разрешаете Codex на этом сервере доступ к нему. "
            "Этот доступ технически не ограничен только чтением лимитов. "
            "Бот не просит пароль, cookie или токены."
            f"{status_text}"
        )
        if draft.status == "pending" and draft.login:
            text += (
                f"\n\nКод: <code>{html.escape(draft.login.user_code)}</code>\n"
                "1. Откройте официальный адрес OpenAI.\n"
                "2. Войдите в ChatGPT и введите код."
            )
        builder = InlineKeyboardBuilder()
        builder.button(text=f"Авторизовать аккаунт: {status}", callback_data="add:auth")
        builder.button(text=f"Имя: {draft.name or '—'}", callback_data="add:name")
        if draft.status == "pending" and draft.login:
            builder.row(
                InlineKeyboardButton(
                    text="Открыть авторизацию", url=draft.login.verification_url
                )
            )
        builder.button(text="Сохранить", callback_data="add:save")
        builder.button(text="← Назад", callback_data="add:cancel")
        builder.adjust(1)
        await self.view.edit(draft.owner_id, text, builder.as_markup())

    async def _render_accounts(self, owner_id: int) -> None:
        accounts = await self.db.list_accounts(owner_id=owner_id)
        text = "<b>Добавленные аккаунты Codex:</b>"
        if not accounts:
            text += "\n\nАккаунтов пока нет."
        builder = InlineKeyboardBuilder()
        for account in accounts:
            icon = "✅" if account.monitoring_enabled else "⏸"
            builder.button(
                text=f"{icon} {account.display_name}", callback_data=f"acct:view:{account.id}"
            )
        builder.button(text="← Назад", callback_data="main")
        builder.adjust(1)
        await self.view.edit(owner_id, text, builder.as_markup())

    async def _render_account(self, owner_id: int, account_id: str | None) -> None:
        if not account_id:
            await self._render_accounts(owner_id)
            return
        account = await self.db.get_account(account_id, owner_id)
        if account is None:
            await self._render_accounts(owner_id)
            return
        states = await self.db.get_states(account.id)
        five = format_state(states.get(WindowKind.FIVE_HOUR))
        weekly = format_state(states.get(WindowKind.WEEKLY))
        text = (
            f"<b>{html.escape(account.display_name)}</b>\n\n"
            f"Авторизация: {'✅' if account.auth_status == 'authorized' else '❌'}\n"
            f"Мониторинг: {'✅ активен' if account.monitoring_enabled else '⏸ приостановлен'}\n\n"
            f"<b>5-часовой лимит</b>\n{five}\n\n"
            f"<b>Недельный лимит</b>\n{weekly}\n\n"
            f"Последняя успешная проверка: {format_time(account.last_success_at)}"
        )
        if account.last_error:
            text += f"\nПоследняя ошибка: {html.escape(account.last_error)}"
        builder = InlineKeyboardBuilder()
        builder.button(text="Обновить данные", callback_data=f"acct:refresh:{account.id}")
        builder.button(
            text="Приостановить мониторинг" if account.monitoring_enabled else "Возобновить мониторинг",
            callback_data=f"acct:monitor:{account.id}",
        )
        builder.button(text="Переименовать", callback_data=f"acct:rename:{account.id}")
        builder.button(text="Настройка уведомлений", callback_data=f"notif:view:{account.id}")
        builder.button(text="Удалить аккаунт", callback_data=f"acct:deleteq:{account.id}")
        builder.button(text="← Назад", callback_data="main:accounts")
        builder.adjust(1)
        await self.view.edit(owner_id, text, builder.as_markup())

    async def _render_notification_accounts(self, owner_id: int) -> None:
        accounts = await self.db.list_accounts(owner_id=owner_id)
        text = "<b>Выберите аккаунт для настройки уведомлений:</b>"
        if not accounts:
            text += "\n\nАккаунтов пока нет."
        builder = InlineKeyboardBuilder()
        for account in accounts:
            builder.button(text=account.display_name, callback_data=f"notif:view:{account.id}")
        builder.button(text="← Назад", callback_data="main")
        builder.adjust(1)
        await self.view.edit(owner_id, text, builder.as_markup())

    async def _render_notification_settings(self, owner_id: int, account_id: str) -> None:
        account = await self.db.get_account(account_id, owner_id)
        if account is None:
            await self._render_notification_accounts(owner_id)
            return
        settings = await self.db.get_notification_settings(account.id)
        text = f"<b>Настройка уведомлений — {html.escape(account.display_name)}</b>"
        fields = [
            ("reset_five_hour", settings.reset_five_hour, "Сброс 5-часового лимита"),
            ("reset_weekly", settings.reset_weekly, "Сброс недельного лимита"),
            ("early_resets", settings.early_resets, "Досрочные сбросы"),
            ("planned_resets", settings.planned_resets, "Плановые сбросы"),
            ("errors", settings.errors, "Ошибки мониторинга и авторизации"),
        ]
        builder = InlineKeyboardBuilder()
        for field, enabled, label in fields:
            icon = "✅" if enabled else "❌"
            builder.button(
                text=f"{icon} {label}", callback_data=f"notif:toggle:{account.id}:{field}"
            )
        builder.button(text="← К аккаунту", callback_data=f"acct:view:{account.id}")
        builder.adjust(1)
        await self.view.edit(owner_id, text, builder.as_markup())


def main_screen() -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить аккаунт", callback_data="main:add")
    builder.button(text="Добавленные аккаунты", callback_data="main:accounts")
    builder.button(text="Настройка уведомлений", callback_data="main:notifs")
    builder.adjust(1)
    return "<b>Это бот-чекер лимитов Codex. Меню действий:</b>", builder.as_markup()


def normalize_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    if not MIN_NAME_LENGTH <= len(normalized) <= MAX_NAME_LENGTH:
        return None
    return normalized


def format_time(timestamp: int | None) -> str:
    if timestamp is None:
        return "нет данных"
    return datetime.fromtimestamp(timestamp, MOSCOW).strftime("%d.%m.%Y %H:%M МСК")


def format_state(state: DetectorState | None) -> str:
    if state is None:
        return "Данных пока нет."
    reading = state.pending_reading or state.reading
    return (
        f"Использовано: {reading.used_percent}%\n"
        f"Осталось: {reading.remaining_percent}%\n"
        f"Следующий сброс: {format_time(reading.resets_at)}"
    )


async def delete_temporary(bot: Bot, chat_id: int, *message_ids: int | None) -> None:
    for message_id in message_ids:
        if message_id is None:
            continue
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramBadRequest:
            logger.debug("Временное сообщение уже недоступно для удаления")


def remove_home(storage_root: Path, home_key: str) -> None:
    root = storage_root.resolve()
    target = (root / home_key).resolve()
    if target.parent != root or not home_key:
        raise ValueError("Небезопасный путь CODEX_HOME")
    if target.exists():
        shutil.rmtree(target)
