from datetime import date, datetime
from io import BytesIO

from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on

from helpdesk import clock
from helpdesk.db import queries
from helpdesk.db.models import Client, Message, Task, Ticket
from helpdesk.stats.service import STATUS_RU, between
from helpdesk.tasks.render import KIND_PLAIN
from helpdesk.tasks.render import STATUS_RU as TASK_STATUS_RU
from helpdesk.xlsx_util import layout, naive, safe

HEADERS = [
    "№",
    "Создана",
    "ФИО",
    "Отдел/кабинет",
    "Телефон",
    "Первое сообщение",
    "Исполнитель",
    "Статус",
    "Взята",
    "Закрыта",
    "Реакция, мин",
    "Решение, мин",
]
WIDTHS = [6, 17, 28, 24, 15, 50, 16, 11, 17, 17, 13, 13]
TASK_HEADERS = [
    "№",
    "Тип",
    "Статус",
    "Описание",
    "Для кого",
    "Создал",
    "Исполнитель",
    "Срок",
    "Создана",
    "Закрыта",
    "С опозданием",
]
TASK_WIDTHS = [8, 14, 16, 50, 24, 16, 16, 11, 17, 17, 13]


def _minutes(a: datetime | None, b: datetime) -> int | None:
    return round((a - b).total_seconds() / 60) if a else None


def _late(t: Task, today: date) -> str | None:
    if t.due_date is None:
        return None
    day = clock.local(t.closed_at).date() if t.closed_at else today
    return "да" if day > t.due_date else "нет"


async def export_xlsx(s, start, end) -> bytes:
    q = (
        select(Ticket, Client)
        .join(Client, Client.id == Ticket.client_id)
        .where(*between(Ticket.created_at, start, end))
        .order_by(Ticket.id)
    )
    rows = (await s.execute(q)).all()
    ids = [t.id for t, _ in rows]
    first: dict[int, str] = {}
    if ids:
        fq = (
            select(Message.ticket_id, Message.text, Message.kind)
            .ext(distinct_on(Message.ticket_id))
            .where(Message.ticket_id.in_(ids), Message.direction == "in")
            .order_by(Message.ticket_id, Message.id)
        )
        first = {tid: text or f"[{kind}]" for tid, text, kind in (await s.execute(fq)).all()}
    names = await queries.staff_names(s)

    wb = Workbook()
    ws = wb.active
    ws.title = "Заявки"
    ws.append(HEADERS)
    for t, c in rows:
        ws.append(
            [
                t.id,
                naive(t.created_at),
                safe(c.full_name or c.wa_name),
                safe(c.location),
                "+" + c.phone,
                safe(first.get(t.id)),
                names.get(t.assignee_id or t.closed_by_id),
                STATUS_RU[t.status],
                naive(t.first_taken_at),
                naive(t.closed_at),
                _minutes(t.first_taken_at, t.created_at),
                _minutes(t.closed_at, t.created_at),
            ]
        )
    layout(ws, WIDTHS)

    tq = select(Task).where(*between(Task.created_at, start, end)).order_by(Task.id)
    today = clock.local(clock.utcnow()).date()
    ws2 = wb.create_sheet("Задачи")
    ws2.append(TASK_HEADERS)
    for t in (await s.scalars(tq)).all():
        ws2.append(
            [
                f"З-{t.id}",
                KIND_PLAIN[t.kind],
                TASK_STATUS_RU[t.status],
                safe(t.description),
                safe(t.requester),
                names.get(t.creator_id),
                names.get(t.assignee_id or t.closed_by_id),
                t.due_date,
                naive(t.created_at),
                naive(t.closed_at),
                _late(t, today),
            ]
        )
    layout(ws2, TASK_WIDTHS)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
