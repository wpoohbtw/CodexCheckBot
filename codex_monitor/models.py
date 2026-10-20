from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class WindowKind(StrEnum):
    FIVE_HOUR = "five_hour"
    WEEKLY = "weekly"


class ResetType(StrEnum):
    PLANNED = "planned"
    EARLY = "early"


@dataclass(frozen=True, slots=True)
class LimitReading:
    kind: WindowKind
    used_percent: int
    window_duration_mins: int
    resets_at: int | None
    checked_at: int

    @property
    def remaining_percent(self) -> int:
        return max(0, min(100, 100 - self.used_percent))


@dataclass(frozen=True, slots=True)
class DetectorState:
    reading: LimitReading
    pending_reading: LimitReading | None = None


@dataclass(frozen=True, slots=True)
class ResetEvent:
    kind: WindowKind
    reset_type: ResetType
    occurred_at: int
    previous_used_percent: int
    current_used_percent: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class DetectionResult:
    state: DetectorState
    event: ResetEvent | None = None
    candidate_started: bool = False


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    owner_id: int
    display_name: str
    codex_home_key: str
    auth_status: str
    monitoring_enabled: bool
    last_success_at: int | None
    last_error: str | None
    consecutive_failures: int
    failure_notified: bool


@dataclass(frozen=True, slots=True)
class NotificationSettings:
    reset_five_hour: bool = True
    reset_weekly: bool = True
    early_resets: bool = True
    planned_resets: bool = True
    errors: bool = True


def utc_timestamp() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def account_from_row(row: Any) -> Account:
    return Account(
        id=row["id"],
        owner_id=row["owner_id"],
        display_name=row["display_name"],
        codex_home_key=row["codex_home_key"],
        auth_status=row["auth_status"],
        monitoring_enabled=bool(row["monitoring_enabled"]),
        last_success_at=row["last_success_at"],
        last_error=row["last_error"],
        consecutive_failures=row["consecutive_failures"],
        failure_notified=bool(row["failure_notified"]),
    )
