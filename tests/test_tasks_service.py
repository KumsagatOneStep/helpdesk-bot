from datetime import date

import pytest
from sqlalchemy import select

from helpdesk.db.models import Task, TaskEvent
from helpdesk.tasks import service as tasks
from tests.factories import clear_outbox, make_staff, payloads


async def _team(s):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    tima = await make_staff(s, tg_id=2, name="Тимур")
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    return erlan, tima, coord


def _cards(ps):
    return {p["chat_id"]: p for p in ps if p.get("task_card_staff_id")}


async def test_helpdesk_creates_helpdesk_task_for_self(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await tasks.create(
        s,
        erlan,
        kind="helpdesk",
        description="Картридж",
        requester="каб. 214",
        due_date=date(2026, 10, 10),
    )
    assert (t.status, t.assignee_id, t.creator_id, t.taken_at) == (
        "in_progress",
        erlan.id,
        erlan.id,
        clock_.now,
    )
    cards = _cards(await payloads(s, "tg"))
    assert set(cards) == {1, 2, 3}
    assert cards[1]["buttons"] == [
        [["✅ Закрыть", f"tk_close:{t.id}"], ["🆘 Нужна помощь", f"tk_help:{t.id}"]]
    ]
    assert cards[2]["buttons"] is None and cards[3]["buttons"] is None
    assert cards[2]["text"].startswith(f"📌 З-{t.id} · Helpdesk · срок 10.10")
    assert all(p["task_id"] == t.id for p in cards.values())
    assert (await s.scalars(select(TaskEvent.type))).all() == ["created"]


async def test_helpdesk_sends_coordinator_task(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await tasks.create(s, erlan, kind="coordinator", description="Нужен ноутбук")
    assert (t.status, t.assignee_id, t.taken_at) == ("queued", None, None)
    cards = _cards(await payloads(s, "tg"))
    assert set(cards) == {1, 3}  # Тимур не видит
    assert cards[3]["buttons"] == [[["✅ Принять", f"tk_take:{t.id}"]]]
    assert cards[1]["buttons"] == [[["✅ Закрыть", f"tk_close:{t.id}"]]]


async def test_coordinator_creates_both_kinds_for_self(s, clock_):
    erlan, tima, coord = await _team(s)
    own = await tasks.create(s, coord, kind="coordinator", description="Закупка")
    assert (own.status, own.assignee_id) == ("in_progress", coord.id)
    assert set(_cards(await payloads(s, "tg"))) == {3}
    await clear_outbox(s)
    hd = await tasks.create(s, coord, kind="helpdesk", description="Мышь")
    assert (hd.status, hd.assignee_id) == ("in_progress", coord.id)
    assert set(_cards(await payloads(s, "tg"))) == {1, 2, 3}


async def test_coordinator_task_without_coordinator_is_refused(s, clock_):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    await make_staff(s, tg_id=3, name="Ушла", role="coordinator", status="disabled")
    with pytest.raises(tasks.NoCoordinator):
        await tasks.create(s, erlan, kind="coordinator", description="X")
    assert (await s.scalars(select(Task))).all() == []
    assert await payloads(s) == []


async def test_attachment_goes_before_card(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await tasks.create(
        s,
        erlan,
        kind="helpdesk",
        description="Ошибка",
        file_path="/f/a.jpg",
        file_name="photo_a.jpg",
        file_kind="photo",
    )
    to_tima = [p for p in await payloads(s, "tg") if p["chat_id"] == 2]
    assert [(p["file_path"], p.get("task_card_staff_id")) for p in to_tima] == [
        ("/f/a.jpg", None),
        (None, tima.id),
    ]
    assert to_tima[0]["text"] == f"📎 З-{t.id}" and to_tima[0]["file_kind"] == "photo"


async def test_help_button_hidden_when_nobody_else_can_do(s, clock_):
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    await make_staff(s, tg_id=1, name="Ерлан")
    t = await tasks.create(s, coord, kind="coordinator", description="Закупка")
    assert _cards(await payloads(s, "tg"))[3]["buttons"] == [[["✅ Закрыть", f"tk_close:{t.id}"]]]
