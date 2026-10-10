from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from helpdesk.db.models import Staff
from helpdesk.stock import render
from helpdesk.stock import service as stock
from helpdesk.stock.catalog import CATEGORIES, CONDITION_ONE, CONDITIONS
from helpdesk.telegram import menu
from helpdesk.telegram.filters import ID_DATA, IsActive, arg_id
from helpdesk.telegram.gateway import markup

router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

ONLY_COORD = "Только для координатора"
COND_RE = "(new|used|repaired)"
NOT_CANCEL = F.text != menu.CANCEL


class StockForm(StatesGroup):
    take_qty = State()
    in_qty = State()
    fix_qty = State()
    min_qty = State()
    new_name = State()
    new_min = State()
    search = State()
    import_file = State()
    import_confirm = State()


def leaves_dialog(text: str | None) -> bool:
    """Команда или кнопка меню (кроме «Отмена» и «Склад») посреди диалога склада."""
    if not text:
        return False
    return text.startswith("/") or (
        text in menu.MENU_TEXTS and text not in (menu.CANCEL, menu.STOCK)
    )


NOT_LEAVING = ~F.text.func(leaves_dialog)


def parse_int(text: str | None, lo: int, hi: int) -> int | None:
    t = (text or "").strip()
    if not t.isdecimal():
        return None
    n = int(t)
    return n if lo <= n <= hi else None


async def _deny(cq: CallbackQuery, staff: Staff) -> bool:
    if stock.is_coordinator(staff):
        return False
    await cq.answer(ONLY_COORD, show_alert=True)
    return True


async def _drop_markup(cq: CallbackQuery) -> None:
    try:
        await cq.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


async def _edit(cq: CallbackQuery, text: str, buttons) -> None:
    try:
        await cq.message.edit_text(text, reply_markup=markup(buttons))
    except TelegramBadRequest:  # «message is not modified» и т.п.
        pass


async def _home(s, staff: Staff):
    totals = await stock.category_totals(s)
    return "📦 Склад", render.home_buttons(totals, stock.is_coordinator(staff))


@router.message(F.text == menu.STOCK, IsActive())
async def stock_home(message: Message, state: FSMContext, s, staff: Staff):
    await state.clear()
    text, buttons = await _home(s, staff)
    await message.answer(text, reply_markup=markup(buttons))


@router.message(StateFilter(StockForm), F.text.func(leaves_dialog))
async def leave_dialog(message: Message, state: FSMContext):
    await state.clear()
    raise SkipHandler()


@router.callback_query(F.data == "sk_home", IsActive())
async def cb_home(cq: CallbackQuery, s, staff: Staff):
    await _edit(cq, *await _home(s, staff))
    await cq.answer()


@router.callback_query(F.data.regexp(r"^sk_cat:[a-z]{1,24}:\d{1,4}$"), IsActive())
async def cb_category(cq: CallbackQuery, s):
    _, code, page = cq.data.split(":")
    if code not in CATEGORIES:
        await cq.answer("Кнопка устарела", show_alert=True)
        return
    rows, page, pages = await stock.items_in(s, code, int(page))
    title = CATEGORIES[code] + (f" · стр. {page + 1}/{pages}" if pages > 1 else "")
    if not rows:
        title += "\n\nПока пусто."
    await _edit(cq, title, render.category_buttons(rows, code, page, pages))
    await cq.answer()


async def _active_row(cq: CallbackQuery, s, item_id: int):
    item = await stock.get_item(s, item_id)
    if item is None or item.archived:
        await cq.answer("Позиция в архиве", show_alert=True)
        return None
    return await stock.row(s, item)


@router.callback_query(F.data.regexp(ID_DATA.format("sk_item")), IsActive())
async def cb_item(cq: CallbackQuery, s, staff: Staff):
    row = await _active_row(cq, s, arg_id(cq))
    if row is None:
        return
    await _edit(cq, render.card(row), render.card_buttons(row, stock.is_coordinator(staff)))
    await cq.answer()


@router.callback_query(F.data == "sk_find", IsActive())
async def cb_find(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(StockForm.search)
    await cq.message.answer("Введите часть названия:", reply_markup=menu.cancel_menu())
    await cq.answer()


@router.message(StockForm.search, IsActive(), F.text.func(menu.is_client_reply_text))
async def got_search(message: Message, state: FSMContext, s, staff: Staff):
    await state.clear()
    rows = await stock.search(s, message.text)
    if not rows:
        await message.answer("Ничего не найдено.", reply_markup=menu.main_menu(staff))
        return
    await message.answer("Найдено:", reply_markup=menu.main_menu(staff))
    buttons = [[(render.item_label(r), f"sk_item:{r.item.id}")] for r in rows]
    await message.answer("Выберите позицию:", reply_markup=markup(buttons))


# ── Взять ──────────────────────────────────────────────────────────────


async def _ask_take_qty(message, state: FSMContext, row, condition: str) -> None:
    available = row.levels[condition]
    await state.clear()
    await state.set_state(StockForm.take_qty)
    await state.update_data(item_id=row.item.id, condition=condition)
    buttons = [[(str(n), f"sk_tq:{n}") for n in (1, 2, 3) if n <= available]]
    buttons[0].append(("Другое", "sk_tq:other"))
    await message.answer(
        f"{row.item.name} ({CONDITION_ONE[condition]}), есть {available}. Сколько взять?",
        reply_markup=markup(buttons),
    )


@router.callback_query(F.data.regexp(ID_DATA.format("sk_take")), IsActive())
async def cb_take(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    row = await _active_row(cq, s, arg_id(cq))
    if row is None:
        return
    have = [c for c in CONDITIONS if row.levels[c] > 0]
    if not have:
        await cq.answer("Нет в наличии", show_alert=True)
        return
    if len(have) == 1:
        await _ask_take_qty(cq.message, state, row, have[0])
    else:
        buttons = [
            [(f"{CONDITIONS[c]} · {row.levels[c]}", f"sk_tc:{row.item.id}:{c}") for c in have]
        ]
        await cq.message.answer(f"{row.item.name}: какое состояние?", reply_markup=markup(buttons))
    await cq.answer()


@router.callback_query(F.data.regexp(rf"^sk_tc:\d{{1,9}}:{COND_RE}$"), IsActive())
async def cb_take_cond(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    _, item_id, condition = cq.data.split(":")
    row = await _active_row(cq, s, int(item_id))
    if row is None:
        return
    await _drop_markup(cq)
    if row.levels[condition] == 0:
        await cq.message.answer("Этого уже нет в наличии.")
    else:
        await _ask_take_qty(cq.message, state, row, condition)
    await cq.answer()


async def _do_take(message, state: FSMContext, s, staff: Staff, qty: int) -> None:
    data = await state.get_data()
    try:
        r = await stock.take(s, staff, data["item_id"], data["condition"], qty)
    except stock.Archived:
        await state.clear()
        await message.answer("Позиция в архиве.", reply_markup=menu.main_menu(staff))
        return
    if not r.ok:
        if r.available == 0:
            await state.clear()
            await message.answer("Уже закончилось.", reply_markup=menu.main_menu(staff))
        else:
            await message.answer(f"Осталось только {r.available}.")
        return
    await state.clear()
    item = await stock.get_item(s, data["item_id"])
    await message.answer(
        f"✅ Взято: {item.name} ×{qty} ({CONDITION_ONE[data['condition']]}). Осталось {r.left}.",
        reply_markup=menu.main_menu(staff),
    )


@router.callback_query(StockForm.take_qty, F.data.regexp(r"^sk_tq:(\d{1,4}|other)$"), IsActive())
async def cb_take_qty(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    arg = cq.data.split(":")[1]
    if arg == "other":
        data = await state.get_data()
        have = (await stock.levels(s, data["item_id"]))[data["condition"]]
        await cq.message.answer(
            f"Введите число от 1 до {max(have, 1)}.", reply_markup=menu.cancel_menu()
        )
    else:
        await _do_take(cq.message, state, s, staff, int(arg))
    await cq.answer()


@router.message(StockForm.take_qty, IsActive(), F.text.func(menu.is_client_reply_text))
async def got_take_qty(message: Message, state: FSMContext, s, staff: Staff):
    data = await state.get_data()
    have = (await stock.levels(s, data["item_id"]))[data["condition"]]
    qty = parse_int(message.text, 1, max(have, 1))
    if qty is None:
        await message.answer(f"Введите число от 1 до {max(have, 1)}.")
        return
    await _do_take(message, state, s, staff, qty)


@router.callback_query(F.data == "sk_cancel", IsActive())
async def cb_cancel(cq: CallbackQuery, state: FSMContext, staff: Staff):
    await state.clear()
    await _drop_markup(cq)
    await cq.message.answer("Отменено.", reply_markup=menu.main_menu(staff))
    await cq.answer()


# Обработчики задач 5–6 вставлять ВЫШЕ этой строки: stray_input и stale — последние.


@router.message(StateFilter(StockForm), IsActive(), NOT_CANCEL, NOT_LEAVING)
async def stray_input(message: Message):
    """Лишний ввод посреди диалога склада не должен уйти пользователю WhatsApp."""
    await message.answer("Используйте кнопки выше или «✖️ Отмена».")


@router.callback_query(F.data.startswith("sk_"))
async def stale(cq: CallbackQuery):
    await cq.answer("Диалог уже завершён", show_alert=True)
