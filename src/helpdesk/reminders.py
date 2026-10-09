import asyncio
from datetime import timedelta

import structlog
from sqlalchemy import select

from helpdesk import clock
from helpdesk.db.models import Ticket
from helpdesk.tasks import reminders as task_reminders
from helpdesk.tickets import service as tickets

log = structlog.get_logger()


async def run_once(sf, after_min: int) -> int:
    async with sf() as s, s.begin():
        now = clock.utcnow()
        q = (
            select(Ticket)
            .where(
                Ticket.status == "new",
                Ticket.last_broadcast_at <= now - timedelta(minutes=after_min),
            )
            .order_by(Ticket.id)
            .with_for_update(skip_locked=True)
        )
        due = list((await s.scalars(q)).all())
        for t in due:
            waited = max(1, int((now - t.created_at).total_seconds() // 60))
            await tickets.broadcast(s, t, waited_min=waited, warn_if_empty=False)
    return len(due)


async def run(
    sf, after_min: int, stop: asyncio.Event, interval: float = 60.0, task_remind_hour: int = 9
) -> None:
    while not stop.is_set():
        try:
            await run_once(sf, after_min)
        except Exception:
            log.exception("reminders failed")
        try:
            await task_reminders.run_once(sf, task_remind_hour)
        except Exception:
            log.exception("task reminders failed")
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except TimeoutError:
            pass
