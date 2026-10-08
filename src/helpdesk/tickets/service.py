from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select, update

from helpdesk import clock, texts
from helpdesk.db import queries
from helpdesk.db.models import Client, Message, Staff, TgCard, TgLink, Ticket, TicketEvent
from helpdesk.delivery import outbox
from helpdesk.tickets import render

TG_FILE_KIND = {"image": "photo", "document": "document", "audio": "voice", "video": "video"}


def QUEUE_BUTTONS(tid: int):
    return [[("✋ Взять", f"take:{tid}")]]


def WORK_BUTTONS(tid: int):
    return [[("✅ Закрыть", f"close:{tid}"), ("↩️ Вернуть", f"ret:{tid}")]]


async def open_ticket(s, client_id: int, *, lock: bool = False) -> Ticket | None:
    q = select(Ticket).where(Ticket.client_id == client_id, Ticket.status != "closed")
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    return await s.scalar(q)


async def ticket_messages(s, ticket_id: int) -> list[Message]:
    q = select(Message).where(Message.ticket_id == ticket_id).order_by(Message.id)
    return list((await s.scalars(q)).all())


async def on_client_message(s, client: Client, message: Message | None) -> Ticket:
    # блокировка: take/close/возврат не проскочат между чтением статуса и разводкой сообщения
    t = await open_ticket(s, client.id, lock=True)
    if t is None:
        t = Ticket(client_id=client.id)
        s.add(t)
        await s.flush()
        await s.execute(
            update(Message)
            .where(
                Message.client_id == client.id,
                Message.ticket_id.is_(None),
                Message.intake.is_(False),
            )
            .values(ticket_id=t.id)
            .execution_options(synchronize_session=False)
        )
        if message is not None:
            message.ticket_id = t.id
        s.add(TicketEvent(ticket_id=t.id, type="created"))
        outbox.wa_text(
            s, client.wa_chat_id, texts.t(client.lang, "accepted", id=t.id), ticket_id=t.id
        )
        await broadcast(s, t, client)
        return t
    if message is not None:
        message.ticket_id = t.id
        await s.flush()
    if t.status == "in_progress" and message is not None:
        staff = await s.get(Staff, t.assignee_id)
        _forward(s, staff, t, client, message)
    elif t.status == "new":  # админов уже предупредили при создании/возврате заявки
        await broadcast(s, t, client, warn_if_empty=False)
    return t


def _header(t: Ticket, c: Client) -> str:
    return f"#{t.id} · {c.full_name or c.wa_name or '+' + c.phone}"


def _forward(s, staff: Staff, t: Ticket, c: Client, m: Message) -> None:
    if m.file_path:
        text = f"{_header(t, c)}\n{m.text}" if m.text else _header(t, c)
        outbox.tg_send(
            s,
            staff.tg_chat_id,
            text=text,
            file_path=m.file_path,
            file_kind=TG_FILE_KIND[m.kind],
            ticket_id=t.id,
        )
    else:
        text = f"{_header(t, c)}\n{render.message_line(m)}"
        outbox.tg_send(s, staff.tg_chat_id, text=text, ticket_id=t.id)


async def broadcast(
    s, t: Ticket, client: Client | None = None, *, waited_min=None, warn_if_empty=True
) -> int:
    client = client or await s.get(Client, t.client_id)
    t.broadcast_seq += 1
    t.last_broadcast_at = clock.utcnow()
    await _delete_open_cards(s, t, f"#{t.id} — карточка обновлена")
    recipients = await queries.on_shift_staff(s)
    if not recipients:
        if warn_if_empty:
            for a in await queries.active_admins(s):
                text = f"⚠️ Заявка #{t.id}: на смене никого нет"
                outbox.tg_send(s, a.tg_chat_id, text=text, ticket_id=t.id)
        return 0
    msgs = await ticket_messages(s, t.id)
    for st in recipients:
        _send_cards(s, t, client, msgs, st, waited_min=waited_min)
    return len(recipients)


def _send_cards(s, t, client, msgs, st: Staff, waited_min=None) -> None:
    for m in msgs:
        if m.direction == "in" and m.file_path:
            outbox.tg_send(
                s,
                st.tg_chat_id,
                text=m.text,
                file_path=m.file_path,
                file_kind=TG_FILE_KIND[m.kind],
                ticket_id=t.id,
                card_staff_id=st.id,
                card_seq=t.broadcast_seq,
            )
    outbox.tg_send(
        s,
        st.tg_chat_id,
        text=render.queue_card(t, client, msgs, waited_min),
        buttons=QUEUE_BUTTONS(t.id),
        ticket_id=t.id,
        card_staff_id=st.id,
        card_seq=t.broadcast_seq,
    )


async def _delete_open_cards(s, t: Ticket, fallback: str, keep_staff_id: int | None = None):
    q = select(TgCard).where(TgCard.ticket_id == t.id, TgCard.state == "open")
    for card in (await s.scalars(q)).all():
        if card.staff_id != keep_staff_id:
            outbox.tg_delete_card(s, card, fallback)


@dataclass
class TakeResult:
    ok: bool
    holder_name: str | None


async def _working_text(s, t: Ticket, client: Client) -> str:
    msgs = await ticket_messages(s, t.id)
    return render.working_card(t, client, msgs, await queries.staff_names(s))


async def take(s, ticket_id: int, staff: Staff) -> TakeResult:
    now = clock.utcnow()
    res = await s.execute(
        update(Ticket)
        .where(Ticket.id == ticket_id, Ticket.status == "new")
        .values(
            status="in_progress",
            assignee_id=staff.id,
            first_taken_at=func.coalesce(Ticket.first_taken_at, now),
        )
        .returning(Ticket.id)
        .execution_options(synchronize_session=False)
    )
    claimed = res.scalar_one_or_none()
    t = await s.get(Ticket, ticket_id, populate_existing=True)
    if claimed is None:
        holder = await s.get(Staff, t.assignee_id) if t and t.assignee_id else None
        mine = select(TgCard).where(
            TgCard.ticket_id == ticket_id, TgCard.staff_id == staff.id, TgCard.state == "open"
        )
        for card in (await s.scalars(mine)).all():
            outbox.tg_delete_card(s, card, f"🔒 #{ticket_id} уже взята")
        return TakeResult(False, holder.name if holder else None)

    client = await s.get(Client, t.client_id)
    s.add(TicketEvent(ticket_id=t.id, type="taken", staff_id=staff.id))
    staff.active_ticket_id = t.id
    working = await _working_text(s, t, client)
    cards = (
        await s.scalars(select(TgCard).where(TgCard.ticket_id == t.id, TgCard.state == "open"))
    ).all()
    edited = False
    for card in cards:
        if card.staff_id != staff.id:
            outbox.tg_delete_card(s, card, f"🔒 #{t.id} взял {staff.name}")
            continue
        card.state = "kept"
        if card.has_button:
            outbox.tg_edit(s, card.tg_chat_id, card.tg_message_id, working, WORK_BUTTONS(t.id))
            edited = True
    if not edited:
        outbox.tg_send(
            s, staff.tg_chat_id, text=working, buttons=WORK_BUTTONS(t.id), ticket_id=t.id
        )
    outbox.wa_text(
        s,
        client.wa_chat_id,
        texts.t(client.lang, "taken", id=t.id, name=staff.name),
        ticket_id=t.id,
    )
    return TakeResult(True, staff.name)


async def _owned_in_progress(s, ticket_id: int, staff: Staff) -> Ticket | None:
    t = await s.get(Ticket, ticket_id, with_for_update=True, populate_existing=True)
    if t is None or t.status != "in_progress":
        return None
    if t.assignee_id != staff.id and not staff.is_admin:
        return None
    return t


async def _release(s, t: Ticket) -> None:
    prev = await s.get(Staff, t.assignee_id) if t.assignee_id else None
    if prev is not None and prev.active_ticket_id == t.id:
        prev.active_ticket_id = None


async def return_ticket(s, ticket_id: int, staff: Staff) -> bool:
    t = await _owned_in_progress(s, ticket_id, staff)
    if t is None:
        return False
    await _release(s, t)
    t.status = "new"
    t.assignee_id = None
    s.add(TicketEvent(ticket_id=t.id, type="returned", staff_id=staff.id))
    await s.flush()
    await broadcast(s, t)
    return True


async def close_ticket(s, ticket_id: int, staff: Staff) -> bool:
    t = await _owned_in_progress(s, ticket_id, staff)
    if t is None:
        return False
    await _release(s, t)
    t.status = "closed"
    t.closed_at = clock.utcnow()
    t.closed_by_id = staff.id
    s.add(TicketEvent(ticket_id=t.id, type="closed", staff_id=staff.id))
    client = await s.get(Client, t.client_id)
    outbox.wa_text(s, client.wa_chat_id, texts.t(client.lang, "closed", id=t.id), ticket_id=t.id)
    await s.flush()
    return True


def _is_mine(t: Ticket | None, staff: Staff) -> bool:
    return t is not None and t.status == "in_progress" and t.assignee_id == staff.id


async def resolve_target(s, staff: Staff, reply_to: int | None) -> tuple[Ticket | None, str]:
    if reply_to is not None:
        link = await s.get(TgLink, (staff.tg_chat_id, reply_to))
        if link is not None:
            t = await s.get(Ticket, link.ticket_id)
            if _is_mine(t, staff):
                return t, ""
            return None, f"Заявка #{link.ticket_id} закрыта или не у вас — сообщение не отправлено."
        # reply на сообщение без привязки не отправляем в активную: можно ответить не тому клиенту
        return None, (
            "Не понятно, к какой заявке относится ответ — сообщение не отправлено. "
            "Ответьте на карточку заявки или отправьте без reply в активную."
        )
    if staff.active_ticket_id:
        t = await s.get(Ticket, staff.active_ticket_id)
        if _is_mine(t, staff):
            return t, ""
    return None, "Нет активной заявки — сообщение не отправлено. /my — ваши заявки."


async def send_staff_reply(
    s,
    staff: Staff,
    t: Ticket,
    kind: str,
    text: str | None,
    file_path: str | None,
    file_name: str | None,
) -> Message:
    client = await s.get(Client, t.client_id)
    m = Message(
        client_id=client.id,
        ticket_id=t.id,
        direction="out",
        staff_id=staff.id,
        kind=kind,
        text=text,
        file_path=file_path,
        file_name=file_name,
    )
    s.add(m)
    if file_path:
        name = file_name or Path(file_path).name
        outbox.wa_file(s, client.wa_chat_id, file_path, name, text, ticket_id=t.id)
    else:
        outbox.wa_text(s, client.wa_chat_id, text or "", ticket_id=t.id)
    await s.flush()
    return m


async def set_active(s, staff: Staff, ticket_id: int) -> Ticket | None:
    t = await s.get(Ticket, ticket_id)
    if not _is_mine(t, staff):
        return None
    staff.active_ticket_id = t.id
    return t


async def my_tickets(s, staff: Staff) -> list[tuple[Ticket, Client]]:
    q = (
        select(Ticket, Client)
        .join(Client, Client.id == Ticket.client_id)
        .where(Ticket.status == "in_progress", Ticket.assignee_id == staff.id)
        .order_by(Ticket.id)
    )
    return [(t, c) for t, c in (await s.execute(q)).all()]


async def recent_view(s, t: Ticket, limit: int = 5) -> str:
    client = await s.get(Client, t.client_id)
    msgs = (await ticket_messages(s, t.id))[-limit:]
    name = client.full_name or client.wa_name or "+" + client.phone
    body = render.history(msgs, await queries.staff_names(s))
    return f"▶️ Активная заявка #{t.id} · {name}\n\n{render.clip(body)}"


async def link(s, chat_id: int, message_id: int, ticket_id: int) -> None:
    await s.merge(TgLink(tg_chat_id=chat_id, tg_message_id=message_id, ticket_id=ticket_id))


async def deliver_queue_to(s, staff: Staff) -> int:
    q = (
        select(Ticket, Client)
        .join(Client, Client.id == Ticket.client_id)
        .where(Ticket.status == "new")
        .order_by(Ticket.id)
    )
    rows = (await s.execute(q)).all()
    for t, c in rows:
        _send_cards(s, t, c, await ticket_messages(s, t.id), staff)
    return len(rows)
