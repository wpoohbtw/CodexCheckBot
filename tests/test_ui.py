from codex_monitor.ui import main_screen, normalize_name


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
