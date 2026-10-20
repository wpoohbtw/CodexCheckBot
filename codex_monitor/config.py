from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} должен быть целым числом") from exc
    if value <= 0:
        raise ValueError(f"{name} должен быть больше нуля")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    check_interval_seconds: int
    reset_confirm_delay_seconds: int
    log_level: str
    database_path: Path
    accounts_storage_path: Path
    log_path: Path
    codex_executable: str
    app_server_timeout_seconds: int
    auth_timeout_seconds: int
    monitor_max_concurrency: int
    monitor_retry_attempts: int
    error_notification_threshold: int

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN не задан в .env")

        codex_executable = os.getenv("CODEX_EXECUTABLE", "codex").strip() or "codex"
        return cls(
            telegram_bot_token=token,
            check_interval_seconds=_positive_int("CHECK_INTERVAL_SECONDS", 300),
            reset_confirm_delay_seconds=_positive_int("RESET_CONFIRM_DELAY_SECONDS", 5),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            database_path=Path(os.getenv("DATABASE_PATH", "./data/codex_monitor.db")).resolve(),
            accounts_storage_path=Path(os.getenv("ACCOUNTS_STORAGE_PATH", "./accounts")).resolve(),
            log_path=Path(os.getenv("LOG_PATH", "./logs/codex_monitor.log")).resolve(),
            codex_executable=codex_executable,
            app_server_timeout_seconds=_positive_int("APP_SERVER_REQUEST_TIMEOUT_SECONDS", 30),
            auth_timeout_seconds=_positive_int("AUTH_TIMEOUT_SECONDS", 900),
            monitor_max_concurrency=_positive_int("MONITOR_MAX_CONCURRENCY", 2),
            monitor_retry_attempts=_positive_int("MONITOR_RETRY_ATTEMPTS", 3),
            error_notification_threshold=_positive_int("ERROR_NOTIFICATION_THRESHOLD", 3),
        )

    def prepare_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.accounts_storage_path.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def validate_codex(self) -> None:
        executable = Path(self.codex_executable)
        if executable.parent != Path(".") and executable.exists():
            return
        if shutil.which(self.codex_executable) is None:
            raise ValueError(
                f"Codex CLI не найден: {self.codex_executable}. "
                "Установите @openai/codex или задайте CODEX_EXECUTABLE."
            )
