import pytest
from sqlalchemy.exc import IntegrityError

from helpdesk.db.models import Ticket
from tests.factories import make_client


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
