from pathlib import Path

from sqlalchemy import func, select

from helpdesk.db.models import Client, Message
from helpdesk.whatsapp import poller
from tests.factories import make_client
from tests.fakes import FakeGreen


def _note(mid, text="Привет", type_message="textMessage", **file):
    md = {"typeMessage": type_message}
    if type_message == "textMessage":
        md["textMessageData"] = {"textMessage": text}
    else:
        md["fileMessageData"] = {"caption": text, **file}
    return {
        "typeWebhook": "incomingMessageReceived",
        "idMessage": mid,
        "senderData": {"chatId": "77011234567@c.us", "senderName": "Aigerim"},
        "messageData": md,
    }


async def _count(sf, model):
    async with sf() as s:
        return await s.scalar(select(func.count()).select_from(model))


async def test_empty_queue(sf, tmp_path):
    assert await poller.poll_once(sf, FakeGreen(), str(tmp_path), {}) is False


async def test_poller_deletes_after_commit(sf, clock_, tmp_path):
    green = FakeGreen([(1, _note("M1")), (2, _note("M1"))])  # второе — повтор того же сообщения
    assert await poller.poll_once(sf, green, str(tmp_path), {})
    assert green.deleted == [1] and await _count(sf, Message) == 1
    assert await poller.poll_once(sf, green, str(tmp_path), {})
    assert green.deleted == [1, 2] and await _count(sf, Message) == 1


async def test_ignored_notification_is_deleted(sf, clock_, tmp_path):
    green = FakeGreen([(5, {"typeWebhook": "outgoingMessageReceived"})])
    await poller.poll_once(sf, green, str(tmp_path), {})
    assert green.deleted == [5] and await _count(sf, Client) == 0


async def test_failing_handler_is_retried_then_dropped(sf, clock_, tmp_path, monkeypatch):
    async def boom(s, msg, file_path):
        raise RuntimeError("db down")

    monkeypatch.setattr("helpdesk.inbound.handle_incoming", boom)
    green = FakeGreen([(9, _note("M9"))])
    failures: dict[int, int] = {}
    for _ in range(poller.MAX_FAILURES - 1):
        await poller.poll_once(sf, green, str(tmp_path), failures, retry_sleep=0)
        assert green.deleted == []
    await poller.poll_once(sf, green, str(tmp_path), failures, retry_sleep=0)
    assert green.deleted == [9] and failures == {}


async def test_download_success_stores_file(sf, clock_, tmp_path):
    async with sf() as s, s.begin():
        await make_client(s)
    note = _note("F1", "скрин", "imageMessage", downloadUrl="https://cdn/x", fileName="x.jpg")
    await poller.poll_once(sf, FakeGreen([(1, note)]), str(tmp_path), {})
    async with sf() as s:
        m = await s.scalar(select(Message))
        assert m.kind == "image" and Path(m.file_path).read_bytes() == b"img"
        assert str(tmp_path) in m.file_path


async def test_download_failure_marks_text(sf, clock_, tmp_path):
    async with sf() as s, s.begin():
        await make_client(s)
    note = _note("F2", "скрин", "imageMessage", downloadUrl="https://cdn/x", fileName="x.jpg")
    green = FakeGreen([(1, note)])
    green.fail_download = True
    await poller.poll_once(sf, green, str(tmp_path), {})
    async with sf() as s:
        m = await s.scalar(select(Message))
        assert (m.text, m.file_path) == ("скрин [файл не загружен]", None)


async def test_malformed_notification_is_dropped(sf, clock_, tmp_path):
    no_id = _note("X")
    del no_id["idMessage"]
    null_chat = {**_note("Y"), "senderData": {"chatId": None}}
    green = FakeGreen([(1, no_id), (2, null_chat)])
    await poller.poll_once(sf, green, str(tmp_path), {})
    await poller.poll_once(sf, green, str(tmp_path), {})
    assert green.deleted == [1, 2] and await _count(sf, Message) == 0


async def test_parse_crash_is_retried_then_dropped(sf, clock_, tmp_path, monkeypatch):
    def boom(body):
        raise KeyError("idMessage")

    monkeypatch.setattr(poller, "parse_notification", boom)
    green = FakeGreen([(7, _note("M7"))])
    failures: dict[int, int] = {}
    for _ in range(poller.MAX_FAILURES - 1):
        assert await poller.poll_once(sf, green, str(tmp_path), failures, retry_sleep=0)
        assert green.deleted == []
    await poller.poll_once(sf, green, str(tmp_path), failures, retry_sleep=0)
    assert green.deleted == [7] and failures == {}
