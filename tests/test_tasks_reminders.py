from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from helpdesk.db.models import Task, TaskEvent
from helpdesk.tasks import reminders
from tests.factories import make_staff, make_task, payloads

TODAY = date(2026, 10, 7)


async def _setup(sf, clock_):
    async with sf() as s, s.begin():
        erlan = await make_staff(s, tg_id=1, name="Ерлан")
        await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
        due = await make_task(s, erlan, due_date=TODAY, description="Сегодня")
        due.created_at = clock_.now - timedelta(days=2)
        over = await make_task(s, erlan, due_date=TODAY - timedelta(days=1), description="Вчера")
        await make_task(s, erlan, due_date=TODAY + timedelta(days=1))
        await make_task(s, erlan, status="closed", due_date=TODAY - timedelta(days=3))
        return due.id, over.id


async def test_no_reminders_before_hour(sf, clock_):
    await _setup(sf, clock_)
    clock_.now = datetime(2026, 10, 7, 2, 0, tzinfo=UTC)  # 07:00 Алматы
    assert await reminders.run_once(sf, 9) == 0


async def test_due_and_overdue_once_per_day(sf, clock_):
    due_id, over_id = await _setup(sf, clock_)
    assert await reminders.run_once(sf, 9) == 2
    async with sf() as s:
        ps = await payloads(s, "tg")
        texts = {(p["chat_id"], p["text"]) for p in ps}
        assert texts == {
            (1, f"⏰ Сегодня срок: З-{due_id}\nСегодня"),
            (1, f"⚠️ Просрочена З-{over_id} (срок был 06.10)\nВчера"),
            (3, f"⚠️ Просрочена З-{over_id} (срок был 06.10)\nВчера"),
        }
        assert all(p["task_id"] in (due_id, over_id) for p in ps)
        assert (await s.get(Task, due_id)).last_reminded_on == TODAY
        types = sorted((await s.scalars(select(TaskEvent.type))).all())
        assert types == ["reminded_due", "reminded_overdue"]
    assert await reminders.run_once(sf, 9) == 0  # повтор в тот же день — тишина
    clock_.advance(days=1)
    # назавтра: две просрочены + задача «на завтра» стала «сегодня» (создана вчера)
    assert await reminders.run_once(sf, 9) == 3


async def test_overdue_without_assignee_goes_to_creator_and_no_duplicates(sf, clock_):
    async with sf() as s, s.begin():
        coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
        t = await make_task(
            s, coord, status="queued", assignee=None, due_date=TODAY - timedelta(days=1)
        )
        tid = t.id
    assert await reminders.run_once(sf, 9) == 1
    async with sf() as s:
        ps = await payloads(s, "tg")
    assert [(p["chat_id"], p["task_id"]) for p in ps] == [
        (3, tid)
    ]  # создатель = координатор, один раз


async def test_due_today_created_today_is_not_reminded(sf, clock_):
    async with sf() as s, s.begin():
        erlan = await make_staff(s, tg_id=1, name="Ерлан")
        t = await make_task(s, erlan, due_date=TODAY)
        tid = t.id
    assert await reminders.run_once(sf, 9) == 1
    async with sf() as s:
        assert await payloads(s, "tg") == []
        assert (await s.get(Task, tid)).last_reminded_on == TODAY
