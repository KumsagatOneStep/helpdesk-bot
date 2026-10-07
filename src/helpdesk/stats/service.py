from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select

from helpdesk import clock
from helpdesk.db import queries
from helpdesk.db.models import Client, Ticket, TicketEvent
from helpdesk.tickets import render
from helpdesk.tickets.service import TG_FILE_KIND, ticket_messages

PERIOD_TITLES = {
    "today": "сегодня",
    "week": "эта неделя",
    "month": "этот месяц",
    "prev_month": "прошлый месяц",
    "all": "всё время",
}
STATUS_RU = {"new": "в очереди", "in_progress": "в работе", "closed": "закрыта"}
EVENT_RU = {
    "created": "создана",
    "taken": "взял",
    "returned": "вернул в очередь",
    "closed": "закрыл",
    "delivery_failed": "⚠️ сбой доставки",
}


def period_bounds(kind: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    if kind == "all":
        return None, None
    day = clock.local(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "today":
        return day, None
    if kind == "week":
        return day - timedelta(days=day.weekday()), None
    month = day.replace(day=1)
    if kind == "month":
        return month, None
    if kind == "prev_month":
        return (month - timedelta(days=1)).replace(day=1), month
    raise ValueError(kind)


def between(col, start, end) -> list:
    conds = []
    if start is not None:
        conds.append(col >= start)
    if end is not None:
        conds.append(col < end)
    return conds


def _minutes(a, b):
    return func.extract("epoch", a - b) / 60


def _f(x) -> float | None:
    return float(x) if x is not None else None


def _name(c: Client) -> str:
    return c.full_name or c.wa_name or "+" + c.phone


@dataclass
class StaffStat:
    name: str
    taken: int
    closed: int
    avg_resolve_min: float | None


@dataclass
class Stats:
    created: int
    closed: int
    in_progress: int
    queued: int
    avg_reaction_min: float | None
    avg_resolve_min: float | None
    per_staff: list[StaffStat]
    top_locations: list[tuple[str, int]]


async def _count(s, *conds) -> int:
    return await s.scalar(select(func.count()).select_from(Ticket).where(*conds))


async def compute(s, start, end) -> Stats:
    created_in = between(Ticket.created_at, start, end)
    closed_in = [Ticket.status == "closed", *between(Ticket.closed_at, start, end)]
    avg_reaction = await s.scalar(
        select(func.avg(_minutes(Ticket.first_taken_at, Ticket.created_at))).where(
            Ticket.first_taken_at.is_not(None), *created_in
        )
    )
    avg_resolve = await s.scalar(
        select(func.avg(_minutes(Ticket.closed_at, Ticket.created_at))).where(*closed_in)
    )
    names = await queries.staff_names(s)
    taken_q = (
        select(TicketEvent.staff_id, func.count())
        .where(TicketEvent.type == "taken", *between(TicketEvent.created_at, start, end))
        .group_by(TicketEvent.staff_id)
    )
    taken = dict((await s.execute(taken_q)).all())
    closed_q = (
        select(
            Ticket.closed_by_id,
            func.count(),
            func.avg(_minutes(Ticket.closed_at, Ticket.created_at)),
        )
        .where(*closed_in)
        .group_by(Ticket.closed_by_id)
    )
    closed_by = {sid: (n, _f(avg)) for sid, n, avg in (await s.execute(closed_q)).all()}
    ids = [i for i in set(taken) | set(closed_by) if i is not None]
    ids.sort(key=lambda i: (-closed_by.get(i, (0, None))[0], -taken.get(i, 0), i))
    per_staff = [
        StaffStat(
            names.get(i, "?"),
            taken.get(i, 0),
            closed_by.get(i, (0, None))[0],
            closed_by.get(i, (0, None))[1],
        )
        for i in ids
    ]
    loc_q = (
        select(Client.location, func.count(Ticket.id))
        .select_from(Ticket)
        .join(Client, Client.id == Ticket.client_id)
        .where(*created_in)
        .group_by(Client.location)
        .order_by(func.count(Ticket.id).desc(), Client.location)
        .limit(5)
    )
    top = [(loc or "—", n) for loc, n in (await s.execute(loc_q)).all()]
    return Stats(
        created=await _count(s, *created_in),
        closed=await _count(s, *closed_in),
        in_progress=await _count(s, Ticket.status == "in_progress"),
        queued=await _count(s, Ticket.status == "new"),
        avg_reaction_min=_f(avg_reaction),
        avg_resolve_min=_f(avg_resolve),
        per_staff=per_staff,
        top_locations=top,
    )


def fmt_min(x: float | None) -> str:
    return "—" if x is None else f"{round(x)} мин"


def render_stats(st: Stats, title: str) -> str:
    lines = [
        f"📊 Статистика: {title}",
        f"Создано: {st.created} · Закрыто: {st.closed}",
        f"Сейчас в работе: {st.in_progress} · в очереди: {st.queued}",
        f"Ср. время реакции: {fmt_min(st.avg_reaction_min)} · решения: {fmt_min(st.avg_resolve_min)}",
    ]
    if st.per_staff:
        lines += ["", "По специалистам:"]
        lines += [
            f"• {p.name} — взял {p.taken}, закрыл {p.closed}, ср. {fmt_min(p.avg_resolve_min)}"
            for p in st.per_staff
        ]
    if st.top_locations:
        lines += ["", "Топ отделов:"] + [f"• {loc} — {n}" for loc, n in st.top_locations]
    return "\n".join(lines)


def parse_ticket_id(q: str) -> int | None:
    q = q.strip().lstrip("#")
    return int(q) if q.isdigit() and len(q) <= 6 else None


async def search(s, q: str, limit: int = 10) -> list[tuple[Ticket, Client, str | None]]:
    like = f"%{q.strip()}%"
    conds = [Client.full_name.ilike(like), Client.location.ilike(like), Client.wa_name.ilike(like)]
    digits = "".join(ch for ch in q if ch.isdigit())
    if len(digits) >= 4:
        conds.append(Client.phone.like(f"%{digits}%"))
    query = (
        select(Ticket, Client)
        .join(Client, Client.id == Ticket.client_id)
        .where(or_(*conds))
        .order_by(Ticket.id.desc())
        .limit(limit)
    )
    names = await queries.staff_names(s)
    return [(t, c, names.get(t.assignee_id)) for t, c in (await s.execute(query)).all()]


def search_line(t: Ticket, c: Client, assignee: str | None) -> str:
    who = f" · {assignee}" if assignee else ""
    return f"#{t.id} · {clock.local(t.created_at):%d.%m} · {_name(c)} · {STATUS_RU[t.status]}{who}"


async def ticket_details(s, ticket_id: int) -> str | None:
    t = await s.get(Ticket, ticket_id)
    if t is None:
        return None
    c = await s.get(Client, t.client_id)
    names = await queries.staff_names(s)
    head = f"📄 Заявка #{t.id} — {STATUS_RU[t.status]}"
    if t.assignee_id:
        head += f" · {names.get(t.assignee_id, '?')}"
    lines = [head, render.client_line(c), "", "Ход исполнения:"]
    q = select(TicketEvent).where(TicketEvent.ticket_id == t.id).order_by(TicketEvent.id)
    for e in (await s.scalars(q)).all():
        who = f" {names.get(e.staff_id, '?')}" if e.staff_id else ""
        lines.append(f"{clock.local(e.created_at):%d.%m %H:%M} {EVENT_RU.get(e.type, e.type)}{who}")
    return "\n".join(lines)


@dataclass
class Conversation:
    chunks: list[str]
    files: list[tuple[str, str, str | None]]


async def conversation(s, ticket_id: int) -> Conversation | None:
    t = await s.get(Ticket, ticket_id)
    if t is None:
        return None
    msgs = await ticket_messages(s, t.id)
    body = render.history(msgs, await queries.staff_names(s)) or "(пусто)"
    text = f"💬 Переписка по заявке #{t.id}\n\n{body}"
    chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
    files = [(m.file_path, TG_FILE_KIND[m.kind], m.text) for m in msgs if m.file_path]
    return Conversation(chunks, files)


async def open_tickets_text(s) -> str:
    q = (
        select(Ticket, Client)
        .join(Client, Client.id == Ticket.client_id)
        .where(Ticket.status != "closed")
        .order_by(Ticket.id)
    )
    rows = (await s.execute(q)).all()
    if not rows:
        return "Открытых заявок нет 🎉"
    names = await queries.staff_names(s)
    now = clock.utcnow()
    lines = ["Открытые заявки:"]
    for t, c in rows:
        mins = int((now - t.created_at).total_seconds() // 60)
        who = names.get(t.assignee_id, "?") if t.assignee_id else "в очереди"
        lines.append(f"#{t.id} · {_name(c)} · {who} · {mins} мин")
    return "\n".join(lines)[:4000]
