"""Read-only protocol smoke test using an isolated, unauthenticated CODEX_HOME."""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path

from codex_monitor.appserver import AppServerSession


async def check(executable: str) -> None:
    with tempfile.TemporaryDirectory(prefix="codex-appserver-smoke-") as directory:
        session = AppServerSession(executable, Path(directory), 20)
        try:
            await session.start()
            result = await session.request("account/read", {"refreshToken": False})
            if result.get("account") is not None:
                raise RuntimeError("Изолированный CODEX_HOME неожиданно содержит авторизацию")
            print("OK: initialize/initialized and account/read")
        finally:
            await session.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default="codex")
    args = parser.parse_args()
    asyncio.run(check(args.codex))


if __name__ == "__main__":
    main()
