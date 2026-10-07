import json

import httpx
import pytest

from helpdesk.whatsapp.client import GreenApiClient


def _client(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GreenApiClient("https://api.test", "https://media.test", "1101", "tok", http=http)


async def test_receive_returns_none_when_queue_empty():
    c = _client(lambda req: httpx.Response(200, text="null"))
    assert await c.receive() is None


async def test_receive_and_delete_urls():
    seen = []

    def h(req):
        seen.append((req.method, req.url.path, dict(req.url.params)))
        if req.method == "GET":
            return httpx.Response(200, json={"receiptId": 7, "body": {"typeWebhook": "x"}})
        return httpx.Response(200, json={"result": True})

    c = _client(h)
    assert await c.receive(timeout=5) == (7, {"typeWebhook": "x"})
    await c.delete(7)
    assert seen == [
        ("GET", "/waInstance1101/receiveNotification/tok", {"receiveTimeout": "5"}),
        ("DELETE", "/waInstance1101/deleteNotification/tok/7", {}),
    ]


async def test_send_text_posts_json():
    def h(req):
        assert req.url.path == "/waInstance1101/sendMessage/tok"
        assert json.loads(req.content) == {"chatId": "1@c.us", "message": "привет"}
        return httpx.Response(200, json={"idMessage": "ABC"})

    assert await _client(h).send_text("1@c.us", "привет") == "ABC"


async def test_send_file_uses_media_host(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF")

    def h(req):
        assert req.url.host == "media.test"
        assert req.url.path == "/waInstance1101/sendFileByUpload/tok"
        assert b'name="chatId"' in req.content and b"%PDF" in req.content
        assert b'name="caption"' in req.content
        return httpx.Response(200, json={"idMessage": "F1"})

    assert await _client(h).send_file("1@c.us", str(f), "a.pdf", "акт") == "F1"


async def test_download_writes_file(tmp_path):
    c = _client(lambda req: httpx.Response(200, content=b"imagebytes"))
    dest = tmp_path / "x.jpg"
    await c.download("https://cdn.test/x", dest)
    assert dest.read_bytes() == b"imagebytes"


async def test_http_error_raises():
    c = _client(lambda req: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await c.send_text("1@c.us", "x")
