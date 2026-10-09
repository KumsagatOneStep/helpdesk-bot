import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from helpdesk import clock
from helpdesk.config import Settings
from helpdesk.db.models import Client, Staff, Ticket
from helpdesk.staff import service as staff_svc
from helpdesk.stats import export
from helpdesk.stats import service as stats
from helpdesk.telegram import menu, task_handlers
from helpdesk.telegram.filters import ID_DATA as _ID_DATA
from helpdesk.telegram.filters import IsActive, IsAdmin
from helpdesk.telegram.filters import arg_id as _arg_id
from helpdesk.telegram.gateway import AiogramSender, markup
from helpdesk.telegram.media import FileTooBig, extract_content
from helpdesk.telegram.middleware import DbMiddleware
from helpdesk.telegram.parsing import parse_client_args
from helpdesk.tickets import render
from helpdesk.tickets import service as tickets

router = Router()
# только личные чаты: /start в группе иначе записал бы tg_chat_id группы
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

BOT_COMMANDS = [
    ("my", "Мои заявки в работе"),
    ("open", "Все открытые заявки"),
    ("find", "Поиск заявки"),
    ("stats", "Статистика"),
    ("export", "Выгрузка в Excel"),
    ("on", "Я на смене"),
    ("off", "Я не на смене"),
    ("help", "Помощь"),
]

HELP = (
    "Команды:\n"
    "/my — мои заявки в работе\n"
    "/open — все незакрытые заявки\n"
    "/find <№ | ФИО | кабинет | телефон> — поиск\n"
    "/stats — статистика\n"
    "/export — выгрузка в Excel\n"
    "/on, /off — на смене / не на смене\n"
    "/client <№> name|location <значение> — исправить данные пользователя\n"
    "/staff — специалисты (только админ)\n"
    "То же самое — кнопками меню под полем ввода.\n\n"
    "Ответ пользователю: просто напишите сообщение (текст, фото, файл, голосовое) — "
    "оно уйдёт в активную заявку. Чтобы ответить в другую заявку, нажмите «Ответить» "
    "на её сообщение или выберите её в /my."
)


class SearchForm(StatesGroup):
    query = State()


def _cname(c: Client) -> str:
    return c.full_name or c.wa_name or "+" + c.phone


# ---------- доступ ----------


@router.message(CommandStart())
async def cmd_start(message: Message, s, settings: Settings):
    u = message.from_user
    st = await staff_svc.register(
        s,
        tg_user_id=u.id,
        chat_id=message.chat.id,
        name=u.full_name,
        username=u.username,
        admin_ids=settings.admin_ids,
    )
    if st.status == "active":
        await message.answer(f"Вы в системе, {st.name}.\n\n{HELP}", reply_markup=menu.main_menu(st))
    elif st.status == "pending":
        await message.answer("Запрос на доступ отправлен администратору. Ожидайте.")
    else:
        await message.answer("Доступ закрыт.")


@router.message(Command("help"), IsActive())
async def cmd_help(message: Message, staff: Staff):
    await message.answer(HELP, reply_markup=menu.main_menu(staff))


@router.message(F.text == menu.CANCEL, IsActive())
async def menu_cancel(message: Message, staff: Staff, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=menu.main_menu(staff))


@router.message(Command("on"), IsActive())
@router.message(F.text == menu.SHIFT_ON, IsActive())
async def cmd_on(message: Message, s, staff: Staff):
    n = await staff_svc.set_shift(s, staff, True)
    await message.answer(
        "Вы уже на смене." if n is None else f"Вы на смене. Заявок в очереди: {n}.",
        reply_markup=menu.main_menu(staff),
    )


@router.message(Command("off"), IsActive())
@router.message(F.text == menu.SHIFT_OFF, IsActive())
async def cmd_off(message: Message, s, staff: Staff):
    n = await staff_svc.set_shift(s, staff, False)
    await message.answer(
        "Вы уже не на смене."
        if n is None
        else "Вы не на смене: новые заявки приходить не будут. Заявки в работе остаются за вами.",
        reply_markup=menu.main_menu(staff),
    )


# ---------- работа с заявками ----------


@router.message(Command("my"), IsActive())
@router.message(F.text == menu.MY, IsActive())
async def cmd_my(message: Message, s, staff: Staff):
    rows = await tickets.my_tickets(s, staff)
    if not rows:
        await message.answer("У вас нет заявок в работе.")
        return
    lines = [
        f"{'▶️ ' if t.id == staff.active_ticket_id else ''}#{t.id} · {_cname(c)}" for t, c in rows
    ]
    buttons = [[(f"Перейти к #{t.id}", f"goto:{t.id}")] for t, _ in rows]
    await message.answer("Ваши заявки в работе:\n" + "\n".join(lines), reply_markup=markup(buttons))


@router.callback_query(F.data.regexp(_ID_DATA.format("goto")), IsActive())
async def cb_goto(cq: CallbackQuery, s, staff: Staff):
    t = await tickets.set_active(s, staff, _arg_id(cq))
    if t is None:
        await cq.answer("Заявка не у вас или закрыта", show_alert=True)
        return
    sent = await cq.message.answer(await tickets.recent_view(s, t))
    await tickets.link(s, staff.tg_chat_id, sent.message_id, t.id)
    await cq.answer(f"Активная: #{t.id}")


@router.callback_query(F.data.regexp(_ID_DATA.format("take")), IsActive())
async def cb_take(cq: CallbackQuery, s, staff: Staff):
    r = await tickets.take(s, _arg_id(cq), staff)
    if r.ok:
        await cq.answer("Заявка ваша ✅")
    else:
        text = f"Уже взял {r.holder_name}" if r.holder_name else "Заявка уже недоступна"
        await cq.answer(text, show_alert=True)


async def _drop_buttons(cq: CallbackQuery) -> None:
    try:
        await cq.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.regexp(_ID_DATA.format("close")), IsActive())
async def cb_close(cq: CallbackQuery, s, staff: Staff):
    tid = _arg_id(cq)
    if not await tickets.close_ticket(s, tid, staff):
        await cq.answer("Заявка уже не у вас или закрыта", show_alert=True)
        return
    await _drop_buttons(cq)
    await cq.message.answer(f"✔️ Заявка #{tid} закрыта.")
    await cq.answer()


@router.callback_query(F.data.regexp(_ID_DATA.format("ret")), IsActive())
async def cb_return(cq: CallbackQuery, s, staff: Staff):
    tid = _arg_id(cq)
    if not await tickets.return_ticket(s, tid, staff):
        await cq.answer("Заявка уже не у вас или закрыта", show_alert=True)
        return
    await _drop_buttons(cq)
    await cq.message.answer(f"↩️ Заявка #{tid} возвращена в очередь.")
    await cq.answer()


# ---------- поиск, статистика, выгрузка ----------


@router.message(Command("open"), IsActive())
@router.message(F.text == menu.OPEN, IsActive())
async def cmd_open(message: Message, s):
    await message.answer(await stats.open_tickets_text(s))


async def _send_details(message: Message, s, staff: Staff, tid: int) -> None:
    text = await stats.ticket_details(s, tid)
    if text is None:
        await message.answer("Заявка не найдена.")
        return
    t = await s.get(Ticket, tid)
    buttons = [[("💬 Показать переписку", f"chat:{tid}")]]
    if t.status == "new":
        buttons += tickets.QUEUE_BUTTONS(tid)
    if t.status == "in_progress" and (staff.is_admin or t.assignee_id == staff.id):
        buttons += tickets.WORK_BUTTONS(tid)
    sent = await message.answer(text, reply_markup=markup(buttons))
    if t.status == "in_progress" and t.assignee_id == staff.id:
        await tickets.link(s, staff.tg_chat_id, sent.message_id, t.id)


@router.message(Command("find"), IsActive())
async def cmd_find(message: Message, command: CommandObject, s, staff: Staff):
    q = (command.args or "").strip()
    if not q:
        await message.answer("Использование: /find 125 | ФИО | кабинет | телефон")
        return
    await _find(message, s, staff, q)


@router.message(F.text == menu.SEARCH, IsActive())
async def menu_search(message: Message, state: FSMContext):
    await state.set_state(SearchForm.query)
    await message.answer(
        "Введите номер заявки, ФИО, кабинет или телефон:", reply_markup=menu.cancel_menu()
    )


@router.message(SearchForm.query, IsActive(), F.text.func(menu.is_client_reply_text))
async def search_query(message: Message, s, staff: Staff, state: FSMContext):
    await state.clear()
    await message.answer("Ищу…", reply_markup=menu.main_menu(staff))
    await _find(message, s, staff, message.text.strip())


async def _find(message: Message, s, staff: Staff, q: str) -> None:
    tid = stats.parse_ticket_id(q)
    if tid is not None:
        await _send_details(message, s, staff, tid)
        return
    rows = await stats.search(s, q)
    if not rows:
        await message.answer("Ничего не найдено.")
        return
    lines = [stats.search_line(t, c, name) for t, c, name in rows]
    await message.answer("\n".join(lines) + "\n\nПодробнее: /find <номер>")


@router.callback_query(F.data.regexp(_ID_DATA.format("chat")), IsActive())
async def cb_chat(cq: CallbackQuery, s, bot: Bot):
    conv = await stats.conversation(s, _arg_id(cq))
    if conv is None:
        await cq.answer("Заявка не найдена", show_alert=True)
        return
    await cq.answer()
    tid = _arg_id(cq)
    for chunk in conv.chunks:
        sent = await cq.message.answer(chunk)
        await tickets.link(s, cq.message.chat.id, sent.message_id, tid)
    sender = AiogramSender(bot)
    for path, kind, caption in conv.files:
        if os.path.exists(path):
            await sender.send(
                cq.message.chat.id, text=caption, file_path=path, file_kind=kind, buttons=None
            )
        else:
            await cq.message.answer(f"[файл не найден: {os.path.basename(path)}]")


@router.message(Command("stats"), IsActive())
@router.message(F.text == menu.STATS, IsActive())
async def cmd_stats(message: Message):
    kb = markup([[("Сегодня", "stats:today"), ("Неделя", "stats:week"), ("Месяц", "stats:month")]])
    await message.answer("Статистика за период:", reply_markup=kb)


@router.callback_query(F.data.in_({"stats:today", "stats:week", "stats:month"}), IsActive())
async def cb_stats(cq: CallbackQuery, s):
    kind = cq.data.split(":", 1)[1]
    start, end = stats.period_bounds(kind, clock.utcnow())
    result = await stats.compute(s, start, end)
    await cq.message.answer(stats.render_stats(result, stats.PERIOD_TITLES[kind]))
    await cq.answer()


@router.message(Command("export"), IsActive())
@router.message(F.text == menu.EXPORT, IsActive())
async def cmd_export(message: Message):
    kb = markup(
        [
            [("Этот месяц", "export:month"), ("Прошлый месяц", "export:prev_month")],
            [("Всё время", "export:all")],
        ]
    )
    await message.answer("Выгрузка заявок в Excel:", reply_markup=kb)


@router.callback_query(F.data.in_({"export:month", "export:prev_month", "export:all"}), IsActive())
async def cb_export(cq: CallbackQuery, s):
    kind = cq.data.split(":", 1)[1]
    start, end = stats.period_bounds(kind, clock.utcnow())
    data = await export.export_xlsx(s, start, end)
    name = f"zayavki_{kind}_{clock.local(clock.utcnow()):%Y-%m-%d}.xlsx"
    await cq.message.answer_document(
        BufferedInputFile(data, filename=name), caption=f"Заявки: {stats.PERIOD_TITLES[kind]}"
    )
    await cq.answer()


@router.message(Command("client"), IsActive())
async def cmd_client(message: Message, command: CommandObject, s):
    parsed = parse_client_args(command.args)
    if parsed is None:
        await message.answer("Использование: /client <№ заявки> name|location <значение>")
        return
    tid, field, value = parsed
    t = await s.get(Ticket, tid)
    if t is None:
        await message.answer("Заявка не найдена.")
        return
    c = await s.get(Client, t.client_id)
    setattr(c, field, value)
    await message.answer(f"Сохранено (заявка #{tid}):\n{render.client_line(c)}")


# ---------- администрирование ----------


@router.message(Command("staff"), IsAdmin())
@router.message(F.text == menu.STAFF, IsAdmin())
async def cmd_staff(message: Message, s, staff: Staff):
    for st in await staff_svc.list_staff(s):
        await message.answer(
            staff_svc.staff_line(st), reply_markup=markup(staff_svc.staff_buttons(st, staff))
        )


@router.callback_query(F.data.regexp(_ID_DATA.format("approve")), IsAdmin())
async def cb_approve(cq: CallbackQuery, s, staff: Staff):
    st = await staff_svc.approve(s, staff, _arg_id(cq))
    if st is None:
        await cq.answer("Уже обработано", show_alert=True)
        return
    await cq.message.edit_text(f"✅ {st.name} — доступ открыт")
    await cq.answer()


@router.callback_query(F.data.regexp(_ID_DATA.format("reject")), IsAdmin())
async def cb_reject(cq: CallbackQuery, s, staff: Staff):
    st = await staff_svc.reject(s, staff, _arg_id(cq))
    if st is None:
        await cq.answer("Уже обработано", show_alert=True)
        return
    await cq.message.edit_text(f"❌ {st.name} — доступ отклонён")
    await cq.answer()


_STAFF_ACTIONS = {
    "stf_dis": staff_svc.disable,
    "stf_en": staff_svc.enable,
    "stf_adm": staff_svc.toggle_admin,
    "stf_role": staff_svc.toggle_role,
}


@router.callback_query(F.data.regexp(r"^stf_(dis|en|adm|role):\d{1,9}$"), IsAdmin())
async def cb_staff_action(cq: CallbackQuery, s, staff: Staff):
    action, sid = cq.data.split(":")
    if not await _STAFF_ACTIONS[action](s, staff, int(sid)):
        await cq.answer("Это действие недоступно", show_alert=True)
        return
    st = await s.get(Staff, int(sid))
    await cq.message.edit_text(
        staff_svc.staff_line(st), reply_markup=markup(staff_svc.staff_buttons(st, staff))
    )
    await cq.answer("Готово")


# ---------- ответы пользователю ----------


async def _reply_to_client(message: Message, s, staff: Staff, bot: Bot, settings: Settings):
    reply_to = message.reply_to_message.message_id if message.reply_to_message else None
    t, err = await tickets.resolve_target(s, staff, reply_to)
    if t is None:
        await message.answer(err)
        return
    try:
        content = await extract_content(bot, message, settings.files_dir)
    except FileTooBig:
        await message.answer("Файл больше 20 МБ — бот не может его скачать. Отправьте ссылкой.")
        return
    await tickets.send_staff_reply(
        s, staff, t, content.kind, content.text, content.file_path, content.file_name
    )
    await tickets.link(s, staff.tg_chat_id, message.message_id, t.id)
    c = await s.get(Client, t.client_id)
    await message.answer(f"→ #{t.id} {_cname(c)} ✓")


@router.message(IsActive(), F.text.func(menu.is_client_reply_text))
async def staff_text(message: Message, s, staff: Staff, bot: Bot, settings: Settings):
    await _reply_to_client(message, s, staff, bot, settings)


@router.message(IsActive(), F.photo | F.document | F.voice | F.audio | F.video | F.video_note)
async def staff_media(message: Message, s, staff: Staff, bot: Bot, settings: Settings):
    await _reply_to_client(message, s, staff, bot, settings)


# ---------- всё остальное ----------


@router.message()
async def fallback(message: Message, staff: Staff | None = None):
    if staff is None:
        await message.answer("Напишите /start, чтобы запросить доступ.")
    elif staff.status == "pending":
        await message.answer("Ожидайте одобрения администратора.")
    elif staff.status == "disabled":
        await message.answer("Доступ закрыт.")
    else:
        await message.answer("Не понял команду. /help — список команд.")


@router.callback_query()
async def cb_fallback(cq: CallbackQuery):
    await cq.answer("Нет доступа или действие устарело", show_alert=True)


def setup(dp: Dispatcher, sf) -> None:
    mw = DbMiddleware(sf)
    for r in (task_handlers.router, router):
        r.message.outer_middleware(mw)
        r.callback_query.outer_middleware(mw)
    dp.include_router(task_handlers.router)  # раньше общего: у общего есть catch-all
    dp.include_router(router)
