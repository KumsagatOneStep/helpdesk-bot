from helpdesk.db.models import Outbox, TgCard

Buttons = list[list[tuple[str, str]]]


def _add(s, channel: str, chat_key: str, payload: dict) -> None:
    s.add(Outbox(channel=channel, chat_key=chat_key, payload=payload))


def _buttons(buttons: Buttons | None) -> list | None:
    return [[list(b) for b in row] for row in buttons] if buttons else None


def wa_text(s, chat_id: str, text: str, *, ticket_id: int | None = None) -> None:
    payload = {"op": "text", "chat_id": chat_id, "text": text, "ticket_id": ticket_id}
    _add(s, "wa", f"wa:{chat_id}", payload)


def wa_file(s, chat_id, path, file_name, caption=None, *, ticket_id=None) -> None:
    payload = {
        "op": "file",
        "chat_id": chat_id,
        "path": path,
        "file_name": file_name,
        "caption": caption,
        "ticket_id": ticket_id,
    }
    _add(s, "wa", f"wa:{chat_id}", payload)


def tg_send(
    s,
    chat_id: int,
    *,
    text: str | None = None,
    file_path: str | None = None,
    file_kind: str | None = None,
    buttons: Buttons | None = None,
    ticket_id: int | None = None,
    card_staff_id: int | None = None,
    card_seq: int | None = None,
) -> None:
    payload = {
        "op": "send",
        "chat_id": chat_id,
        "text": text,
        "file_path": file_path,
        "file_kind": file_kind,
        "buttons": _buttons(buttons),
        "ticket_id": ticket_id,
        "card_staff_id": card_staff_id,
        "card_seq": card_seq,
    }
    _add(s, "tg", f"tg:{chat_id}", payload)


def tg_edit(s, chat_id: int, message_id: int, text: str, buttons: Buttons | None = None) -> None:
    payload = {
        "op": "edit",
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "buttons": _buttons(buttons),
    }
    _add(s, "tg", f"tg:{chat_id}", payload)


def tg_delete_card(s, card: TgCard, fallback_text: str) -> None:
    card.state = "deleting"
    payload = {"op": "delete_card", "card_id": card.id, "fallback_text": fallback_text}
    _add(s, "tg", f"tg:{card.tg_chat_id}", payload)
