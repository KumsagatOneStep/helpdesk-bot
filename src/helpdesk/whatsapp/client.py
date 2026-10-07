from pathlib import Path

import httpx


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
        r = await self._http.get(
            f"{self._base}/receiveNotification/{self._token}",
            params={"receiveTimeout": timeout},
            timeout=timeout + 10,
        )
        r.raise_for_status()
        if not r.content.strip():  # пустая очередь: Green API отвечает пустым телом
            return None
        data = r.json()
        if not data:
            return None
        return data["receiptId"], data["body"]

    async def delete(self, receipt_id: int) -> None:
        r = await self._http.delete(f"{self._base}/deleteNotification/{self._token}/{receipt_id}")
        r.raise_for_status()

    async def send_text(self, chat_id: str, text: str) -> str:
        r = await self._http.post(
            f"{self._base}/sendMessage/{self._token}", json={"chatId": chat_id, "message": text}
        )
        r.raise_for_status()
        return r.json()["idMessage"]

    async def send_file(self, chat_id: str, path: str, file_name: str, caption: str | None) -> str:
        content = Path(path).read_bytes()
        data = {"chatId": chat_id, "fileName": file_name}
        if caption:
            data["caption"] = caption
        r = await self._http.post(
            f"{self._media}/sendFileByUpload/{self._token}",
            data=data,
            files={"file": (file_name, content)},
        )
        r.raise_for_status()
        return r.json()["idMessage"]

    async def download(self, url: str, dest: Path) -> None:
        async with self._http.stream("GET", url) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)

    async def aclose(self) -> None:
        await self._http.aclose()
