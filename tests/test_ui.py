from datetime import datetime
from zoneinfo import ZoneInfo

from codex_monitor.models import Account, DetectorState, LimitReading, WindowKind
from codex_monitor.ui import format_limits_blocks, main_screen, normalize_name


def account() -> Account:
    return Account(
        id="account-1",
        owner_id=1,
        display_name="Основа",
        codex_home_key="home-1",
        auth_status="authorized",
        monitoring_enabled=True,
        last_success_at=None,
        last_error=None,
        consecutive_failures=0,
        failure_notified=False,
    )


def test_main_menu_has_required_actions() -> None:
    text, markup = main_screen()
    assert "Это бот-чекер лимитов Codex" in text
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert [button.text for button in buttons] == [
        "➕ Добавить аккаунт",
        "Добавленные аккаунты",
        "Настройка уведомлений",
    ]


def test_account_name_is_normalized_and_limited() -> None:
    assert normalize_name("  Основной   Plus ") == "Основной Plus"
    assert normalize_name("x" * 41) is None
    assert normalize_name(None) is None


def test_limits_command_format_includes_duration_and_exact_moscow_time() -> None:
    moscow = ZoneInfo("Europe/Moscow")
    now = int(datetime(2026, 10, 20, 15, 37, tzinfo=moscow).timestamp())
    reset_five = int(datetime(2026, 10, 20, 17, 42, tzinfo=moscow).timestamp())
    reset_week = int(datetime(2026, 10, 27, 12, 42, tzinfo=moscow).timestamp())
    states = {
        "account-1": {
            WindowKind.FIVE_HOUR: DetectorState(
                LimitReading(WindowKind.FIVE_HOUR, 100, 300, reset_five, now)
            ),
            WindowKind.WEEKLY: DetectorState(
                LimitReading(WindowKind.WEEKLY, 16, 10080, reset_week, now)
            ),
        }
    }

    block = format_limits_blocks([account()], states, now_timestamp=now)[0]

    assert "<b>Основа:</b>" in block
    assert "5ч - осталось: 0% | Сброс через 2 ч 5 мин (20.10.2026 17:42 МСК)" in block
    assert "Недельный - осталось: 84% | Сброс через 165 ч 5 мин (27.10.2026 12:42 МСК)" in block


def test_limits_command_does_not_invent_missing_window_data() -> None:
    block = format_limits_blocks([account()], {"account-1": {}}, now_timestamp=1)[0]
    assert "5ч - данных пока нет" in block
    assert "Недельный - данных пока нет" in block
