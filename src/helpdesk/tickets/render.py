from helpdesk import clock
from helpdesk.db.models import Client, Message, Ticket

KIND_LABEL = {
    "image": "🖼 фото",
    "document": "📎 файл",
    "audio": "🎤 голосовое",
    "video": "🎬 видео",
}


def clip(text: str, limit: int = 3500) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


def client_line(c: Client) -> str:
    name = c.full_name or c.wa_name or "без имени"
    return f"👤 {name} · {c.location or '—'}\n📞 +{c.phone}"


def message_line(m: Message) -> str:
    parts = []
    if m.kind in KIND_LABEL:
        parts.append(f"[{KIND_LABEL[m.kind]}]")
    if m.text:
        parts.append(m.text)
    return " ".join(parts) or "[пусто]"


def queue_card(t: Ticket, c: Client, msgs: list[Message], waited_min: int | None = None) -> str:
    if waited_min:
        head = f"⏰ Заявка #{t.id} ждёт {waited_min} мин"
    else:
        head = f"🆕 Заявка #{t.id} · {clock.local(t.created_at):%H:%M}"
    body = "\n".join(f"«{message_line(m)}»" for m in msgs if m.direction == "in")
    return f"{head}\n{client_line(c)}\n\n{clip(body)}"


def history(msgs: list[Message], names: dict[int, str]) -> str:
    lines = []
    for m in msgs:
        who = "👤" if m.direction == "in" else f"🛠 {names.get(m.staff_id, '?')}"
        lines.append(f"{clock.local(m.created_at):%d.%m %H:%M} {who}: {message_line(m)}")
    return "\n".join(lines)


def working_card(t: Ticket, c: Client, msgs: list[Message], names: dict[int, str]) -> str:
    return f"✅ Заявка #{t.id} у вас в работе\n{client_line(c)}\n\n{clip(history(msgs, names))}"
