from __future__ import annotations

import hashlib

from .models import (
    DetectionResult,
    DetectorState,
    LimitReading,
    ResetEvent,
    ResetType,
)


class ResetDetector:
    """Pure two-sample reset detector with explicit confirmation.

    A candidate requires at least a 20 percentage-point drop and restoration of
    at least 40% of the previously consumed quota. A second close reading must
    confirm the candidate. This tolerates partial resets while filtering small
    corrections and single anomalous responses.
    """

    MIN_DROP_POINTS = 20
    MIN_RESTORED_FRACTION = 0.40
    CONFIRM_TOLERANCE_POINTS = 3
    PLANNED_TOLERANCE_SECONDS = 15 * 60

    def evaluate(
        self,
        state: DetectorState | None,
        current: LimitReading,
    ) -> DetectionResult:
        if state is None:
            return DetectionResult(DetectorState(reading=current))
        if state.reading.kind != current.kind:
            raise ValueError("Нельзя сравнивать разные окна лимитов")

        baseline = state.reading
        if state.pending_reading is not None:
            candidate = state.pending_reading
            if self._is_candidate(baseline, current) and (
                current.used_percent <= candidate.used_percent + self.CONFIRM_TOLERANCE_POINTS
            ):
                event = self._make_event(baseline, current)
                return DetectionResult(DetectorState(reading=current), event=event)
            return DetectionResult(DetectorState(reading=current))

        if self._is_candidate(baseline, current):
            return DetectionResult(
                DetectorState(reading=baseline, pending_reading=current),
                candidate_started=True,
            )

        return DetectionResult(DetectorState(reading=current))

    def _is_candidate(self, previous: LimitReading, current: LimitReading) -> bool:
        drop = previous.used_percent - current.used_percent
        if previous.used_percent <= 0 or drop < self.MIN_DROP_POINTS:
            return False
        return (drop / previous.used_percent) >= self.MIN_RESTORED_FRACTION

    def _make_event(self, previous: LimitReading, current: LimitReading) -> ResetEvent:
        planned = (
            previous.resets_at is not None
            and abs(current.checked_at - previous.resets_at) <= self.PLANNED_TOLERANCE_SECONDS
        )
        reset_type = ResetType.PLANNED if planned else ResetType.EARLY
        identity = ":".join(
            [
                previous.kind.value,
                str(previous.resets_at or 0),
                str(current.resets_at or 0),
                str(previous.checked_at // 300),
            ]
        )
        fingerprint = hashlib.sha256(identity.encode("ascii")).hexdigest()
        return ResetEvent(
            kind=previous.kind,
            reset_type=reset_type,
            occurred_at=current.checked_at,
            previous_used_percent=previous.used_percent,
            current_used_percent=current.used_percent,
            fingerprint=fingerprint,
        )
