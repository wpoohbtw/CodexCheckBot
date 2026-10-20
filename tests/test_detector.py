from codex_monitor.detector import ResetDetector
from codex_monitor.models import DetectorState, LimitReading, ResetType, WindowKind


def reading(used: int, checked: int, reset: int | None = 10_000) -> LimitReading:
    return LimitReading(WindowKind.WEEKLY, used, 10_080, reset, checked)


def test_first_reading_is_only_baseline() -> None:
    result = ResetDetector().evaluate(None, reading(86, 1_000))
    assert result.event is None
    assert not result.candidate_started
    assert result.state.reading.used_percent == 86


def test_early_reset_requires_confirmation() -> None:
    detector = ResetDetector()
    state = DetectorState(reading(86, 1_000, reset=20_000))
    candidate = detector.evaluate(state, reading(5, 2_000, reset=30_000))
    assert candidate.candidate_started
    assert candidate.event is None

    confirmed = detector.evaluate(candidate.state, reading(6, 2_010, reset=30_000))
    assert confirmed.event is not None
    assert confirmed.event.reset_type is ResetType.EARLY
    assert confirmed.event.previous_used_percent == 86
    assert confirmed.event.current_used_percent == 6


def test_planned_partial_reset_is_detected_near_expected_time() -> None:
    detector = ResetDetector()
    state = DetectorState(reading(80, 8_000, reset=10_000))
    candidate = detector.evaluate(state, reading(45, 10_100, reset=20_000))
    confirmed = detector.evaluate(candidate.state, reading(44, 10_110, reset=20_000))
    assert confirmed.event is not None
    assert confirmed.event.reset_type is ResetType.PLANNED


def test_small_drop_is_not_reset() -> None:
    state = DetectorState(reading(86, 1_000))
    result = ResetDetector().evaluate(state, reading(75, 2_000))
    assert result.event is None
    assert result.state.pending_reading is None
    assert result.state.reading.used_percent == 75


def test_anomalous_candidate_that_bounces_back_is_discarded() -> None:
    detector = ResetDetector()
    state = DetectorState(reading(90, 1_000))
    candidate = detector.evaluate(state, reading(0, 2_000))
    result = detector.evaluate(candidate.state, reading(91, 2_010))
    assert result.event is None
    assert result.state.pending_reading is None
    assert result.state.reading.used_percent == 91
