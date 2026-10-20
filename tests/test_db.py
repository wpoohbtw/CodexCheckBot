from pathlib import Path

import pytest

from codex_monitor.db import Database
from codex_monitor.detector import ResetDetector
from codex_monitor.models import Account, DetectorState, LimitReading, WindowKind


def account(account_id: str = "a1", owner_id: int = 1) -> Account:
    return Account(
        id=account_id,
        owner_id=owner_id,
        display_name="Основной",
        codex_home_key=f"home-{account_id}",
        auth_status="authorized",
        monitoring_enabled=True,
        last_success_at=None,
        last_error=None,
        consecutive_failures=0,
        failure_notified=False,
    )


@pytest.mark.asyncio
async def test_accounts_are_owner_scoped_and_notifications_default_on(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    await db.initialize()
    await db.upsert_user(1, 1)
    await db.upsert_user(2, 2)
    await db.create_account(account())

    assert await db.get_account("a1", 1) is not None
    assert await db.get_account("a1", 2) is None
    settings = await db.get_notification_settings("a1")
    assert settings.reset_five_hour
    assert settings.reset_weekly
    assert settings.early_resets
    assert settings.planned_resets
    assert settings.errors


@pytest.mark.asyncio
async def test_detector_candidate_survives_database_reopen_and_event_is_deduplicated(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    db = Database(path)
    await db.initialize()
    await db.upsert_user(1, 1)
    await db.create_account(account())
    detector = ResetDetector()
    baseline = LimitReading(WindowKind.FIVE_HOUR, 90, 300, 10_000, 1_000)
    candidate_reading = LimitReading(WindowKind.FIVE_HOUR, 10, 300, 20_000, 2_000)
    candidate = detector.evaluate(DetectorState(baseline), candidate_reading)
    await db.save_detection("a1", candidate.state, None)

    reopened = Database(path)
    restored = await reopened.load_detector_state("a1", WindowKind.FIVE_HOUR)
    assert restored is not None and restored.pending_reading is not None
    confirmed = detector.evaluate(
        restored, LimitReading(WindowKind.FIVE_HOUR, 10, 300, 20_000, 2_010)
    )
    assert confirmed.event is not None
    assert await reopened.save_detection("a1", confirmed.state, confirmed.event)
    assert not await reopened.save_detection("a1", confirmed.state, confirmed.event)
