from datetime import timedelta

from sqlalchemy import select

from helpdesk import reminders
from helpdesk.db.models import Ticket
from tests.factories import make_client, make_staff, make_ticket, payloads


async def test_reminds_only_stale_queued_tickets(sf, clock_):
    now = clock_.now
    async with sf() as s, s.begin():
        erlan = await make_staff(s, tg_id=1, name="Ерлан")
        old = await make_ticket(s, await make_client(s, phone="1"))
        old.created_at = old.last_broadcast_at = now - timedelta(minutes=11)
        fresh = await make_ticket(s, await make_client(s, phone="2"))
        fresh.last_broadcast_at = now - timedelta(minutes=5)
        work = await make_ticket(
            s, await make_client(s, phone="3"), status="in_progress", assignee=erlan
        )
        work.last_broadcast_at = now - timedelta(minutes=30)
        old_id = old.id
    assert await reminders.run_once(sf, 10) == 1
    async with sf() as s:
        t = await s.get(Ticket, old_id)
        assert t.last_broadcast_at == now and t.broadcast_seq == 1
        cards = [p for p in await payloads(s, "tg") if p.get("buttons")]
        assert len(cards) == 1
        assert cards[0]["text"].startswith(f"⏰ Заявка #{old_id} ждёт 11 мин")
    assert await reminders.run_once(sf, 10) == 0


async def test_reminder_without_staff_does_not_warn_admins(sf, clock_):
    async with sf() as s, s.begin():
        await make_staff(s, tg_id=9, name="Админ", is_admin=True, on_shift=False)
        t = await make_ticket(s, await make_client(s))
        t.last_broadcast_at = clock_.now - timedelta(minutes=15)
    assert await reminders.run_once(sf, 10) == 1
    async with sf() as s:
        assert await payloads(s, "tg") == []
        assert (await s.scalar(select(Ticket))).last_broadcast_at == clock_.now
