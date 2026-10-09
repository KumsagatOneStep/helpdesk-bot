from helpdesk.db.models import Staff
from helpdesk.telegram import menu


def _staff(**kw):
    return Staff(
        name="Ерлан", is_admin=kw.get("is_admin", False), on_shift=kw.get("on_shift", True)
    )


def test_specialist_menu_on_shift():
    assert menu.menu_rows(_staff()) == [
        ["📋 Мои заявки", "📂 Открытые"],
        ["➕ Новая задача"],
        ["🔍 Поиск", "📊 Статистика"],
        ["📥 Выгрузка", "🔴 Уйти со смены"],
    ]


def test_shift_button_follows_state_and_admin_row():
    rows = menu.menu_rows(_staff(on_shift=False, is_admin=True))
    assert rows[3][1] == "🟢 Выйти на смену"
    assert rows[-1] == ["👥 Специалисты"]


def test_keyboard_is_persistent_and_resized():
    kb = menu.main_menu(_staff())
    assert kb.is_persistent and kb.resize_keyboard
    assert [[b.text for b in row] for row in kb.keyboard] == menu.menu_rows(_staff())
    assert [[b.text for b in row] for row in menu.cancel_menu().keyboard] == [["✖️ Отмена"]]


def test_menu_buttons_never_go_to_client():
    for text in menu.MENU_TEXTS:
        assert not menu.is_client_reply_text(text)
    assert not menu.is_client_reply_text("/my")
    assert not menu.is_client_reply_text("")
    assert menu.is_client_reply_text("Сейчас подойду")
