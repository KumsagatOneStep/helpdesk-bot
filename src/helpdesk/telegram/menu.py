from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from helpdesk.db.models import Staff

MY = "📋 Мои заявки"
OPEN = "📂 Открытые"
SEARCH = "🔍 Поиск"
STATS = "📊 Статистика"
EXPORT = "📥 Выгрузка"
SHIFT_OFF = "🔴 Уйти со смены"
SHIFT_ON = "🟢 Выйти на смену"
STAFF = "👥 Специалисты"
CANCEL = "✖️ Отмена"
NEW_TASK = "➕ Новая задача"
STOCK = "📦 Склад"

MENU_TEXTS = frozenset(
    {MY, OPEN, NEW_TASK, STOCK, SEARCH, STATS, EXPORT, SHIFT_OFF, SHIFT_ON, STAFF, CANCEL}
)


def menu_rows(staff: Staff) -> list[list[str]]:
    rows = [
        [MY, OPEN],
        [NEW_TASK],
        [STOCK],
        [SEARCH, STATS],
        [EXPORT, SHIFT_OFF if staff.on_shift else SHIFT_ON],
    ]
    if staff.is_admin:
        rows.append([STAFF])
    return rows


def _keyboard(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_menu(staff: Staff) -> ReplyKeyboardMarkup:
    return _keyboard(menu_rows(staff))


def cancel_menu() -> ReplyKeyboardMarkup:
    return _keyboard([[CANCEL]])


def is_client_reply_text(text: str | None) -> bool:
    """Текст специалиста, который нужно отправить пользователю, а не команда/кнопка меню."""
    return bool(text) and not text.startswith("/") and text not in MENU_TEXTS
