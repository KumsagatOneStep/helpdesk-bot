from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from helpdesk.delivery.worker import PermanentSendError
from helpdesk.telegram.gateway import AiogramSender


class RecBot:
    def __init__(self, fail: dict | None = None):
        self.calls: list[tuple] = []
        self.fail = fail or {}

    def __getattr__(self, name):
        async def call(chat_id, *args, **kw):
            if name in self.fail:
                raise self.fail[name]
            self.calls.append((name, chat_id, args, kw))
            return SimpleNamespace(message_id=len(self.calls))

        return call


def _big(tmp_path, mb: int) -> str:
    p = tmp_path / "big.bin"
    with open(p, "wb") as f:
        f.truncate(mb * 1024 * 1024 + 1)
    return str(p)


async def test_oversized_file_becomes_placeholder_text(tmp_path):
    bot = RecBot()
    path = _big(tmp_path, 50)
    await AiogramSender(bot).send(
        1, text="#5 · Айгерим", file_path=path, file_kind="document", buttons=None
    )
    [(name, chat, args, kw)] = bot.calls
    assert name == "send_message" and chat == 1
    assert args[0] == "[файл 50 МБ — слишком большой для Telegram]\n#5 · Айгерим"


async def test_big_photo_goes_as_document(tmp_path):
    bot = RecBot()
    await AiogramSender(bot).send(
        1, text=None, file_path=_big(tmp_path, 10), file_kind="photo", buttons=None
    )
    assert [c[0] for c in bot.calls] == ["send_document"]


async def test_rejected_photo_falls_back_to_document(tmp_path):
    p = tmp_path / "x.jpg"
    p.write_bytes(b"img")
    bot = RecBot(fail={"send_photo": TelegramBadRequest(None, "IMAGE_PROCESS_FAILED")})
    await AiogramSender(bot).send(1, text="cap", file_path=str(p), file_kind="photo", buttons=None)
    assert [c[0] for c in bot.calls] == ["send_document"]


@pytest.mark.parametrize(
    "exc",
    [
        TelegramForbiddenError(None, "Forbidden: bot was blocked by the user"),
        TelegramBadRequest(None, "Bad Request: chat not found"),
    ],
)
async def test_permanent_telegram_errors_are_marked(exc):
    bot = RecBot(fail={"send_message": exc})
    with pytest.raises(PermanentSendError):
        await AiogramSender(bot).send(1, text="x", file_path=None, file_kind=None, buttons=None)
