import re
from datetime import date, timedelta

from helpdesk import clock
from helpdesk.db.models import Staff, Task, TaskComment
from helpdesk.delivery.outbox import Buttons
from helpdesk.tasks import rules
from helpdesk.tickets.render import KIND_LABEL

KIND_RU = {"helpdesk": "Helpdesk", "coordinator": "🧭 Координатор"}
KIND_PLAIN = {"helpdesk": "Helpdesk", "coordinator": "Координатор"}
STATUS_RU = {"queued": "ждёт исполнителя", "in_progress": "в работе", "closed": "закрыта"}
MAX_DESCRIPTION = 2000
MAX_REQUESTER = 200
DUE_DAYS = {"today": 0, "tomorrow": 1, "week": 7}

_TASK_ID = re.compile(r"^\s*[ЗзZz]\s*-?\s*(\d{1,6})\s*$")
_DATE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s*$")


def number(t: Task) -> str:
    return f"З-{t.id}"


def parse_task_id(q: str) -> int | None:
    m = _TASK_ID.match(q)
    return int(m.group(1)) if m else None


def parse_due(text: str, today: date) -> date | None:
    m = _DATE.match(text)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        if y:
            res = date(int(y), mo, d)
        else:
            res = date(today.year, mo, d)
            if res < today:
                res = date(today.year + 1, mo, d)
    except ValueError:
        return None
    return res if res >= today else None


def due_from_choice(code: str, today: date) -> date:
    return today + timedelta(days=DUE_DAYS[code])


def is_overdue(t: Task, today: date) -> bool:
    return t.status != "closed" and t.due_date is not None and t.due_date < today


def short(text: str, limit: int = 100) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def card(t: Task, names: dict[int, str], today: date) -> str:
    head = f"📌 {number(t)} · {KIND_RU[t.kind]}"
    if t.due_date:
        mark = "⚠️ " if is_overdue(t, today) else ""
        head += f" · {mark}срок {t.due_date:%d.%m}"
    lines = [head]
    if t.requester:
        lines.append(f"Для: {t.requester}")
    lines.append(t.description)
    who = names.get(t.assignee_id, "?") if t.assignee_id else "— (ищем)"
    lines.append(f"Создал: {names.get(t.creator_id, '?')} · Исполнитель: {who}")
    if t.status == "closed":
        lines.append(f"✔️ Закрыта: {names.get(t.closed_by_id, '?')}")
    return "\n".join(lines)


def buttons_for(t: Task, viewer: Staff, *, can_help: bool) -> Buttons | None:
    if t.status == "closed":
        return None
    close = ("✅ Закрыть", f"tk_close:{t.id}")
    if t.status == "queued":
        rows = []
        if rules.can_do(viewer, t.kind):
            label = "✅ Принять" if t.taken_at is None else "✋ Взять"
            rows.append([(label, f"tk_take:{t.id}")])
        if viewer.id == t.creator_id:
            rows.append([close])
        return rows or None
    if viewer.id == t.assignee_id:
        row = [close]
        if can_help:
            row.append(("🆘 Нужна помощь", f"tk_help:{t.id}"))
        return [row]
    if viewer.id == t.creator_id:
        return [[close]]
    return None


def summary(data: dict) -> str:
    lines = [
        "Проверьте задачу:",
        f"Тип: {KIND_RU[data['kind']]}",
        f"Описание: {data['description']}",
    ]
    if data.get("requester"):
        lines.append(f"Для: {data['requester']}")
    due = data.get("due")
    lines.append(f"Срок: {date.fromisoformat(due):%d.%m.%Y}" if due else "Срок: без срока")
    if data.get("file_name"):
        lines.append(f"Вложение: {data['file_name']}")
    return "\n".join(lines)


def list_line(t: Task, today: date) -> str:
    mark = "⚠️ " if is_overdue(t, today) else ""
    due = f"до {t.due_date:%d.%m}" if t.due_date else "без срока"
    return f"{mark}{number(t)} · {due} · {short(t.description, 50)}"


def open_line(t: Task, names: dict[int, str], today: date) -> str:
    mark = "⚠️ " if is_overdue(t, today) else ""
    who = names.get(t.assignee_id, "?") if t.assignee_id else "ждёт исполнителя"
    return f"{mark}{number(t)} · {KIND_PLAIN[t.kind]} · {who} · {short(t.description, 50)}"


def search_line(t: Task, names: dict[int, str]) -> str:
    who = f" · {names.get(t.assignee_id, '?')}" if t.assignee_id else ""
    return (
        f"{number(t)} · {clock.local(t.created_at):%d.%m} · {short(t.description, 50)}"
        f" · {STATUS_RU[t.status]}{who}"
    )


def _comment_line(c: TaskComment, names: dict[int, str]) -> str:
    parts = [f"[{KIND_LABEL[c.kind]}]"] if c.kind in KIND_LABEL else []
    if c.text:
        parts.append(c.text)
    body = " ".join(parts) or "[пусто]"
    return f"{clock.local(c.created_at):%d.%m %H:%M} {names.get(c.staff_id, '?')}: {body}"


def details(t: Task, names: dict[int, str], comments: list[TaskComment], today: date) -> str:
    text = card(t, names, today)
    if comments:
        text += "\n\nКомментарии:\n" + "\n".join(_comment_line(c, names) for c in comments)
    return text[-4000:] if len(text) > 4000 else text
