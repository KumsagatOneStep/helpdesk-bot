from datetime import datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on

from helpdesk import clock
from helpdesk.db import queries
from helpdesk.db.models import Client, Message, Ticket
from helpdesk.stats.service import STATUS_RU, between

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


def _safe(v: str | None) -> str | None:
    if v is None:
        return None
    v = ILLEGAL_CHARACTERS_RE.sub("", v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


def _naive(dt: datetime | None) -> datetime | None:
    return clock.local(dt).replace(tzinfo=None) if dt else None


def _minutes(a: datetime | None, b: datetime) -> int | None:
    return round((a - b).total_seconds() / 60) if a else None


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
                _naive(t.created_at),
                _safe(c.full_name or c.wa_name),
                _safe(c.location),
                "+" + c.phone,
                _safe(first.get(t.id)),
                names.get(t.assignee_id or t.closed_by_id),
                STATUS_RU[t.status],
                _naive(t.first_taken_at),
                _naive(t.closed_at),
                _minutes(t.first_taken_at, t.created_at),
                _minutes(t.closed_at, t.created_at),
            ]
        )
    for i, width in enumerate(WIDTHS):
        ws.column_dimensions[chr(ord("A") + i)].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, datetime):
                cell.number_format = "DD.MM.YYYY HH:MM"
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
