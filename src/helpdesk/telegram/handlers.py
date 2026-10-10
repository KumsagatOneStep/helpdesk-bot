import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from helpdesk import clock
from helpdesk.config import Settings
from helpdesk.db import queries
from helpdesk.db.models import Client, Staff, Task, Ticket
from helpdesk.staff import service as staff_svc
from helpdesk.stats import export
from helpdesk.stats import service as stats
from helpdesk.tasks import render as task_render
from helpdesk.tasks import rules as task_rules
from helpdesk.tasks import service as tasks
from helpdesk.telegram import menu, stock_handlers, task_handlers, voice
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
    "➕ Новая задача — создать внутреннюю задачу (тип, описание, для кого, срок)\n"
    "Комментарий к задаче — «Ответить» на её карточку или сообщение\n"
    "/find З-12 — карточка задачи\n"
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
    mine = await tasks.my_tasks(s, staff)
    waiting = await tasks.queued_for(s, staff)
    if not rows and not mine and not waiting:
        await message.answer("У вас нет заявок и задач в работе.")
        return
    today = tasks.today()
    parts, buttons = [], []
    if rows:
        lines = [
            f"{'▶️ ' if t.id == staff.active_ticket_id else ''}#{t.id} · {_cname(c)}"
            for t, c in rows
        ]
        parts.append("Ваши заявки в работе:\n" + "\n".join(lines))
        buttons = [[(f"Перейти к #{t.id}", f"goto:{t.id}")] for t, _ in rows]
    if rows or mine:
        buttons.append([("✅ Закрыть…", "cls_pick")])
    if mine:
        parts.append("Мои задачи:\n" + "\n".join(task_render.list_line(t, today) for t in mine))
    if waiting:
        parts.append(
            "Ждут исполнителя:\n" + "\n".join(task_render.list_line(t, today) for t in waiting)
        )
    await message.answer("\n\n".join(parts)[:4000], reply_markup=markup(buttons))


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


# ---------- закрытие из «Мои заявки» ----------


def _confirm_buttons(kind: str, item_id: int):
    return [[("✅ Да, закрыть", f"cls_yes_{kind}:{item_id}"), ("↩️ Назад", "cls_pick")]]


@router.callback_query(F.data == "cls_pick", IsActive())
async def cb_close_pick(cq: CallbackQuery, s, staff: Staff):
    items = [(f"#{t.id}", f"cls_tkt:{t.id}") for t, _ in await tickets.my_tickets(s, staff)]
    items += [(task_render.number(t), f"cls_task:{t.id}") for t in await tasks.my_tasks(s, staff)]
    if not items:
        await cq.answer("Закрывать нечего", show_alert=True)
        return
    rows = [items[i : i + 3] for i in range(0, len(items), 3)]
    rows.append([("✖️ Отмена", "cls_cancel")])
    await cq.message.edit_text("Что закрыть?", reply_markup=markup(rows))
    await cq.answer()


@router.callback_query(F.data.regexp(_ID_DATA.format("cls_task")), IsActive())
async def cb_close_ask_task(cq: CallbackQuery, s, staff: Staff):
    t = await s.get(Task, _arg_id(cq))
    if t is None or t.status != "in_progress" or t.assignee_id != staff.id:
        await cq.answer("Задача уже не у вас или закрыта", show_alert=True)
        return
    text = f"Закрыть {task_render.number(t)} · {task_render.short(t.description, 60)}?"
    await cq.message.edit_text(text, reply_markup=markup(_confirm_buttons("task", t.id)))
    await cq.answer()


@router.callback_query(F.data.regexp(_ID_DATA.format("cls_tkt")), IsActive())
async def cb_close_ask_ticket(cq: CallbackQuery, s, staff: Staff):
    t = await s.get(Ticket, _arg_id(cq))
    if t is None or t.status != "in_progress" or t.assignee_id != staff.id:
        await cq.answer("Заявка уже не у вас или закрыта", show_alert=True)
        return
    c = await s.get(Client, t.client_id)
    text = f"Закрыть #{t.id} · {_cname(c)}?\nПользователю уйдёт «Заявка выполнена»."
    await cq.message.edit_text(text, reply_markup=markup(_confirm_buttons("tkt", t.id)))
    await cq.answer()


@router.callback_query(F.data.regexp(_ID_DATA.format("cls_yes_task")), IsActive())
async def cb_close_yes_task(cq: CallbackQuery, s, staff: Staff):
    tid = _arg_id(cq)
    if not await tasks.close(s, tid, staff):
        await cq.answer("Уже закрыта или нет прав", show_alert=True)
        return
    await cq.message.edit_text(f"✔️ З-{tid} закрыта", reply_markup=None)
    await cq.answer()


@router.callback_query(F.data.regexp(_ID_DATA.format("cls_yes_tkt")), IsActive())
async def cb_close_yes_ticket(cq: CallbackQuery, s, staff: Staff):
    tid = _arg_id(cq)
    if not await tickets.close_ticket(s, tid, staff):
        await cq.answer("Заявка уже не у вас или закрыта", show_alert=True)
        return
    await cq.message.edit_text(f"✔️ Заявка #{tid} закрыта", reply_markup=None)
    await cq.answer()


@router.callback_query(F.data == "cls_cancel", IsActive())
async def cb_close_cancel(cq: CallbackQuery):
    await cq.message.edit_text("Отменено.", reply_markup=None)
    await cq.answer()


# ---------- поиск, статистика, выгрузка ----------


@router.message(Command("open"), IsActive())
@router.message(F.text == menu.OPEN, IsActive())
async def cmd_open(message: Message, s, staff: Staff):
    text = await stats.open_tickets_text(s)
    open_tasks = await tasks.open_tasks(s, staff)
    if open_tasks:
        names = await queries.staff_names(s)
        today = tasks.today()
        lines = [task_render.open_line(t, names, today) for t in open_tasks]
        text += "\n\nОткрытые задачи:\n" + "\n".join(lines)
    await message.answer(text[:4000])


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
        await message.answer("Использование: /find 125 | З-12 | ФИО | кабинет | телефон")
        return
    await _find(message, s, staff, q)


@router.message(F.text == menu.SEARCH, IsActive())
async def menu_search(message: Message, state: FSMContext):
    await state.set_state(SearchForm.query)
    await message.answer(
        "Введите номер заявки, З-номер задачи, ФИО, кабинет или телефон:",
        reply_markup=menu.cancel_menu(),
    )


@router.message(SearchForm.query, IsActive(), F.text.func(menu.is_client_reply_text))
async def search_query(message: Message, s, staff: Staff, state: FSMContext):
    await state.clear()
    await message.answer("Ищу…", reply_markup=menu.main_menu(staff))
    await _find(message, s, staff, message.text.strip())


async def _send_task_details(message: Message, s, staff: Staff, task_id: int) -> None:
    t = await tasks.get_visible(s, staff, task_id)
    if t is None:
        await message.answer("Задача не найдена.")
        return
    names = await queries.staff_names(s)
    text = task_render.details(t, names, await tasks.recent_comments(s, t.id), tasks.today())
    help_ok = t.assignee_id == staff.id and await tasks.can_help(s, t, staff)
    buttons = task_render.buttons_for(t, staff, can_help=help_ok) or []
    has_close = any(data.startswith("tk_close:") for row in buttons for _, data in row)
    if staff.is_admin and t.status != "closed" and not has_close:
        buttons = [*buttons, [("✅ Закрыть", f"tk_close:{t.id}")]]
    sent = await message.answer(text, reply_markup=markup(buttons))
    await tasks.link(s, staff.tg_chat_id, sent.message_id, t.id)


async def _find(message: Message, s, staff: Staff, q: str) -> None:
    tid = stats.parse_ticket_id(q)
    if tid is not None:
        await _send_details(message, s, staff, tid)
        return
    task_id = task_render.parse_task_id(q)
    if task_id is not None:
        await _send_task_details(message, s, staff, task_id)
        return
    rows = await stats.search(s, q)
    found = await tasks.search(s, staff, q)
    if not rows and not found:
        await message.answer("Ничего не найдено.")
        return
    parts = []
    if rows:
        lines = [stats.search_line(t, c, name) for t, c, name in rows]
        parts.append("\n".join(lines) + "\n\nПодробнее: /find <номер>")
    if found:
        names = await queries.staff_names(s)
        lines = [task_render.search_line(t, names) for t in found]
        parts.append("Задачи:\n" + "\n".join(lines) + "\n\nПодробнее: /find З-<номер>")
    await message.answer("\n\n".join(parts)[:4000])


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
    text = stats.render_stats(await stats.compute(s, start, end), stats.PERIOD_TITLES[kind])
    ts = await stats.compute_tasks(s, start, end, tasks.today())
    if ts.created or ts.closed or ts.overdue_now:
        text += "\n\n" + stats.render_task_stats(ts)
    await cq.message.answer(text[:4096])
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


async def _comment_task(
    message: Message, s, staff: Staff, bot: Bot, settings: Settings, t, stt=None
) -> None:
    n = task_render.number(t)
    if not task_rules.can_see(staff, t):
        await message.answer("Задача недоступна.")
        return
    if t.status == "closed":
        await message.answer(f"{n} уже закрыта — комментарий не отправлен.")
        return
    is_voice = bool(message.voice or message.audio)
    progress = None
    try:
        if is_voice and stt is not None:
            r = await voice.recognize(message, bot, settings, stt, keep_file=True)
            content, progress = r.content, r.progress
            text = r.text or content.text
        else:
            content = await extract_content(bot, message, settings.files_dir)
            text = content.text
    except FileTooBig:
        await message.answer("Файл больше 20 МБ — бот не может его скачать. Отправьте ссылкой.")
        return
    # распознавание идёт до минуты — задачу могли закрыть, перечитываем
    await s.refresh(t)
    c = await tasks.comment(
        s,
        t,
        staff,
        kind=content.kind,
        text=text,
        file_path=content.file_path,
        file_name=content.file_name,
        voice=is_voice,
    )
    if c is None:
        reply = f"{n} уже закрыта — комментарий не отправлен."
    else:
        await tasks.link(s, staff.tg_chat_id, message.message_id, t.id)
        reply = f"💬 → {n} ✓" + (f"\n🎤 {text}" if is_voice and text else "")
    reply = reply[:4096]  # ответ идёт мимо gateway, который режет до лимита Telegram
    if progress is not None:
        await progress.edit_text(reply)
    else:
        await message.answer(reply)


async def _reply_to_client(
    message: Message, s, staff: Staff, bot: Bot, settings: Settings, stt=None
):
    reply_to = message.reply_to_message.message_id if message.reply_to_message else None
    if reply_to is not None:
        # сначала задачи: ответ на карточку задачи никогда не уходит в WhatsApp
        task = await tasks.task_for_reply(s, staff.tg_chat_id, reply_to)
        if task is not None:
            await _comment_task(message, s, staff, bot, settings, task, stt)
            return
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
async def staff_text(message: Message, s, staff: Staff, bot: Bot, settings: Settings, stt):
    await _reply_to_client(message, s, staff, bot, settings, stt)


@router.message(IsActive(), F.photo | F.document | F.voice | F.audio | F.video | F.video_note)
async def staff_media(message: Message, s, staff: Staff, bot: Bot, settings: Settings, stt):
    await _reply_to_client(message, s, staff, bot, settings, stt)


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
    for r in (task_handlers.router, stock_handlers.router, router):
        r.message.outer_middleware(mw)
        r.callback_query.outer_middleware(mw)
    dp.include_router(task_handlers.router)  # раньше общего: у общего есть catch-all
    dp.include_router(stock_handlers.router)
    dp.include_router(router)
