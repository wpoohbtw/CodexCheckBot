from codex_monitor.appserver import parse_rate_limits
from codex_monitor.models import WindowKind


def test_windows_are_identified_by_duration_not_position() -> None:
    payload = {
        "rateLimits": {
            "primary": {"usedPercent": 70, "windowDurationMins": 10080, "resetsAt": 20},
            "secondary": {"usedPercent": 15, "windowDurationMins": 300, "resetsAt": 10},
        }
    }
    result = parse_rate_limits(payload, checked_at=1)
    assert result[WindowKind.FIVE_HOUR].used_percent == 15
    assert result[WindowKind.WEEKLY].used_percent == 70


def test_codex_bucket_is_preferred_over_other_model_buckets() -> None:
    payload = {
        "rateLimits": {"primary": {"usedPercent": 99, "windowDurationMins": 300}},
        "rateLimitsByLimitId": {
            "spark": {"primary": {"usedPercent": 80, "windowDurationMins": 300}},
            "codex": {
                "primary": {"usedPercent": 12, "windowDurationMins": 300},
                "secondary": {"usedPercent": 34, "windowDurationMins": 10080},
            },
        },
    }
    result = parse_rate_limits(payload, checked_at=1)
    assert result[WindowKind.FIVE_HOUR].used_percent == 12
    assert result[WindowKind.WEEKLY].used_percent == 34


def test_missing_or_unknown_duration_is_not_guessed() -> None:
    payload = {
        "rateLimits": {
            "primary": {"usedPercent": 10},
            "secondary": {"usedPercent": 20, "windowDurationMins": 1440},
        }
    }
    assert parse_rate_limits(payload, checked_at=1) == {}


def test_duplicate_window_kind_is_treated_as_ambiguous() -> None:
    payload = {
        "rateLimits": {
            "primary": {"usedPercent": 10, "windowDurationMins": 300},
            "secondary": {"usedPercent": 20, "windowDurationMins": 310},
        }
    }
    assert parse_rate_limits(payload, checked_at=1) == {}
