from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from helpdesk.db.models import Staff, TaskCard, TaskComment, TaskEvent, TaskLink, Ticket
from tests.factories import make_client, make_staff, make_task


async def test_only_one_open_ticket_per_client(s, clock_):
    c = await make_client(s)
    s.add(Ticket(client_id=c.id))
    await s.flush()
    s.add(Ticket(client_id=c.id))
    with pytest.raises(IntegrityError):
        await s.flush()


async def test_closed_ticket_does_not_block_new_one(s, clock_):
    c = await make_client(s)
    s.add(Ticket(client_id=c.id, status="closed"))
    s.add(Ticket(client_id=c.id))
    await s.flush()


async def test_defaults_use_clock(s, clock_):
    c = await make_client(s)
    t = Ticket(client_id=c.id)
    s.add(t)
    await s.flush()
    assert t.created_at == clock_.now
    assert t.status == "new" and t.broadcast_seq == 0


async def test_staff_role_defaults_to_helpdesk(s, clock_):
    st = Staff(tg_user_id=1, tg_chat_id=1, name="Ерлан")
    s.add(st)
    await s.flush()
    await s.refresh(st)
    assert st.role == "helpdesk"


async def test_task_defaults_and_relations(s, clock_):
    st = await make_staff(s, tg_id=1, name="Ерлан")
    t = await make_task(s, st, due_date=date(2026, 10, 10))
    s.add_all(
        [
            TaskComment(task_id=t.id, staff_id=st.id, kind="text", text="ок"),
            TaskEvent(task_id=t.id, type="created", staff_id=st.id),
            TaskCard(task_id=t.id, staff_id=st.id, tg_chat_id=1, tg_message_id=10),
            TaskLink(tg_chat_id=1, tg_message_id=10, task_id=t.id),
        ]
    )
    await s.flush()
    assert t.created_at == clock_.now
    assert (t.due_date, t.last_reminded_on, t.taken_at) == (date(2026, 10, 10), None, clock_.now)
