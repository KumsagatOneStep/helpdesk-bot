from sqlalchemy import select, update

from helpdesk import clock, texts
from helpdesk.db import queries
from helpdesk.db.models import Client, Message, Staff, TgCard, Ticket, TicketEvent
from helpdesk.delivery import outbox
from helpdesk.tickets import render

TG_FILE_KIND = {"image": "photo", "document": "document", "audio": "voice", "video": "video"}


def QUEUE_BUTTONS(tid: int):
    return [[("✋ Взять", f"take:{tid}")]]


def WORK_BUTTONS(tid: int):
    return [[("✅ Закрыть", f"close:{tid}"), ("↩️ Вернуть", f"ret:{tid}")]]


async def open_ticket(s, client_id: int) -> Ticket | None:
    q = select(Ticket).where(Ticket.client_id == client_id, Ticket.status != "closed")
    return await s.scalar(q)


async def ticket_messages(s, ticket_id: int) -> list[Message]:
    q = select(Message).where(Message.ticket_id == ticket_id).order_by(Message.id)
    return list((await s.scalars(q)).all())


async def on_client_message(s, client: Client, message: Message | None) -> Ticket:
    t = await open_ticket(s, client.id)
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
    elif t.status == "new":
        await broadcast(s, t, client)
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
