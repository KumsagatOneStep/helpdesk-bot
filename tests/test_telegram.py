from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram import Dispatcher
from aiogram.types import InlineKeyboardMarkup

from helpdesk.telegram import handlers
from helpdesk.telegram.gateway import markup
from helpdesk.telegram.media import MAX_BYTES, FileTooBig, extract_content
from helpdesk.telegram.parsing import parse_client_args


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
