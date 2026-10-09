from datetime import date

from helpdesk.db.models import TaskCard, TaskComment
from helpdesk.tasks import service as tasks
from tests.factories import clear_outbox, make_staff, make_task, payloads


async def _team(s):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    tima = await make_staff(s, tg_id=2, name="Тимур")
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    return erlan, tima, coord


async def test_comment_goes_to_other_participants(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await make_task(s, erlan)
    c = await tasks.comment(s, t, tima, kind="text", text="Я посмотрю")
    assert c.id and c.task_id == t.id
    ps = await payloads(s, "tg")
    assert sorted(p["chat_id"] for p in ps) == [1, 3]
    assert {p["text"] for p in ps} == {f"💬 З-{t.id} · Тимур: Я посмотрю"}
    assert all(p["task_id"] == t.id for p in ps)
    assert await payloads(s, "wa") == []


async def test_comment_with_file_and_on_closed_task(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await make_task(s, erlan, kind="coordinator", assignee=coord)
    await tasks.comment(
        s, t, coord, kind="image", text=None, file_path="/f/x.jpg", file_name="x.jpg"
    )
    [p] = await payloads(s, "tg")
    assert (p["chat_id"], p["text"], p["file_kind"]) == (1, f"💬 З-{t.id} · Айжан", "photo")
    await clear_outbox(s)
    t.status = "closed"
    assert await tasks.comment(s, t, coord, kind="text", text="ещё") is None
    assert await payloads(s) == []


async def test_reply_lookup(s, clock_):
    erlan, *_ = await _team(s)
    t = await make_task(s, erlan)
    await tasks.link(s, 1, 555, t.id)
    assert (await tasks.task_for_reply(s, 1, 555)).id == t.id
    assert await tasks.task_for_reply(s, 1, 556) is None
    assert await tasks.task_for_reply(s, 2, 555) is None


async def test_my_and_queued(s, clock_):
    erlan, tima, coord = await _team(s)
    late = await make_task(s, erlan, due_date=date(2026, 10, 20))
    soon = await make_task(s, erlan, due_date=date(2026, 10, 8))
    nodue = await make_task(s, erlan)
    await make_task(s, erlan, status="closed")
    await make_task(s, tima)
    q_hd = await make_task(s, tima, status="queued", assignee=None)
    q_co = await make_task(s, erlan, kind="coordinator", status="queued", assignee=None)
    assert [t.id for t in await tasks.my_tasks(s, erlan)] == [soon.id, late.id, nodue.id]
    assert [t.id for t in await tasks.queued_for(s, erlan)] == [q_hd.id]
    assert [t.id for t in await tasks.queued_for(s, coord)] == [q_hd.id, q_co.id]


async def test_deliver_queue_on_shift(s, clock_):
    erlan, tima, coord = await _team(s)
    q = await make_task(s, erlan, status="queued", assignee=None)
    q.taken_at = clock_.now
    await clear_outbox(s)
    assert await tasks.deliver_queue_to(s, tima) == 1
    [p] = await payloads(s, "tg")
    assert p["chat_id"] == 2 and p["task_card_staff_id"] == tima.id
    assert p["text"].startswith(f"🆘 Задаче нужен исполнитель\n📌 З-{q.id}")


async def test_helpdesk_cannot_see_foreign_coordinator_tasks(s, clock_):
    erlan, tima, coord = await _team(s)
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    foreign = await make_task(
        s, erlan, kind="coordinator", assignee=coord, description="Ноутбук для бухгалтерии"
    )
    shared = await make_task(s, erlan, description="Ноутбук не включается")
    assert [t.id for t in await tasks.search(s, tima, "ноутбук")] == [shared.id]
    assert [t.id for t in await tasks.search(s, erlan, "ноутбук")] == [shared.id, foreign.id]
    assert [t.id for t in await tasks.search(s, coord, "НОУТБУК")] == [shared.id, foreign.id]
    assert await tasks.get_visible(s, tima, foreign.id) is None
    assert (await tasks.get_visible(s, admin, foreign.id)).id == foreign.id
    assert [t.id for t in await tasks.open_tasks(s, tima)] == [shared.id]
    assert [t.id for t in await tasks.open_tasks(s, coord)] == [foreign.id, shared.id]


async def test_search_by_requester_and_recent_comments(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await make_task(s, erlan, requester="Иванова, каб. 214")
    assert [x.id for x in await tasks.search(s, tima, "каб. 214")] == [t.id]
    for i in range(7):
        s.add(TaskComment(task_id=t.id, staff_id=erlan.id, kind="text", text=f"c{i}"))
    await s.flush()
    assert [c.text for c in await tasks.recent_comments(s, t.id)] == ["c2", "c3", "c4", "c5", "c6"]


async def test_deliver_queue_skips_tasks_already_carded(s, clock_):
    erlan, tima, coord = await _team(s)
    q = await make_task(s, erlan, status="queued", assignee=None)
    s.add(TaskCard(task_id=q.id, staff_id=tima.id, tg_chat_id=2, tg_message_id=7))
    await s.flush()
    await clear_outbox(s)
    assert await tasks.deliver_queue_to(s, tima) == 0
    assert await payloads(s, "tg") == []
    assert await tasks.deliver_queue_to(s, coord) == 1
