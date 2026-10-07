from pathlib import Path


class FakeWa:
    def __init__(self, fail_chats=None):
        self.sent: list[tuple] = []
        self.fail_chats: set[str] = set(fail_chats or ())

    async def send_text(self, chat_id, text):
        if chat_id in self.fail_chats:
            raise RuntimeError("wa down")
        self.sent.append(("text", chat_id, text))
        return f"wa{len(self.sent)}"

    async def send_file(self, chat_id, path, file_name, caption):
        if chat_id in self.fail_chats:
            raise RuntimeError("wa down")
        self.sent.append(("file", chat_id, path, file_name, caption))
        return f"wa{len(self.sent)}"


class FakeTg:
    def __init__(self):
        self.sent: list[dict] = []
        self.edited: list[tuple] = []
        self.deleted: list[tuple] = []
        self.fail_send = False
        self.fail_delete = False
        self.permanent_fail_texts: set[str] = set()
        self._mid = 1000

    async def send(self, chat_id, *, text, file_path, file_kind, buttons):
        if self.fail_send:
            raise RuntimeError("tg down")
        if text in self.permanent_fail_texts:
            from helpdesk.delivery.worker import PermanentSendError

            raise PermanentSendError("Forbidden: bot was blocked by the user")
        self._mid += 1
        self.sent.append(
            dict(
                chat_id=chat_id,
                text=text,
                file_path=file_path,
                file_kind=file_kind,
                buttons=buttons,
                mid=self._mid,
            )
        )
        return self._mid

    async def edit(self, chat_id, message_id, text, buttons):
        self.edited.append((chat_id, message_id, text, buttons))

    async def delete(self, chat_id, message_id):
        if self.fail_delete:
            raise RuntimeError("message can't be deleted")
        self.deleted.append((chat_id, message_id))


class FakeGreen:
    def __init__(self, items=()):
        self.items = list(items)  # [(receipt_id, body), ...]
        self.deleted: list[int] = []
        self.fail_download = False

    async def receive(self, timeout=20):
        return self.items[0] if self.items else None

    async def delete(self, receipt_id):
        self.deleted.append(receipt_id)
        self.items = [i for i in self.items if i[0] != receipt_id]

    async def download(self, url, dest):
        if self.fail_download:
            raise RuntimeError("link expired")
        Path(dest).write_bytes(b"img")
