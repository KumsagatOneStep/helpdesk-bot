from sqlalchemy import or_, select

from helpdesk import clock
from helpdesk.db.models import Staff, Task, TaskEvent
from helpdesk.tasks import render, rules
from helpdesk.tasks import service as tasks


async def run_once(sf, remind_hour: int) -> int:
    local_now = clock.local(clock.utcnow())
    if local_now.hour < remind_hour:
        return 0
    today = local_now.date()
    async with sf() as s, s.begin():
        q = (
            select(Task)
            .where(
                Task.status != "closed",
                Task.due_date <= today,
                or_(Task.last_reminded_on.is_(None), Task.last_reminded_on < today),
            )
            .order_by(Task.id)
            .with_for_update(skip_locked=True)
        )
        due = list((await s.scalars(q)).all())
        coords = await rules.coordinators(s)
        for t in due:
            t.last_reminded_on = today
            # создал сегодня со сроком «сегодня» — человек и так в курсе
            if t.due_date == today and clock.local(t.created_at).date() == today:
                continue
            owner = await s.get(Staff, t.assignee_id or t.creator_id)
            body = render.short(t.description)
            if t.due_date == today:
                text = f"⏰ Сегодня срок: {render.number(t)}\n{body}"
                people, event = [owner], "reminded_due"
            else:
                text = f"⚠️ Просрочена {render.number(t)} (срок был {t.due_date:%d.%m})\n{body}"
                people, event = [owner, *coords], "reminded_overdue"
            tasks.notify(s, t, people, text)
            s.add(TaskEvent(task_id=t.id, type=event))
    return len(due)
