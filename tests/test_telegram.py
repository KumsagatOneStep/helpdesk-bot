from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram import Dispatcher
from aiogram.types import InlineKeyboardMarkup

from helpdesk.db.models import Message as DbMessage
from helpdesk.db.models import TgLink
from helpdesk.telegram import handlers
from helpdesk.telegram.gateway import markup
from helpdesk.telegram.media import MAX_BYTES, FileTooBig, extract_content
from helpdesk.telegram.parsing import parse_client_args
from helpdesk.tickets import service as tickets
from tests.factories import make_client, make_staff, make_ticket


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
