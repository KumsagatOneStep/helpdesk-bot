from contextlib import asynccontextmanager
from pathlib import Path

import httpx


class GreenApiError(Exception):
    """Ошибка Green API без URL: в пути запроса лежит токен, он не должен попасть в логи/БД."""

    def __init__(self, method: str, status: int | None = None, detail: str = ""):
        self.method = method
        self.status = status
        msg = f"Green API {method} failed"
        if status is not None:
            msg += f": HTTP {status}"
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)


@asynccontextmanager
async def _guard(method: str):
    # `from None`: исходное исключение httpx (с URL в тексте) не должно попасть в traceback
    try:
        yield
    except httpx.HTTPStatusError as e:
        raise GreenApiError(method, e.response.status_code) from None
    except httpx.HTTPError as e:
        raise GreenApiError(method, detail=type(e).__name__) from None


def _check(r: httpx.Response, method: str) -> None:
    if r.is_error:
        raise GreenApiError(method, r.status_code)


class GreenApiClient:
    def __init__(
        self,
        api_url: str,
        media_url: str,
        id_instance: str,
        token: str,
        http: httpx.AsyncClient | None = None,
    ):
        self._base = f"{api_url.rstrip('/')}/waInstance{id_instance}"
        self._media = f"{media_url.rstrip('/')}/waInstance{id_instance}"
        self._token = token
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(60))

    async def receive(self, timeout: int = 20) -> tuple[int, dict] | None:
        async with _guard("receiveNotification"):
            r = await self._http.get(
                f"{self._base}/receiveNotification/{self._token}",
                params={"receiveTimeout": timeout},
                timeout=timeout + 10,
            )
        _check(r, "receiveNotification")
        if not r.content.strip():  # пустая очередь: Green API отвечает пустым телом
            return None
        data = r.json()
        if not data:
            return None
        return data["receiptId"], data["body"]

    async def delete(self, receipt_id: int) -> None:
        async with _guard("deleteNotification"):
            r = await self._http.delete(
                f"{self._base}/deleteNotification/{self._token}/{receipt_id}"
            )
        _check(r, "deleteNotification")

    async def send_text(self, chat_id: str, text: str) -> str:
        async with _guard("sendMessage"):
            r = await self._http.post(
                f"{self._base}/sendMessage/{self._token}",
                json={"chatId": chat_id, "message": text},
            )
        _check(r, "sendMessage")
        return r.json()["idMessage"]

    async def send_file(self, chat_id: str, path: str, file_name: str, caption: str | None) -> str:
        content = Path(path).read_bytes()
        data = {"chatId": chat_id, "fileName": file_name}
        if caption:
            data["caption"] = caption
        async with _guard("sendFileByUpload"):
            r = await self._http.post(
                f"{self._media}/sendFileByUpload/{self._token}",
                data=data,
                files={"file": (file_name, content)},
            )
        _check(r, "sendFileByUpload")
        return r.json()["idMessage"]

    async def download(self, url: str, dest: Path) -> None:
        async with _guard("download"), self._http.stream("GET", url) as r:
            _check(r, "download")
            with open(dest, "wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)

    async def aclose(self) -> None:
        await self._http.aclose()
