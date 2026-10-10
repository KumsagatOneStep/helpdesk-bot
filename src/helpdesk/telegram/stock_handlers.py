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


# ── Координатор ────────────────────────────────────────────────────────


def _cond_buttons(prefix: str, item_id: int):
    return [[(label, f"{prefix}:{item_id}:{c}") for c, label in CONDITIONS.items()]]


async def _coord_text(message: Message, state: FSMContext, staff: Staff) -> bool:
    """Текстовый шаг координаторского диалога: специалиста не пускаем."""
    if stock.is_coordinator(staff):
        return True
    await state.clear()
    await message.answer(f"{ONLY_COORD}.", reply_markup=menu.main_menu(staff))
    return False


@router.callback_query(F.data.regexp(ID_DATA.format("sk_in")), IsActive())
async def cb_in(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    if await _deny(cq, staff) or (row := await _active_row(cq, s, arg_id(cq))) is None:
        return
    await state.clear()
    await cq.message.answer(
        f"{row.item.name}: что пришло?", reply_markup=markup(_cond_buttons("sk_ic", row.item.id))
    )
    await cq.answer()


@router.callback_query(F.data.regexp(rf"^sk_ic:\d{{1,9}}:{COND_RE}$"), IsActive())
async def cb_in_cond(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    _, item_id, condition = cq.data.split(":")
    if await _deny(cq, staff) or (row := await _active_row(cq, s, int(item_id))) is None:
        return
    await _drop_markup(cq)
    await state.clear()
    await state.set_state(StockForm.in_qty)
    await state.update_data(item_id=row.item.id, condition=condition)
    await cq.message.answer(
        f"{row.item.name} ({CONDITION_ONE[condition]}): сколько пришло? 1–{stock.MAX_IN}",
        reply_markup=menu.cancel_menu(),
    )
    await cq.answer()


@router.message(StockForm.in_qty, IsActive(), F.text.func(menu.is_client_reply_text))
async def got_in_qty(message: Message, state: FSMContext, s, staff: Staff):
    if not await _coord_text(message, state, staff):
        return
    qty = parse_int(message.text, 1, stock.MAX_IN)
    if qty is None:
        await message.answer(f"Введите число от 1 до {stock.MAX_IN}.")
        return
    data = await state.get_data()
    await state.clear()
    try:
        now = await stock.receive(s, staff, data["item_id"], data["condition"], qty)
    except stock.Archived:
        await message.answer("Позиция в архиве.", reply_markup=menu.main_menu(staff))
        return
    item = await stock.get_item(s, data["item_id"])
    await message.answer(
        f"➕ {item.name} +{qty} ({CONDITION_ONE[data['condition']]}). Теперь {now}.",
        reply_markup=menu.main_menu(staff),
    )


@router.callback_query(F.data.regexp(ID_DATA.format("sk_fix")), IsActive())
async def cb_fix(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    if await _deny(cq, staff) or (row := await _active_row(cq, s, arg_id(cq))) is None:
        return
    await state.clear()
    await cq.message.answer(
        f"{row.item.name}: какое состояние исправить?",
        reply_markup=markup(_cond_buttons("sk_fc", row.item.id)),
    )
    await cq.answer()


@router.callback_query(F.data.regexp(rf"^sk_fc:\d{{1,9}}:{COND_RE}$"), IsActive())
async def cb_fix_cond(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    _, item_id, condition = cq.data.split(":")
    if await _deny(cq, staff) or (row := await _active_row(cq, s, int(item_id))) is None:
        return
    await _drop_markup(cq)
    await state.clear()
    await state.set_state(StockForm.fix_qty)
    await state.update_data(item_id=row.item.id, condition=condition)
    await cq.message.answer(
        f"{row.item.name} ({CONDITION_ONE[condition]}): сейчас {row.levels[condition]}. "
        f"Сколько на самом деле? 0–{stock.MAX_LEVEL}",
        reply_markup=menu.cancel_menu(),
    )
    await cq.answer()


@router.message(StockForm.fix_qty, IsActive(), F.text.func(menu.is_client_reply_text))
async def got_fix_qty(message: Message, state: FSMContext, s, staff: Staff):
    if not await _coord_text(message, state, staff):
        return
    qty = parse_int(message.text, 0, stock.MAX_LEVEL)
    if qty is None:
        await message.answer(f"Введите число от 0 до {stock.MAX_LEVEL}.")
        return
    data = await state.get_data()
    await state.clear()
    try:
        await stock.set_level(s, staff, data["item_id"], data["condition"], qty)
    except stock.Archived:
        await message.answer("Позиция в архиве.", reply_markup=menu.main_menu(staff))
        return
    item = await stock.get_item(s, data["item_id"])
    await message.answer(
        f"✏️ {item.name} ({CONDITION_ONE[data['condition']]}): теперь {qty}.",
        reply_markup=menu.main_menu(staff),
    )


@router.callback_query(F.data.regexp(ID_DATA.format("sk_min")), IsActive())
async def cb_min(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    if await _deny(cq, staff) or (row := await _active_row(cq, s, arg_id(cq))) is None:
        return
    await state.clear()
    await state.set_state(StockForm.min_qty)
    await state.update_data(item_id=row.item.id)
    now = row.item.min_qty if row.item.min_qty is not None else "не задан"
    await cq.message.answer(
        f"Минимум для «{row.item.name}» (сейчас {now}). Введите число 0–{stock.MAX_LEVEL}:",
        reply_markup=markup([[("Убрать", "sk_minx")]]),
    )
    await cq.answer()


async def _apply_min(message, state: FSMContext, s, staff: Staff, value: int | None) -> None:
    data = await state.get_data()
    await state.clear()
    try:
        await stock.set_min(s, staff, data["item_id"], value)
    except stock.Archived:
        await message.answer("Позиция в архиве.", reply_markup=menu.main_menu(staff))
        return
    item = await stock.get_item(s, data["item_id"])
    text = (
        f"🔔 Минимум для «{item.name}»: {value}."
        if value is not None
        else f"🔔 Минимум для «{item.name}» убран."
    )
    await message.answer(text, reply_markup=menu.main_menu(staff))


@router.message(StockForm.min_qty, IsActive(), F.text.func(menu.is_client_reply_text))
async def got_min(message: Message, state: FSMContext, s, staff: Staff):
    if not await _coord_text(message, state, staff):
        return
    value = parse_int(message.text, 0, stock.MAX_LEVEL)
    if value is None:
        await message.answer(f"Введите число от 0 до {stock.MAX_LEVEL} или нажмите «Убрать».")
        return
    await _apply_min(message, state, s, staff, value)


@router.callback_query(StockForm.min_qty, F.data == "sk_minx", IsActive())
async def cb_min_clear(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    if await _deny(cq, staff):
        return
    await _drop_markup(cq)
    await _apply_min(cq.message, state, s, staff, None)
    await cq.answer()


@router.callback_query(F.data.regexp(ID_DATA.format("sk_arch")), IsActive())
async def cb_arch(cq: CallbackQuery, s, staff: Staff):
    if await _deny(cq, staff) or (row := await _active_row(cq, s, arg_id(cq))) is None:
        return
    await cq.message.answer(
        f"Убрать «{row.item.name}» в архив? История выдач сохранится.",
        reply_markup=markup(
            [[("Да, в архив", f"sk_archok:{row.item.id}"), ("Отмена", "sk_cancel")]]
        ),
    )
    await cq.answer()


@router.callback_query(F.data.regexp(ID_DATA.format("sk_archok")), IsActive())
async def cb_arch_ok(cq: CallbackQuery, s, staff: Staff):
    if await _deny(cq, staff):
        return
    try:
        item = await stock.archive(s, staff, arg_id(cq))
    except stock.Archived:
        await cq.answer("Позиция в архиве", show_alert=True)
        return
    await _edit(cq, f"🗄 {item.name} — в архиве.", None)
    await cq.answer()


@router.callback_query(F.data == "sk_new", IsActive())
async def cb_new(cq: CallbackQuery, state: FSMContext, staff: Staff):
    if await _deny(cq, staff):
        return
    await state.clear()
    cats = [(label, f"sk_nc:{code}") for code, label in CATEGORIES.items()]
    rows = [cats[i : i + 2] for i in range(0, len(cats), 2)]
    await cq.message.answer("Новая позиция — категория:", reply_markup=markup(rows))
    await cq.answer()


@router.callback_query(F.data.regexp(r"^sk_nc:[a-z]{1,24}$"), IsActive())
async def cb_new_cat(cq: CallbackQuery, state: FSMContext, staff: Staff):
    if await _deny(cq, staff):
        return
    code = cq.data.split(":")[1]
    if code not in CATEGORIES:
        await cq.answer("Кнопка устарела", show_alert=True)
        return
    await _drop_markup(cq)
    await state.clear()
    await state.set_state(StockForm.new_name)
    await state.update_data(category=code)
    await cq.message.answer(
        f"{CATEGORIES[code]}: название позиции?", reply_markup=menu.cancel_menu()
    )
    await cq.answer()


@router.message(StockForm.new_name, IsActive(), F.text.func(menu.is_client_reply_text))
async def got_new_name(message: Message, state: FSMContext, s, staff: Staff):
    if not await _coord_text(message, state, staff):
        return
    name = stock.clean_name(message.text)
    if not 1 <= len(name) <= 200:
        await message.answer("Название от 1 до 200 символов.")
        return
    category = (await state.get_data())["category"]
    if dup := await stock.find_active(s, category, name):
        await state.clear()
        await message.answer("Отменено.", reply_markup=menu.main_menu(staff))
        await message.answer(
            f"Уже есть: {dup.name}", reply_markup=markup([[("Открыть", f"sk_item:{dup.id}")]])
        )
        return
    await state.update_data(name=name)
    await state.set_state(StockForm.new_min)
    await message.answer(
        f"Минимум для «{name}»? Число 0–{stock.MAX_LEVEL} или «Пропустить».",
        reply_markup=markup([[("Пропустить", "sk_skipmin")]]),
    )


async def _create(message, state: FSMContext, s, staff: Staff, min_qty: int | None) -> None:
    data = await state.get_data()
    await state.clear()
    try:
        item = await stock.create_item(s, staff, data["category"], data["name"], min_qty)
    except stock.DuplicateName as e:
        await message.answer(
            "Уже есть такая позиция.", reply_markup=markup([[("Открыть", f"sk_item:{e.item_id}")]])
        )
        return
    await message.answer("Готово.", reply_markup=menu.main_menu(staff))
    buttons = _cond_buttons("sk_ic", item.id) + [[("Без прихода — открыть", f"sk_item:{item.id}")]]
    await message.answer(
        f"Позиция «{item.name}» создана. Что пришло?", reply_markup=markup(buttons)
    )


@router.message(StockForm.new_min, IsActive(), F.text.func(menu.is_client_reply_text))
async def got_new_min(message: Message, state: FSMContext, s, staff: Staff):
    if not await _coord_text(message, state, staff):
        return
    value = parse_int(message.text, 0, stock.MAX_LEVEL)
    if value is None:
        await message.answer(f"Введите число от 0 до {stock.MAX_LEVEL} или нажмите «Пропустить».")
        return
    await _create(message, state, s, staff, value)


@router.callback_query(StockForm.new_min, F.data == "sk_skipmin", IsActive())
async def cb_new_skip_min(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    if await _deny(cq, staff):
        return
    await _drop_markup(cq)
    await _create(cq.message, state, s, staff, None)
    await cq.answer()


# Обработчики задач 5–6 вставлять ВЫШЕ этой строки: stray_input и stale — последние.


@router.message(StateFilter(StockForm), IsActive(), NOT_CANCEL, NOT_LEAVING)
async def stray_input(message: Message):
    """Лишний ввод посреди диалога склада не должен уйти пользователю WhatsApp."""
    await message.answer("Используйте кнопки выше или «✖️ Отмена».")


@router.callback_query(F.data.startswith("sk_"))
async def stale(cq: CallbackQuery):
    await cq.answer("Диалог уже завершён", show_alert=True)
