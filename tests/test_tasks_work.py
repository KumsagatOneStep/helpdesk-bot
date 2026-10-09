import asyncio

from sqlalchemy import select

from helpdesk.db.models import Staff, Task, TaskEvent
from helpdesk.tasks import service as tasks
from tests.factories import clear_outbox, make_staff, make_task, payloads


async def _team(s):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    tima = await make_staff(s, tg_id=2, name="Тимур")
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    return erlan, tima, coord


def _texts(ps, chat_id):
    return [p["text"] for p in ps if p["op"] == "send" and p["chat_id"] == chat_id]


async def test_coordinator_accepts_and_creator_is_notified(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await make_task(s, erlan, kind="coordinator", status="queued", assignee=None)
    r = await tasks.take(s, t.id, coord)
    assert r == tasks.TakeResult(True, "Айжан")
    await s.refresh(t)
    assert (t.status, t.assignee_id, t.taken_at) == ("in_progress", coord.id, clock_.now)
    ps = await payloads(s, "tg")
    assert _texts(ps, 1) == [f"✅ Айжан принял(а) З-{t.id}"]
    edits = {p["chat_id"]: p for p in ps if p["op"] == "task_edit"}
    assert set(edits) == {1, 3}
    assert edits[3]["buttons"] == [[["✅ Закрыть", f"tk_close:{t.id}"]]]
    assert (await s.scalars(select(TaskEvent.type))).all() == ["accepted"]


async def test_take_refusals(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await make_task(s, erlan, kind="coordinator", status="queued", assignee=None)
    assert await tasks.take(s, t.id, tima) == tasks.TakeResult(False, None)  # не может
    await tasks.take(s, t.id, coord)
    assert await tasks.take(s, t.id, coord) == tasks.TakeResult(False, "Айжан")
    assert await tasks.take(s, 999, coord) == tasks.TakeResult(False, None)


async def test_help_goes_to_on_shift_colleagues_only(s, clock_):
    erlan, tima, coord = await _team(s)
    coord.on_shift = False
    t = await make_task(s, erlan)
    await clear_outbox(s)
    assert await tasks.ask_help(s, t.id, erlan) == "ok"
    await s.refresh(t)
    assert (t.status, t.assignee_id) == ("queued", None)
    ps = await payloads(s, "tg")
    new_cards = [p for p in ps if p["op"] == "send" and p.get("task_card_staff_id")]
    assert [p["chat_id"] for p in new_cards] == [2]
    assert new_cards[0]["text"].startswith(f"🆘 Ерлан просит помощи\n📌 З-{t.id}")
    assert new_cards[0]["buttons"] == [[["✋ Взять", f"tk_take:{t.id}"]]]
    edits = {p["chat_id"]: p["buttons"] for p in ps if p["op"] == "task_edit"}
    assert edits[1] == [[["✋ Взять", f"tk_take:{t.id}"]], [["✅ Закрыть", f"tk_close:{t.id}"]]]
    await clear_outbox(s)
    assert await tasks.take(s, t.id, tima) == tasks.TakeResult(True, "Тимур")
    assert _texts(await payloads(s, "tg"), 1) == [f"🤝 З-{t.id} взял(а) Тимур"]
    types = (await s.scalars(select(TaskEvent.type).order_by(TaskEvent.id))).all()
    assert types == ["help", "taken"]


async def test_help_when_nobody_on_shift_keeps_task(s, clock_):
    erlan, tima, coord = await _team(s)
    tima.on_shift = coord.on_shift = False
    t = await make_task(s, erlan)
    await clear_outbox(s)
    assert await tasks.ask_help(s, t.id, erlan) == "nobody"
    await s.refresh(t)
    assert (t.status, t.assignee_id) == ("in_progress", erlan.id)
    assert await payloads(s) == []


async def test_help_denied_for_non_assignee(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await make_task(s, erlan)
    assert await tasks.ask_help(s, t.id, tima) == "denied"


async def test_release_moves_task_to_queue(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await make_task(s, erlan)
    await clear_outbox(s)
    erlan.status = "disabled"
    assert await tasks.release(s, t.id, erlan) is True
    await s.refresh(t)
    assert (t.status, t.assignee_id) == ("queued", None)
    new_cards = [p for p in await payloads(s, "tg") if p.get("task_card_staff_id")]
    assert sorted(p["chat_id"] for p in new_cards) == [2, 3]
    assert new_cards[0]["text"].startswith("🆘 Ерлан отключён — задаче нужен исполнитель")
    assert await tasks.release(s, t.id, erlan) is False


async def test_close_rights_and_notifications(s, clock_):
    erlan, tima, coord = await _team(s)
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    t = await make_task(s, erlan, assignee=tima)
    assert await tasks.close(s, t.id, coord) is False  # не исполнитель, не создатель, не админ
    await clear_outbox(s)
    assert await tasks.close(s, t.id, erlan) is True  # создатель
    await s.refresh(t)
    assert (t.status, t.closed_by_id, t.closed_at) == ("closed", erlan.id, clock_.now)
    ps = await payloads(s, "tg")
    notified = sorted(p["chat_id"] for p in ps if p["op"] == "send")
    assert notified == [2, 3, 9]
    assert _texts(ps, 2) == [f"✅ З-{t.id} закрыта (Ерлан)"]
    assert all(p["buttons"] is None for p in ps if p["op"] == "task_edit")
    assert await tasks.close(s, t.id, erlan) is False
    t2 = await make_task(s, erlan)
    assert await tasks.close(s, t2.id, admin) is True


async def test_concurrent_take_has_one_winner(sf, clock_):
    async with sf() as s, s.begin():
        erlan, tima, coord = await _team(s)
        t = await make_task(s, erlan, status="queued", assignee=None)
        t.taken_at = clock_.now
        ids = (erlan.id, tima.id, t.id)

    async def attempt(staff_id):
        async with sf() as s, s.begin():
            st = await s.get(Staff, staff_id)
            return await tasks.take(s, ids[2], st)

    r1, r2 = await asyncio.gather(attempt(ids[0]), attempt(ids[1]))
    assert sorted([r1.ok, r2.ok]) == [False, True]
    async with sf() as s:
        t = await s.get(Task, ids[2])
        winner = r1 if r1.ok else r2
        loser = r2 if r1.ok else r1
        assert loser.holder_name == winner.holder_name
        assert t.assignee_id in ids[:2]
