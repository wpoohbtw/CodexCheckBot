from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import subprocess
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .config import Settings
from .db import Database
from .monitor import MonitorService
from .ui import TelegramUI


def configure_logging(settings: Settings) -> None:
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        settings.log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        handlers=[console, file_handler],
        force=True,
    )


async def run_bot() -> None:
    settings = Settings.load()
    settings.prepare_directories()
    settings.validate_codex()
    configure_logging(settings)
    logger = logging.getLogger(__name__)

    db = Database(settings.database_path)
    await db.initialize()
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    monitor = MonitorService(bot, db, settings)
    ui = TelegramUI(bot, db, settings, monitor)
    dispatcher.include_router(ui.router)

    monitor.start()
    logger.info("Бот Codex Limits Monitor запущен")
    try:
        await dispatcher.start_polling(bot)
    finally:
        await ui.close()
        await monitor.stop()
        await bot.session.close()
        logger.info("Бот Codex Limits Monitor остановлен")


async def check_environment() -> int:
    executable = shutil.which("codex")
    if executable is None:
        print("ERROR: Codex CLI не найден в PATH")
        return 1
    result = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        print("ERROR: Codex CLI не запускается")
        return 1
    with tempfile.TemporaryDirectory(prefix="codex-monitor-check-") as directory:
        db = Database(Path(directory) / "check.db")
        await db.initialize()
    print(f"OK: Python {sys.version.split()[0]}")
    print(f"OK: {result.stdout.strip()}")
    print("OK: SQLite schema initialized")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex Limits Monitor")
    parser.add_argument(
        "--check", action="store_true", help="Проверить окружение без запуска Telegram-бота"
    )
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        raise SystemExit("Требуется Python 3.11 или новее")
    if args.check:
        raise SystemExit(asyncio.run(check_environment()))
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
