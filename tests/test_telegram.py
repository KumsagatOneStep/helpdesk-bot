from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram import Dispatcher
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select, update

from helpdesk.db.models import Message as DbMessage
from helpdesk.db.models import Outbox, Task, TaskComment, TgLink
from helpdesk.tasks import service as tasks_svc
from helpdesk.telegram import handlers
from helpdesk.telegram.gateway import markup
from helpdesk.telegram.media import MAX_BYTES, FileTooBig, extract_content
from helpdesk.telegram.parsing import parse_client_args
from helpdesk.tickets import service as tickets
from tests.factories import make_client, make_staff, make_task, make_ticket
from tests.fakes import FakeTranscriber


def test_parse_client_args():
    assert parse_client_args("125 name Иванов Иван") == (125, "full_name", "Иванов Иван")
    assert parse_client_args("#7 location Склад, каб. 3") == (7, "location", "Склад, каб. 3")
    assert parse_client_args(None) is None
    assert parse_client_args("125 phone 123") is None
    assert parse_client_args("abc name X") is None
    assert parse_client_args("125 name") is None
    assert parse_client_args("125 name " + "x" * 201) is None


def test_markup():
    assert markup(None) is None
    kb = markup([[("✋ Взять", "take:1")], [("A", "a"), ("B", "b")]])
    assert isinstance(kb, InlineKeyboardMarkup)
    assert [[b.callback_data for b in row] for row in kb.inline_keyboard] == [
        ["take:1"],
        ["a", "b"],
    ]
    # формат из payload outbox (списки вместо кортежей) тоже принимается
    assert markup([[["x", "y"]]]).inline_keyboard[0][0].text == "x"


def _msg(**kw):
    base = dict(
        text=None,
        caption=None,
        photo=None,
        document=None,
        voice=None,
        audio=None,
        video=None,
        video_note=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class FakeBot:
    def __init__(self):
        self.downloaded = []

    async def download(self, obj, destination):
        Path(destination).write_bytes(b"data")
        self.downloaded.append(obj)


async def test_extract_text(tmp_path, clock_):
    c = await extract_content(FakeBot(), _msg(text="Иду"), str(tmp_path))
    assert (c.kind, c.text, c.file_path, c.file_name) == ("text", "Иду", None, None)


async def test_extract_photo_takes_largest_size(tmp_path, clock_):
    small = SimpleNamespace(file_unique_id="s", file_size=10)
    big = SimpleNamespace(file_unique_id="b", file_size=1000)
    bot = FakeBot()
    c = await extract_content(bot, _msg(photo=[small, big], caption="скрин"), str(tmp_path))
    assert (c.kind, c.text, c.file_name) == ("image", "скрин", "photo_b.jpg")
    assert bot.downloaded == [big] and Path(c.file_path).read_bytes() == b"data"


async def test_extract_voice_and_document(tmp_path, clock_):
    v = await extract_content(FakeBot(), _msg(voice=SimpleNamespace(file_size=5)), str(tmp_path))
    assert (v.kind, v.file_name) == ("audio", "voice.ogg")
    doc = SimpleNamespace(file_size=5, file_name="акт.pdf")
    d = await extract_content(FakeBot(), _msg(document=doc), str(tmp_path))
    assert (d.kind, d.file_name) == ("document", "акт.pdf")


async def test_extract_too_big(tmp_path, clock_):
    doc = SimpleNamespace(file_size=MAX_BYTES + 1, file_name="big.zip")
    with pytest.raises(FileTooBig):
        await extract_content(FakeBot(), _msg(document=doc), str(tmp_path))


def test_router_setup_registers_handlers():
    dp = Dispatcher()
    handlers.setup(dp, sf=None)
    assert len(handlers.router.message.handlers) >= 14
    assert len(handlers.router.callback_query.handlers) >= 10
    assert ("my", "Мои заявки в работе") in handlers.BOT_COMMANDS


def _tg_message(chat_type: str):
    from aiogram.types import Message

    return Message.model_validate(
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": -100 if chat_type != "private" else 5, "type": chat_type},
            "from": {"id": 5, "is_bot": False, "first_name": "Ерлан"},
            "text": "/start",
        }
    )


@pytest.mark.parametrize("chat_type,ok", [("private", True), ("group", False)])
async def test_router_only_handles_private_chats(chat_type, ok):
    from aiogram.types import CallbackQuery

    msg = _tg_message(chat_type)
    passed, _ = await handlers.router.message._handler.check(msg)
    assert passed is ok
    cq = CallbackQuery.model_validate(
        {
            "id": "1",
            "from": {"id": 5, "is_bot": False, "first_name": "Ерлан"},
            "chat_instance": "x",
            "data": "take:1",
            "message": msg.model_dump(by_alias=True),
        }
    )
    passed, _ = await handlers.router.callback_query._handler.check(cq)
    assert passed is ok


class _Sent:
    _next = 500

    def __init__(self):
        _Sent._next += 1
        self.message_id = _Sent._next


class FakeChatMessage:
    def __init__(self, chat_id, **kw):
        self.chat = SimpleNamespace(id=chat_id)
        self.answers = []
        self.message_id = kw.pop("message_id", 900)
        self.reply_to_message = kw.pop("reply_to_message", None)
        self.__dict__.update(_msg(**kw).__dict__)

    async def answer(self, text, **kw):
        self.answers.append(text)
        return _Sent()


async def test_conversation_chunks_are_linked_to_ticket(s, clock_):
    c = await make_client(s)
    st = await make_staff(s, tg_id=5, name="Ерлан")
    t = await make_ticket(s, c, status="in_progress", assignee=st)
    s.add(DbMessage(client_id=c.id, ticket_id=t.id, direction="in", kind="text", text="Помогите"))
    await s.flush()
    msg = FakeChatMessage(5)

    async def _ans(*a, **kw):
        return None

    cq = SimpleNamespace(data=f"chat:{t.id}", message=msg, answer=_ans)
    await handlers.cb_chat(cq, s, bot=None)
    assert msg.answers
    links = (await s.execute(TgLink.__table__.select())).all()
    assert links and all(r.ticket_id == t.id for r in links)
    assert (await tickets.resolve_target(s, st, _Sent._next))[0] == t


async def test_staff_reply_is_linked_so_replying_to_it_hits_same_ticket(s, clock_, tmp_path):
    c1 = await make_client(s)
    c2 = await make_client(s, phone="77017654321", full_name="Бауыржан")
    st = await make_staff(s, tg_id=6, name="Ерлан")
    t1 = await make_ticket(s, c1, status="in_progress", assignee=st)
    t2 = await make_ticket(s, c2, status="in_progress", assignee=st)
    st.active_ticket_id = t1.id
    await s.flush()
    msg = FakeChatMessage(6, text="Иду", message_id=4242)
    settings = SimpleNamespace(files_dir=str(tmp_path))
    await handlers._reply_to_client(msg, s, st, FakeBot(), settings)
    st.active_ticket_id = t2.id
    await s.flush()
    assert (await tickets.resolve_target(s, st, 4242))[0] == t1


def _cq(data: str):
    from aiogram.types import CallbackQuery

    return CallbackQuery.model_validate(
        {
            "id": "1",
            "from": {"id": 5, "is_bot": False, "first_name": "Ерлан"},
            "chat_instance": "x",
            "data": data,
            "message": _tg_message("private").model_dump(by_alias=True),
        }
    )


async def _matched_handler(data: str) -> str:
    admin = SimpleNamespace(status="active", is_admin=True)
    for h in handlers.router.callback_query.handlers:
        passed, _ = await h.check(_cq(data), staff=admin)
        if passed:
            return h.callback.__name__
    return ""


@pytest.mark.parametrize(
    "data",
    ["take:abc", "take:", "close:1:2", "chat:99999999999999999999", "approve:-1", "goto:1e3"],
)
async def test_malformed_callback_data_goes_to_fallback(data):
    assert await _matched_handler(data) == "cb_fallback"


@pytest.mark.parametrize(
    "data,name",
    [("take:12", "cb_take"), ("chat:7", "cb_chat"), ("approve:3", "cb_approve")],
)
async def test_wellformed_callback_data_reaches_handler(data, name):
    assert await _matched_handler(data) == name


def test_engine_hides_sql_parameters_in_errors():
    from helpdesk.db.session import make_engine

    eng = make_engine("postgresql+asyncpg://u:p@localhost:1/x")
    assert eng.sync_engine.hide_parameters is True


async def test_reply_to_task_card_is_comment_never_whatsapp(s, clock_, tmp_path):
    c = await make_client(s)
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    tima = await make_staff(s, tg_id=7, name="Тимур")
    ticket = await make_ticket(s, c, status="in_progress", assignee=erlan)
    erlan.active_ticket_id = ticket.id
    task = await make_task(s, tima)
    await tasks_svc.link(s, 6, 3333, task.id)
    await s.flush()
    msg = FakeChatMessage(
        6, text="Сейчас подойду", message_id=4343, reply_to_message=SimpleNamespace(message_id=3333)
    )
    await handlers._reply_to_client(
        msg, s, erlan, FakeBot(), SimpleNamespace(files_dir=str(tmp_path))
    )
    assert msg.answers == [f"💬 → З-{task.id} ✓"]
    assert (await s.scalars(select(Outbox).where(Outbox.channel == "wa"))).all() == []
    [cm] = (await s.scalars(select(TaskComment))).all()
    assert (cm.task_id, cm.text) == (task.id, "Сейчас подойду")
    assert (await tasks_svc.task_for_reply(s, 6, 4343)).id == task.id


async def test_reply_to_closed_or_hidden_task_is_refused(s, clock_, tmp_path):
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    coord = await make_staff(s, tg_id=8, name="Айжан", role="coordinator")
    closed = await make_task(s, erlan, status="closed")
    hidden = await make_task(s, coord, kind="coordinator")
    await tasks_svc.link(s, 6, 1, closed.id)
    await tasks_svc.link(s, 6, 2, hidden.id)
    settings = SimpleNamespace(files_dir=str(tmp_path))
    m1 = FakeChatMessage(6, text="a", reply_to_message=SimpleNamespace(message_id=1))
    await handlers._reply_to_client(m1, s, erlan, FakeBot(), settings)
    assert m1.answers == [f"З-{closed.id} уже закрыта — комментарий не отправлен."]
    m2 = FakeChatMessage(6, text="b", reply_to_message=SimpleNamespace(message_id=2))
    await handlers._reply_to_client(m2, s, erlan, FakeBot(), settings)
    assert m2.answers == ["Задача недоступна."]
    assert (await s.scalars(select(TaskComment))).all() == []


async def test_my_lists_tickets_and_tasks(s, clock_):
    from datetime import date

    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    mine = await make_task(s, erlan, due_date=date(2026, 10, 6), description="Картридж")
    queued = await make_task(s, erlan, status="queued", assignee=None, description="Мышь")
    msg = FakeChatMessage(6)
    await handlers.cmd_my(msg, s, erlan)
    assert msg.answers == [
        f"Мои задачи:\n⚠️ З-{mine.id} · до 06.10 · Картридж\n\n"
        f"Ждут исполнителя:\nЗ-{queued.id} · без срока · Мышь"
    ]
    empty = FakeChatMessage(7)
    other = await make_staff(s, tg_id=7, name="Тимур", role="coordinator")
    queued.status = "closed"
    await s.flush()
    await handlers.cmd_my(empty, s, other)
    assert empty.answers == ["У вас нет заявок и задач в работе."]


async def test_find_task_by_number_and_text(s, clock_):
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    tima = await make_staff(s, tg_id=7, name="Тимур")
    t = await make_task(s, erlan, description="Ноутбук не включается")
    m = FakeChatMessage(7)
    await handlers._find(m, s, tima, f"з-{t.id}")
    assert m.answers[0].startswith(f"📌 З-{t.id} · Helpdesk")
    assert (await tasks_svc.task_for_reply(s, 7, _Sent._next)).id == t.id
    m2 = FakeChatMessage(7)
    await handlers._find(m2, s, tima, "ноутбук")
    assert m2.answers == [
        f"Задачи:\nЗ-{t.id} · 07.10 · Ноутбук не включается · в работе · Ерлан\n\n"
        "Подробнее: /find З-<номер>"
    ]
    m3 = FakeChatMessage(7)
    await handlers._find(m3, s, tima, "З-999")
    assert m3.answers == ["Задача не найдена."]


async def test_open_lists_visible_tasks(s, clock_):
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    t = await make_task(s, erlan, description="Картридж")
    msg = FakeChatMessage(6)
    await handlers.cmd_open(msg, s, erlan)
    assert msg.answers == [
        f"Открытых заявок нет 🎉\n\nОткрытые задачи:\nЗ-{t.id} · Helpdesk · Ерлан · Картридж"
    ]


@pytest.mark.parametrize(
    "data,name",
    [
        ("tk_take:12", "cb_task_take"),
        ("tk_help:3", "cb_task_help"),
        ("tk_close:4", "cb_task_close"),
    ],
)
async def test_task_callbacks_reach_handlers(data, name):
    from helpdesk.telegram import task_handlers

    admin = SimpleNamespace(status="active", is_admin=True)
    found = ""
    for h in task_handlers.router.callback_query.handlers:
        passed, _ = await h.check(_cq(data), staff=admin)
        if passed:
            found = h.callback.__name__
            break
    assert found == name


@pytest.mark.parametrize("data", ["tk_take:abc", "tk_close:1:2", "tk_help:99999999999"])
async def test_malformed_task_callbacks_not_handled_by_task_router(data):
    from helpdesk.telegram import task_handlers

    admin = SimpleNamespace(status="active", is_admin=True)
    for h in task_handlers.router.callback_query.handlers:
        passed, _ = await h.check(_cq(data), staff=admin)
        assert not passed, h.callback.__name__
    assert await _matched_handler(data) == "cb_fallback"


class _EditMsg:
    def __init__(self, chat_id):
        self.chat = SimpleNamespace(id=chat_id)
        self.edits = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))


class _CloseCq:
    def __init__(self, data, chat_id):
        self.data = data
        self.message = _EditMsg(chat_id)
        self.alerts = []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append(text)


def _kb(m):
    return [[(b.text, b.callback_data) for b in row] for row in m.inline_keyboard] if m else None


async def test_my_has_close_button_only_when_something_to_close(s, clock_):
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    await make_task(s, erlan)
    msg = FakeChatMessage(6)
    sent = []

    async def answer(text, reply_markup=None, **kw):
        sent.append(_kb(reply_markup))
        return _Sent()

    msg.answer = answer
    await handlers.cmd_my(msg, s, erlan)
    assert sent[-1] == [[("✅ Закрыть…", "cls_pick")]]
    tima = await make_staff(s, tg_id=7, name="Тимур")
    await make_task(s, erlan, status="queued", assignee=None)
    msg2 = FakeChatMessage(7)
    msg2.answer = answer
    await handlers.cmd_my(msg2, s, tima)  # только «Ждут исполнителя» — закрывать нечего
    assert sent[-1] is None


async def test_close_picker_lists_own_tickets_and_tasks(s, clock_):
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    tima = await make_staff(s, tg_id=7, name="Тимур")
    ticket = await make_ticket(s, await make_client(s), status="in_progress", assignee=erlan)
    t1 = await make_task(s, erlan)
    t2 = await make_task(s, erlan)
    await make_task(s, tima)  # чужая
    await make_task(s, erlan, status="closed")
    cq = _CloseCq("cls_pick", 6)
    await handlers.cb_close_pick(cq, s, erlan)
    text, kb = cq.message.edits[-1]
    assert text == "Что закрыть?"
    assert _kb(kb) == [
        [
            (f"#{ticket.id}", f"cls_tkt:{ticket.id}"),
            (f"З-{t1.id}", f"cls_task:{t1.id}"),
            (f"З-{t2.id}", f"cls_task:{t2.id}"),
        ],
        [("✖️ Отмена", "cls_cancel")],
    ]
    empty = _CloseCq("cls_pick", 7)
    tima_task = (await tasks_svc.my_tasks(s, tima))[0]
    tima_task.status = "closed"
    await s.flush()
    await handlers.cb_close_pick(empty, s, tima)
    assert empty.alerts == ["Закрывать нечего"]


async def test_close_task_via_confirm(s, clock_):
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    tima = await make_staff(s, tg_id=7, name="Тимур")
    t = await make_task(s, erlan, description="Заменить картридж")
    cq = _CloseCq(f"cls_task:{t.id}", 6)
    await handlers.cb_close_ask_task(cq, s, erlan)
    text, kb = cq.message.edits[-1]
    assert text == f"Закрыть З-{t.id} · Заменить картридж?"
    assert _kb(kb) == [[("✅ Да, закрыть", f"cls_yes_task:{t.id}"), ("↩️ Назад", "cls_pick")]]
    foreign = _CloseCq(f"cls_task:{t.id}", 7)
    await handlers.cb_close_ask_task(foreign, s, tima)
    assert foreign.alerts == ["Задача уже не у вас или закрыта"]
    yes = _CloseCq(f"cls_yes_task:{t.id}", 6)
    await handlers.cb_close_yes_task(yes, s, erlan)
    await s.refresh(t)
    assert t.status == "closed" and yes.message.edits[-1] == (f"✔️ З-{t.id} закрыта", None)
    again = _CloseCq(f"cls_yes_task:{t.id}", 6)
    await handlers.cb_close_yes_task(again, s, erlan)
    assert again.alerts == ["Уже закрыта или нет прав"]


async def test_close_ticket_via_confirm_notifies_client(s, clock_):
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    tima = await make_staff(s, tg_id=7, name="Тимур")
    c = await make_client(s)
    ticket = await make_ticket(s, c, status="in_progress", assignee=erlan)
    cq = _CloseCq(f"cls_tkt:{ticket.id}", 6)
    await handlers.cb_close_ask_ticket(cq, s, erlan)
    text, kb = cq.message.edits[-1]
    assert text == (
        f"Закрыть #{ticket.id} · Айгерим Сериккызы?\nПользователю уйдёт «Заявка выполнена»."
    )
    assert _kb(kb) == [[("✅ Да, закрыть", f"cls_yes_tkt:{ticket.id}"), ("↩️ Назад", "cls_pick")]]
    foreign = _CloseCq(f"cls_yes_tkt:{ticket.id}", 7)
    await handlers.cb_close_yes_ticket(foreign, s, tima)
    assert foreign.alerts == ["Заявка уже не у вас или закрыта"]
    yes = _CloseCq(f"cls_yes_tkt:{ticket.id}", 6)
    await handlers.cb_close_yes_ticket(yes, s, erlan)
    await s.refresh(ticket)
    assert ticket.status == "closed"
    assert yes.message.edits[-1] == (f"✔️ Заявка #{ticket.id} закрыта", None)
    wa = (await s.scalars(select(Outbox).where(Outbox.channel == "wa"))).all()
    assert len(wa) == 1


async def test_close_cancel():
    cq = _CloseCq("cls_cancel", 6)
    await handlers.cb_close_cancel(cq)
    assert cq.message.edits[-1] == ("Отменено.", None)


@pytest.mark.parametrize(
    "data,name",
    [
        ("cls_pick", "cb_close_pick"),
        ("cls_task:3", "cb_close_ask_task"),
        ("cls_tkt:3", "cb_close_ask_ticket"),
        ("cls_yes_task:3", "cb_close_yes_task"),
        ("cls_yes_tkt:3", "cb_close_yes_ticket"),
        ("cls_cancel", "cb_close_cancel"),
        ("cls_task:x", "cb_fallback"),
        ("cls_yes_tkt:1:2", "cb_fallback"),
    ],
)
async def test_close_callbacks_routing(data, name):
    assert await _matched_handler(data) == name


class _VoiceChatMessage(FakeChatMessage):
    def __init__(self, chat_id, duration=5, **kw):
        super().__init__(
            chat_id,
            voice=SimpleNamespace(duration=duration, file_size=10, file_unique_id="v"),
            **kw,
        )
        self.edits = []

    async def answer(self, text, **kw):
        self.answers.append(text)
        sent = _Sent()
        outer = self

        async def edit_text(t, reply_markup=None, **k):
            outer.edits.append(t)

        sent.edit_text = edit_text
        return sent


async def test_voice_comment_with_transcript_never_whatsapp(s, clock_, tmp_path):
    c = await make_client(s)
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    await make_staff(s, tg_id=7, name="Тимур")
    ticket = await make_ticket(s, c, status="in_progress", assignee=erlan)
    erlan.active_ticket_id = ticket.id
    task = await make_task(s, erlan)
    await tasks_svc.link(s, 6, 3333, task.id)
    await s.flush()
    msg = _VoiceChatMessage(6, message_id=4444, reply_to_message=SimpleNamespace(message_id=3333))
    settings = SimpleNamespace(files_dir=str(tmp_path), stt_max_seconds=120)
    await handlers._reply_to_client(msg, s, erlan, FakeBot(), settings, FakeTranscriber("Заменил"))
    assert msg.answers == ["⏳ Распознаю…"]
    assert msg.edits == [f"💬 → З-{task.id} ✓\n🎤 Заменил"]
    [cm] = (await s.scalars(select(TaskComment))).all()
    assert (cm.kind, cm.text) == ("audio", "Заменил")
    tg = [o.payload for o in (await s.scalars(select(Outbox))).all()]
    assert [o["text"] for o in tg if o.get("file_kind") == "voice"] == [
        f"💬 З-{task.id} · Ерлан (🎤): Заменил"
    ]
    assert (await s.scalars(select(Outbox).where(Outbox.channel == "wa"))).all() == []


async def test_voice_comment_too_long_sent_as_audio(s, clock_, tmp_path):
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    await make_staff(s, tg_id=7, name="Тимур")
    task = await make_task(s, erlan)
    await tasks_svc.link(s, 6, 3333, task.id)
    msg = _VoiceChatMessage(6, duration=500, reply_to_message=SimpleNamespace(message_id=3333))
    stt = FakeTranscriber("не должен вызываться")
    settings = SimpleNamespace(files_dir=str(tmp_path), stt_max_seconds=120)
    await handlers._reply_to_client(msg, s, erlan, FakeBot(), settings, stt)
    assert stt.calls == [] and msg.answers == [f"💬 → З-{task.id} ✓"]
    [cm] = (await s.scalars(select(TaskComment))).all()
    assert (cm.kind, cm.text) == ("audio", None)


class _ClosingTranscriber(FakeTranscriber):
    """Пока «распознаём», задачу закрывают другим запросом."""

    def __init__(self, s, task_id):
        super().__init__("Заменил")
        self.s, self.task_id = s, task_id

    async def transcribe(self, path):
        await self.s.execute(
            update(Task)
            .where(Task.id == self.task_id)
            .values(status="closed")
            .execution_options(synchronize_session=False)
        )
        return await super().transcribe(path)


async def test_task_closed_during_recognition_no_comment(s, clock_, tmp_path):
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    await make_staff(s, tg_id=7, name="Тимур")
    task = await make_task(s, erlan)
    await tasks_svc.link(s, 6, 3333, task.id)
    await s.flush()
    msg = _VoiceChatMessage(6, reply_to_message=SimpleNamespace(message_id=3333))
    settings = SimpleNamespace(files_dir=str(tmp_path), stt_max_seconds=120)
    await handlers._reply_to_client(
        msg, s, erlan, FakeBot(), settings, _ClosingTranscriber(s, task.id)
    )
    assert msg.edits == [f"З-{task.id} уже закрыта — комментарий не отправлен."]
    assert (await s.scalars(select(TaskComment))).all() == []
    assert (await s.scalars(select(Outbox))).all() == []


async def test_voice_comment_long_transcript_echo_capped(s, clock_, tmp_path):
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    await make_staff(s, tg_id=7, name="Тимур")
    task = await make_task(s, erlan)
    await tasks_svc.link(s, 6, 3333, task.id)
    msg = _VoiceChatMessage(6, reply_to_message=SimpleNamespace(message_id=3333))
    settings = SimpleNamespace(files_dir=str(tmp_path), stt_max_seconds=120)
    await handlers._reply_to_client(msg, s, erlan, FakeBot(), settings, FakeTranscriber("я" * 5000))
    [edit] = msg.edits
    assert len(edit) <= 4096 and edit.startswith(f"💬 → З-{task.id} ✓\n🎤 я")
    [cm] = (await s.scalars(select(TaskComment))).all()
    assert cm.text == "я" * 5000
