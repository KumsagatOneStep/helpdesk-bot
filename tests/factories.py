from sqlalchemy import delete, select

from helpdesk import clock
from helpdesk.db.models import Client, Message, Outbox, Staff, Task, Ticket


async def make_client(
    s,
    *,
    phone="77011234567",
    full_name="Айгерим Сериккызы",
    location="Бухгалтерия, каб. 214",
    lang="ru",
    step="done",
) -> Client:
    c = Client(
        wa_chat_id=f"{phone}@c.us",
        phone=phone,
        full_name=full_name,
        location=location,
        lang=lang,
        intake_step=step,
    )
    s.add(c)
    await s.flush()
    return c


async def make_staff(
    s, *, tg_id, name, is_admin=False, status="active", on_shift=True, role="helpdesk"
) -> Staff:
    st = Staff(
        tg_user_id=tg_id,
        tg_chat_id=tg_id,
        name=name,
        is_admin=is_admin,
        status=status,
        on_shift=on_shift,
        role=role,
    )
    s.add(st)
    await s.flush()
    return st


async def make_task(
    s,
    creator,
    *,
    kind="helpdesk",
    status="in_progress",
    assignee="creator",
    description="Заменить картридж",
    requester=None,
    due_date=None,
) -> Task:
    a = creator if assignee == "creator" else assignee
    t = Task(
        kind=kind,
        status=status,
        description=description,
        requester=requester,
        due_date=due_date,
        creator_id=creator.id,
        assignee_id=a.id if a else None,
        taken_at=clock.utcnow() if a else None,
    )
    s.add(t)
    await s.flush()
    return t


async def make_ticket(s, client, *, status="new", assignee=None, text="Не печатает принтер"):
    t = Ticket(client_id=client.id, status=status, assignee_id=assignee.id if assignee else None)
    s.add(t)
    await s.flush()
    s.add(Message(client_id=client.id, ticket_id=t.id, direction="in", kind="text", text=text))
    await s.flush()
    return t


async def payloads(s, channel=None) -> list[dict]:
    q = select(Outbox).order_by(Outbox.id)
    if channel:
        q = q.where(Outbox.channel == channel)
    return [o.payload for o in (await s.scalars(q)).all()]


async def clear_outbox(s) -> None:
    await s.execute(delete(Outbox))
