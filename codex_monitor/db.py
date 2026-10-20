from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from .models import (
    Account,
    DetectorState,
    LimitReading,
    NotificationSettings,
    ResetEvent,
    ResetType,
    WindowKind,
    account_from_row,
    utc_timestamp,
)


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    active_chat_id INTEGER NOT NULL,
    main_message_id INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    codex_home_key TEXT NOT NULL UNIQUE,
    auth_status TEXT NOT NULL DEFAULT 'authorized',
    monitoring_enabled INTEGER NOT NULL DEFAULT 1,
    last_success_at INTEGER,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    failure_notified INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_accounts_owner ON accounts(owner_id);
CREATE INDEX IF NOT EXISTS idx_accounts_active ON accounts(monitoring_enabled, auth_status);

CREATE TABLE IF NOT EXISTS notification_settings (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    reset_five_hour INTEGER NOT NULL DEFAULT 1,
    reset_weekly INTEGER NOT NULL DEFAULT 1,
    early_resets INTEGER NOT NULL DEFAULT 1,
    planned_resets INTEGER NOT NULL DEFAULT 1,
    errors INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS limit_states (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    window_kind TEXT NOT NULL,
    used_percent INTEGER NOT NULL,
    window_duration_mins INTEGER NOT NULL,
    resets_at INTEGER,
    checked_at INTEGER NOT NULL,
    pending_used_percent INTEGER,
    pending_window_duration_mins INTEGER,
    pending_resets_at INTEGER,
    pending_checked_at INTEGER,
    PRIMARY KEY (account_id, window_kind)
);

CREATE TABLE IF NOT EXISTS reset_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    window_kind TEXT NOT NULL,
    reset_type TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    previous_used_percent INTEGER NOT NULL,
    current_used_percent INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    notified_at INTEGER,
    UNIQUE(account_id, window_kind, fingerprint)
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._write_lock = asyncio.Lock()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self.path)
        try:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("PRAGMA busy_timeout = 5000")
            yield db
        finally:
            await db.close()

    async def initialize(self) -> None:
        async with self.connect() as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def upsert_user(self, telegram_id: int, chat_id: int) -> None:
        now = utc_timestamp()
        async with self._write_lock, self.connect() as db:
            await db.execute(
                """INSERT INTO users(telegram_id, active_chat_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                     active_chat_id=excluded.active_chat_id, updated_at=excluded.updated_at""",
                (telegram_id, chat_id, now, now),
            )
            await db.commit()

    async def set_main_message(self, telegram_id: int, chat_id: int, message_id: int) -> None:
        await self.upsert_user(telegram_id, chat_id)
        async with self._write_lock, self.connect() as db:
            await db.execute(
                "UPDATE users SET active_chat_id=?, main_message_id=?, updated_at=? WHERE telegram_id=?",
                (chat_id, message_id, utc_timestamp(), telegram_id),
            )
            await db.commit()

    async def get_main_message(self, telegram_id: int) -> tuple[int, int] | None:
        async with self.connect() as db:
            row = await (await db.execute(
                "SELECT active_chat_id, main_message_id FROM users WHERE telegram_id=?",
                (telegram_id,),
            )).fetchone()
        if row is None or row["main_message_id"] is None:
            return None
        return row["active_chat_id"], row["main_message_id"]

    async def create_account(self, account: Account) -> None:
        now = utc_timestamp()
        async with self._write_lock, self.connect() as db:
            await db.execute(
                """INSERT INTO accounts(
                     id, owner_id, display_name, codex_home_key, auth_status,
                     monitoring_enabled, last_success_at, last_error,
                     consecutive_failures, failure_notified, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    account.id, account.owner_id, account.display_name,
                    account.codex_home_key, account.auth_status,
                    int(account.monitoring_enabled), account.last_success_at,
                    account.last_error, account.consecutive_failures,
                    int(account.failure_notified), now, now,
                ),
            )
            await db.execute(
                "INSERT INTO notification_settings(account_id) VALUES (?)", (account.id,)
            )
            await db.commit()

    async def list_accounts(self, owner_id: int | None = None, active_only: bool = False) -> list[Account]:
        clauses: list[str] = []
        params: list[object] = []
        if owner_id is not None:
            clauses.append("owner_id=?")
            params.append(owner_id)
        if active_only:
            clauses.extend(["monitoring_enabled=1", "auth_status='authorized'"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        async with self.connect() as db:
            rows = await (await db.execute(
                "SELECT * FROM accounts" + where + " ORDER BY created_at", params
            )).fetchall()
        return [account_from_row(row) for row in rows]

    async def get_account(self, account_id: str, owner_id: int | None = None) -> Account | None:
        sql = "SELECT * FROM accounts WHERE id=?"
        params: list[object] = [account_id]
        if owner_id is not None:
            sql += " AND owner_id=?"
            params.append(owner_id)
        async with self.connect() as db:
            row = await (await db.execute(sql, params)).fetchone()
        return account_from_row(row) if row else None

    async def rename_account(self, account_id: str, owner_id: int, name: str) -> bool:
        async with self._write_lock, self.connect() as db:
            cursor = await db.execute(
                "UPDATE accounts SET display_name=?, updated_at=? WHERE id=? AND owner_id=?",
                (name, utc_timestamp(), account_id, owner_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def set_monitoring(self, account_id: str, owner_id: int, enabled: bool) -> bool:
        async with self._write_lock, self.connect() as db:
            cursor = await db.execute(
                "UPDATE accounts SET monitoring_enabled=?, updated_at=? WHERE id=? AND owner_id=?",
                (int(enabled), utc_timestamp(), account_id, owner_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def set_auth_status(self, account_id: str, status: str) -> None:
        async with self._write_lock, self.connect() as db:
            await db.execute(
                "UPDATE accounts SET auth_status=?, updated_at=? WHERE id=?",
                (status, utc_timestamp(), account_id),
            )
            await db.commit()

    async def delete_account(self, account_id: str, owner_id: int) -> Account | None:
        account = await self.get_account(account_id, owner_id)
        if account is None:
            return None
        async with self._write_lock, self.connect() as db:
            await db.execute("DELETE FROM accounts WHERE id=? AND owner_id=?", (account_id, owner_id))
            await db.commit()
        return account

    async def get_notification_settings(self, account_id: str) -> NotificationSettings:
        async with self.connect() as db:
            row = await (await db.execute(
                "SELECT * FROM notification_settings WHERE account_id=?", (account_id,)
            )).fetchone()
        if row is None:
            return NotificationSettings()
        return NotificationSettings(
            reset_five_hour=bool(row["reset_five_hour"]),
            reset_weekly=bool(row["reset_weekly"]),
            early_resets=bool(row["early_resets"]),
            planned_resets=bool(row["planned_resets"]),
            errors=bool(row["errors"]),
        )

    async def toggle_notification(self, account_id: str, owner_id: int, field: str) -> bool:
        allowed = {"reset_five_hour", "reset_weekly", "early_resets", "planned_resets", "errors"}
        if field not in allowed:
            return False
        async with self._write_lock, self.connect() as db:
            cursor = await db.execute(
                f"""UPDATE notification_settings
                    SET {field}=CASE {field} WHEN 1 THEN 0 ELSE 1 END
                    WHERE account_id IN (SELECT id FROM accounts WHERE id=? AND owner_id=?)""",
                (account_id, owner_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def load_detector_state(self, account_id: str, kind: WindowKind) -> DetectorState | None:
        async with self.connect() as db:
            row = await (await db.execute(
                "SELECT * FROM limit_states WHERE account_id=? AND window_kind=?",
                (account_id, kind.value),
            )).fetchone()
        if row is None:
            return None
        reading = LimitReading(
            kind=kind,
            used_percent=row["used_percent"],
            window_duration_mins=row["window_duration_mins"],
            resets_at=row["resets_at"],
            checked_at=row["checked_at"],
        )
        pending = None
        if row["pending_used_percent"] is not None:
            pending = LimitReading(
                kind=kind,
                used_percent=row["pending_used_percent"],
                window_duration_mins=row["pending_window_duration_mins"],
                resets_at=row["pending_resets_at"],
                checked_at=row["pending_checked_at"],
            )
        return DetectorState(reading=reading, pending_reading=pending)

    async def save_detection(
        self,
        account_id: str,
        state: DetectorState,
        event: ResetEvent | None,
    ) -> bool:
        pending = state.pending_reading
        async with self._write_lock, self.connect() as db:
            await db.execute(
                """INSERT INTO limit_states(
                     account_id, window_kind, used_percent, window_duration_mins,
                     resets_at, checked_at, pending_used_percent,
                     pending_window_duration_mins, pending_resets_at, pending_checked_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(account_id, window_kind) DO UPDATE SET
                     used_percent=excluded.used_percent,
                     window_duration_mins=excluded.window_duration_mins,
                     resets_at=excluded.resets_at,
                     checked_at=excluded.checked_at,
                     pending_used_percent=excluded.pending_used_percent,
                     pending_window_duration_mins=excluded.pending_window_duration_mins,
                     pending_resets_at=excluded.pending_resets_at,
                     pending_checked_at=excluded.pending_checked_at""",
                (
                    account_id, state.reading.kind.value, state.reading.used_percent,
                    state.reading.window_duration_mins, state.reading.resets_at,
                    state.reading.checked_at,
                    pending.used_percent if pending else None,
                    pending.window_duration_mins if pending else None,
                    pending.resets_at if pending else None,
                    pending.checked_at if pending else None,
                ),
            )
            inserted = False
            if event is not None:
                cursor = await db.execute(
                    """INSERT OR IGNORE INTO reset_events(
                         account_id, window_kind, reset_type, occurred_at,
                         previous_used_percent, current_used_percent, fingerprint
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        account_id, event.kind.value, event.reset_type.value,
                        event.occurred_at, event.previous_used_percent,
                        event.current_used_percent, event.fingerprint,
                    ),
                )
                inserted = cursor.rowcount == 1
            await db.commit()
        return inserted

    async def mark_event_notified(self, account_id: str, fingerprint: str) -> None:
        async with self._write_lock, self.connect() as db:
            await db.execute(
                "UPDATE reset_events SET notified_at=? WHERE account_id=? AND fingerprint=?",
                (utc_timestamp(), account_id, fingerprint),
            )
            await db.commit()

    async def list_unnotified_events(self, account_id: str) -> list[ResetEvent]:
        async with self.connect() as db:
            rows = await (await db.execute(
                """SELECT * FROM reset_events
                   WHERE account_id=? AND notified_at IS NULL ORDER BY occurred_at""",
                (account_id,),
            )).fetchall()
        return [
            ResetEvent(
                kind=WindowKind(row["window_kind"]),
                reset_type=ResetType(row["reset_type"]),
                occurred_at=row["occurred_at"],
                previous_used_percent=row["previous_used_percent"],
                current_used_percent=row["current_used_percent"],
                fingerprint=row["fingerprint"],
            )
            for row in rows
        ]

    async def mark_check_success(self, account_id: str) -> bool:
        async with self._write_lock, self.connect() as db:
            row = await (await db.execute(
                "SELECT failure_notified FROM accounts WHERE id=?", (account_id,)
            )).fetchone()
            was_notified = bool(row and row["failure_notified"])
            await db.execute(
                """UPDATE accounts SET last_success_at=?, last_error=NULL,
                   consecutive_failures=0, failure_notified=0, auth_status='authorized',
                   updated_at=? WHERE id=?""",
                (utc_timestamp(), utc_timestamp(), account_id),
            )
            await db.commit()
        return was_notified

    async def mark_check_failure(self, account_id: str, safe_error: str, threshold: int) -> tuple[int, bool]:
        async with self._write_lock, self.connect() as db:
            await db.execute(
                """UPDATE accounts SET last_error=?, consecutive_failures=consecutive_failures+1,
                   updated_at=? WHERE id=?""",
                (safe_error[:500], utc_timestamp(), account_id),
            )
            row = await (await db.execute(
                "SELECT consecutive_failures, failure_notified FROM accounts WHERE id=?", (account_id,)
            )).fetchone()
            notify = bool(
                row and row["consecutive_failures"] >= threshold and not row["failure_notified"]
            )
            if notify:
                await db.execute("UPDATE accounts SET failure_notified=1 WHERE id=?", (account_id,))
            await db.commit()
        return (row["consecutive_failures"] if row else 0, notify)

    async def get_states(self, account_id: str) -> dict[WindowKind, DetectorState]:
        result: dict[WindowKind, DetectorState] = {}
        for kind in WindowKind:
            state = await self.load_detector_state(account_id, kind)
            if state:
                result[kind] = state
        return result
