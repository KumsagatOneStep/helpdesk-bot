import json
import traceback

import httpx
import pytest

from helpdesk.whatsapp.client import GreenApiClient, GreenApiError


def _client(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GreenApiClient("https://api.test", "https://media.test", "1101", "tok", http=http)


async def test_receive_returns_none_when_queue_empty():
    c = _client(lambda req: httpx.Response(200, text="null"))
    assert await c.receive() is None


async def test_receive_returns_none_on_empty_body():
    # Green API отвечает пустым телом 200, когда очередь пуста
    c = _client(lambda req: httpx.Response(200, text=""))
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
    with pytest.raises(GreenApiError):
        await c.send_text("1@c.us", "x")


SECRET = "s3cr3tT0kenValue0123456789"


def _secret_client(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GreenApiClient("https://api.test", "https://media.test", "1101", SECRET, http=http)


def _full_trace(e: BaseException) -> str:
    return "".join(traceback.format_exception(e))


@pytest.mark.parametrize("status", [401, 429, 466, 500])
async def test_status_error_does_not_leak_token(status):
    c = _secret_client(lambda req: httpx.Response(status))
    with pytest.raises(GreenApiError) as ei:
        await c.receive()
    assert ei.value.status == status
    assert SECRET not in str(ei.value)
    assert SECRET not in _full_trace(ei.value)


async def test_transport_error_does_not_leak_token():
    def h(req):
        raise httpx.ConnectError(f"cannot connect to {req.url}", request=req)

    c = _secret_client(h)
    with pytest.raises(GreenApiError) as ei:
        await c.send_text("1@c.us", "x")
    assert SECRET not in str(ei.value)
    assert SECRET not in _full_trace(ei.value)


async def test_download_error_does_not_leak_url(tmp_path):
    c = _secret_client(lambda req: httpx.Response(404))
    with pytest.raises(GreenApiError) as ei:
        await c.download(f"https://cdn.test/{SECRET}/x.jpg", tmp_path / "x.jpg")
    assert SECRET not in _full_trace(ei.value)


async def test_download_over_limit_is_aborted_and_partial_file_removed(tmp_path):
    c = _client(lambda req: httpx.Response(200, content=b"x" * 2000))
    dest = tmp_path / "big.mp4"
    with pytest.raises(GreenApiError):
        await c.download("https://cdn.test/big", dest, max_bytes=1000)
    assert not dest.exists()


async def test_download_rejected_by_content_length_before_writing(tmp_path):
    c = _client(
        lambda req: httpx.Response(200, headers={"Content-Length": "5000"}, content=b"x" * 5000)
    )
    dest = tmp_path / "big.mp4"
    with pytest.raises(GreenApiError):
        await c.download("https://cdn.test/big", dest, max_bytes=1000)
    assert not dest.exists()


async def test_download_failure_removes_partial_file(tmp_path):
    c = _client(lambda req: httpx.Response(500))
    dest = tmp_path / "x.jpg"
    with pytest.raises(GreenApiError):
        await c.download("https://cdn.test/x", dest)
    assert not dest.exists()
