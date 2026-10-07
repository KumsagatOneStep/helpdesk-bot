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
        self._mid = 1000

    async def send(self, chat_id, *, text, file_path, file_kind, buttons):
        if self.fail_send:
            raise RuntimeError("tg down")
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
