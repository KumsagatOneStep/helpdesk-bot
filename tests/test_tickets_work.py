import asyncio

from sqlalchemy import func, select

from helpdesk.db.models import Message, Staff, TgCard, TgLink, TicketEvent
from helpdesk.tickets import service as tickets
from tests.factories import clear_outbox, make_client, make_staff, make_ticket, payloads


async def _queued(s):
    c = await make_client(s)
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    tima = await make_staff(s, tg_id=2, name="Тимур")
    t = await make_ticket(s, c)
    for st, mid in ((erlan, 101), (tima, 201)):
        s.add(
            TgCard(
                ticket_id=t.id,
                staff_id=st.id,
                tg_chat_id=st.tg_chat_id,
                tg_message_id=mid,
                has_button=True,
            )
        )
    await s.flush()
    await clear_outbox(s)
    return c, erlan, tima, t


async def test_take_assigns_and_cleans_other_cards(s, clock_):
    c, erlan, tima, t = await _queued(s)
    r = await tickets.take(s, t.id, erlan)
    assert r == tickets.TakeResult(True, "Ерлан")
    await s.refresh(t)
    assert (t.status, t.assignee_id, t.first_taken_at) == ("in_progress", erlan.id, clock_.now)
    assert erlan.active_ticket_id == t.id
    states = dict((await s.execute(select(TgCard.tg_message_id, TgCard.state))).all())
    assert states == {101: "kept", 201: "deleting"}
    tg = await payloads(s, "tg")
    deletes = [p for p in tg if p["op"] == "delete_card"]
    edits = [p for p in tg if p["op"] == "edit"]
    assert [d["fallback_text"] for d in deletes] == [f"🔒 #{t.id} взял Ерлан"]
    assert (edits[0]["chat_id"], edits[0]["message_id"]) == (1, 101)
    assert edits[0]["buttons"] == [[["✅ Закрыть", f"close:{t.id}"], ["↩️ Вернуть", f"ret:{t.id}"]]]
    assert edits[0]["text"].startswith(f"✅ Заявка #{t.id} у вас в работе")
    wa = await payloads(s, "wa")
    assert wa[0]["text"] == f"Вашей заявкой #{t.id} занимается Ерлан."
    types = (await s.scalars(select(TicketEvent.type))).all()
    assert types == ["taken"]


async def test_concurrent_take_has_one_winner(sf, clock_):
    async with sf() as s, s.begin():
        c, erlan, tima, t = await _queued(s)
        ids = (erlan.id, tima.id, t.id)

    async def attempt(staff_id):
        async with sf() as s, s.begin():
            st = await s.get(Staff, staff_id)
            return await tickets.take(s, ids[2], st)

    results = await asyncio.gather(attempt(ids[0]), attempt(ids[1]))
    assert sorted(r.ok for r in results) == [False, True]
    winner = next(r for r in results if r.ok)
    loser = next(r for r in results if not r.ok)
    assert loser.holder_name == winner.holder_name
    async with sf() as s:
        q = select(func.count()).select_from(TicketEvent).where(TicketEvent.type == "taken")
        assert await s.scalar(q) == 1


async def test_late_card_of_loser_is_removed(s, clock_):
    c, erlan, tima, t = await _queued(s)
    await tickets.take(s, t.id, erlan)
    s.add(
        TgCard(ticket_id=t.id, staff_id=tima.id, tg_chat_id=2, tg_message_id=202, has_button=True)
    )
    await s.flush()
    await clear_outbox(s)
    r = await tickets.take(s, t.id, tima)
    assert r == tickets.TakeResult(False, "Ерлан")
    p = await payloads(s, "tg")
    assert [x["op"] for x in p] == ["delete_card"]


async def test_take_without_card_sends_working_card(s, clock_):
    c = await make_client(s)
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    t = await make_ticket(s, c)
    await tickets.take(s, t.id, erlan)
    sends = [p for p in await payloads(s, "tg") if p["op"] == "send"]
    assert len(sends) == 1 and sends[0]["chat_id"] == 1
    assert sends[0]["buttons"][0][0] == ["✅ Закрыть", f"close:{t.id}"]
    assert sends[0]["ticket_id"] == t.id


async def test_return_requeues_and_clears_active(s, clock_):
    c, erlan, tima, t = await _queued(s)
    await tickets.take(s, t.id, erlan)
    await clear_outbox(s)
    assert not await tickets.return_ticket(s, t.id, tima)
    assert await tickets.return_ticket(s, t.id, erlan)
    await s.refresh(t)
    assert (t.status, t.assignee_id, erlan.active_ticket_id) == ("new", None, None)
    assert t.broadcast_seq == 1
    p = await payloads(s, "tg")
    assert sorted(x["chat_id"] for x in p if x["op"] == "send") == [1, 2]
    assert not any(x["op"] == "delete_card" for x in p)
    types = (await s.scalars(select(TicketEvent.type).order_by(TicketEvent.id))).all()
    assert types == ["taken", "returned"]


async def test_admin_can_return_foreign_ticket(s, clock_):
    c, erlan, tima, t = await _queued(s)
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    await tickets.take(s, t.id, erlan)
    assert await tickets.return_ticket(s, t.id, admin)


async def test_close_notifies_client_and_next_message_opens_new(s, clock_):
    c, erlan, tima, t = await _queued(s)
    await tickets.take(s, t.id, erlan)
    await clear_outbox(s)
    assert not await tickets.close_ticket(s, t.id, tima)
    assert await tickets.close_ticket(s, t.id, erlan)
    await s.refresh(t)
    assert (t.status, t.closed_at, t.closed_by_id) == ("closed", clock_.now, erlan.id)
    assert erlan.active_ticket_id is None
    wa = await payloads(s, "wa")
    assert wa[-1]["text"] == f"Заявка #{t.id} выполнена. Если проблема осталась — напишите снова."
    m = Message(client_id=c.id, direction="in", kind="text", text="Опять не печатает")
    s.add(m)
    await s.flush()
    t2 = await tickets.on_client_message(s, c, m)
    assert t2.id != t.id and t2.status == "new"


async def _two_in_work(s):
    c1 = await make_client(s)
    c2 = await make_client(s, phone="77017654321", full_name="Бауыржан")
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    t1 = await make_ticket(s, c1, status="in_progress", assignee=erlan)
    t2 = await make_ticket(s, c2, status="in_progress", assignee=erlan)
    erlan.active_ticket_id = t2.id
    s.add(TgLink(tg_chat_id=1, tg_message_id=700, ticket_id=t1.id))
    await s.flush()
    return erlan, t1, t2


async def test_resolve_prefers_reply_link(s, clock_):
    erlan, t1, t2 = await _two_in_work(s)
    assert await tickets.resolve_target(s, erlan, 700) == (t1, "")


async def test_plain_message_goes_to_active(s, clock_):
    erlan, t1, t2 = await _two_in_work(s)
    assert await tickets.resolve_target(s, erlan, None) == (t2, "")
    assert await tickets.resolve_target(s, erlan, 99999) == (t2, "")  # reply на не-заявку


async def test_reply_to_closed_ticket_is_refused_not_redirected(s, clock_):
    erlan, t1, t2 = await _two_in_work(s)
    t1.status = "closed"
    await s.flush()
    t, err = await tickets.resolve_target(s, erlan, 700)
    assert t is None
    assert err == f"Заявка #{t1.id} закрыта или не у вас — сообщение не отправлено."


async def test_no_active_after_close(s, clock_):
    erlan, t1, t2 = await _two_in_work(s)
    assert await tickets.close_ticket(s, t2.id, erlan)
    t, err = await tickets.resolve_target(s, erlan, None)
    assert t is None
    assert err == "Нет активной заявки — сообщение не отправлено. /my — ваши заявки."


async def test_send_staff_reply_text_and_file(s, clock_):
    erlan, t1, t2 = await _two_in_work(s)
    m = await tickets.send_staff_reply(s, erlan, t2, "text", "Иду", None, None)
    assert (m.direction, m.staff_id, m.ticket_id) == ("out", erlan.id, t2.id)
    await tickets.send_staff_reply(s, erlan, t2, "image", "скрин", "/f/a.jpg", "a.jpg")
    wa = await payloads(s, "wa")
    assert wa[0] == {"op": "text", "chat_id": "77017654321@c.us", "text": "Иду", "ticket_id": t2.id}
    assert (wa[1]["op"], wa[1]["path"], wa[1]["file_name"], wa[1]["caption"]) == (
        "file",
        "/f/a.jpg",
        "a.jpg",
        "скрин",
    )


async def test_set_active_and_my_tickets(s, clock_):
    erlan, t1, t2 = await _two_in_work(s)
    assert (await tickets.set_active(s, erlan, t1.id)).id == t1.id
    assert erlan.active_ticket_id == t1.id
    other = await make_staff(s, tg_id=3, name="Алибек")
    assert await tickets.set_active(s, other, t1.id) is None
    rows = await tickets.my_tickets(s, erlan)
    assert [t.id for t, _ in rows] == [t1.id, t2.id]
    view = await tickets.recent_view(s, t1)
    assert view.startswith(f"▶️ Активная заявка #{t1.id} · Айгерим Сериккызы")
