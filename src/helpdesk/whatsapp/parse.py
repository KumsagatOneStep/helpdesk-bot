from dataclasses import dataclass


@dataclass(frozen=True)
class IncomingMessage:
    wa_message_id: str
    chat_id: str
    phone: str
    sender_name: str | None
    kind: str
    text: str | None
    download_url: str | None = None
    file_name: str | None = None


_FILE_KINDS = {
    "imageMessage": "image",
    "documentMessage": "document",
    "audioMessage": "audio",
    "videoMessage": "video",
}
_OTHER_LABELS = {
    "stickerMessage": "[стикер]",
    "locationMessage": "[геолокация]",
    "contactMessage": "[контакт]",
    "contactsArrayMessage": "[контакты]",
    "pollMessage": "[опрос]",
}
_IGNORED = {"reactionMessage", "editedMessage", "deletedMessage"}


def parse_notification(body: dict) -> IncomingMessage | None:
    if body.get("typeWebhook") != "incomingMessageReceived":
        return None
    sender = body.get("senderData") or {}
    chat_id = sender.get("chatId") or ""
    if not isinstance(chat_id, str) or not chat_id.endswith("@c.us"):
        return None
    if not body.get("idMessage"):
        return None
    md = body.get("messageData") or {}
    tm = md.get("typeMessage")
    if tm in _IGNORED:
        return None
    base = dict(
        wa_message_id=body["idMessage"],
        chat_id=chat_id,
        phone=chat_id.split("@")[0],
        sender_name=sender.get("senderName") or None,
    )
    if tm == "textMessage":
        text = (md.get("textMessageData") or {}).get("textMessage", "")
        return IncomingMessage(**base, kind="text", text=text)
    if tm in ("extendedTextMessage", "quotedMessage"):
        text = (md.get("extendedTextMessageData") or {}).get("text", "")
        return IncomingMessage(**base, kind="text", text=text)
    if tm in _FILE_KINDS:
        f = md.get("fileMessageData") or {}
        return IncomingMessage(
            **base,
            kind=_FILE_KINDS[tm],
            text=f.get("caption") or None,
            download_url=f.get("downloadUrl"),
            file_name=f.get("fileName"),
        )
    return IncomingMessage(**base, kind="other", text=_OTHER_LABELS.get(tm, f"[{tm}]"))
