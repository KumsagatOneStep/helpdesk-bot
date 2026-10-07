from sqlalchemy import select

from helpdesk.db.models import Message, TgCard, TicketEvent
from helpdesk.tickets import service as tickets
from tests.factories import clear_outbox, make_client, make_staff, make_ticket, payloads


async def _in(s, client, text="Не печатает принтер", **kw) -> Message:
    m = Message(
        client_id=client.id,
        direction="in",
        kind=kw.get("kind", "text"),
        text=text,
        file_path=kw.get("file_path"),
        file_name=kw.get("file_name"),
    )
    s.add(m)
    await s.flush()
    return m


async def test_first_message_creates_ticket_and_broadcasts(s, clock_):
    c = await make_client(s)
    await make_staff(s, tg_id=1, name="Ерлан")
    await make_staff(s, tg_id=2, name="Тимур")
    await make_staff(s, tg_id=3, name="Алибек", on_shift=False)
    await make_staff(s, tg_id=4, name="Новичок", status="pending")
    m = await _in(s, c)
    t = await tickets.on_client_message(s, c, m)
    await s.refresh(m)
    assert t.status == "new" and m.ticket_id == t.id
    assert t.broadcast_seq == 1 and t.last_broadcast_at == clock_.now
    assert await payloads(s, "wa") == [
        {
            "op": "text",
            "chat_id": c.wa_chat_id,
            "text": f"Заявка #{t.id} принята. Специалист скоро ответит.",
            "ticket_id": t.id,
        }
    ]
    cards = await payloads(s, "tg")
    assert sorted(p["chat_id"] for p in cards) == [1, 2]
    assert all(p["buttons"] == [[["✋ Взять", f"take:{t.id}"]]] for p in cards)
    assert all(p["card_seq"] == 1 and p["ticket_id"] == t.id for p in cards)
    assert "Айгерим Сериккызы" in cards[0]["text"] and "Не печатает принтер" in cards[0]["text"]
    types = (await s.scalars(select(TicketEvent.type))).all()
    assert types == ["created"]


async def test_pending_messages_without_ticket_are_attached(s, clock_):
    c = await make_client(s)
    await make_staff(s, tg_id=1, name="Ерлан")
    first = await _in(s, c, "Привет")
    s.add(Message(client_id=c.id, direction="in", kind="text", text="1", intake=True))
    await s.flush()
    t = await tickets.on_client_message(s, c, None)
    texts_ = [m.text for m in await tickets.ticket_messages(s, t.id)]
    assert texts_ == ["Привет"]
    await s.refresh(first)
    assert first.ticket_id == t.id


async def test_attachment_cards_come_before_button_card(s, clock_):
    c = await make_client(s)
    st = await make_staff(s, tg_id=1, name="Ерлан")
    m = await _in(
        s, c, "вот ошибка", kind="image", file_path="/data/files/x.jpg", file_name="x.jpg"
    )
    await tickets.on_client_message(s, c, m)
    cards = await payloads(s, "tg")
    assert [p["file_kind"] for p in cards] == ["photo", None]
    assert cards[0]["file_path"] == "/data/files/x.jpg" and cards[0]["text"] == "вот ошибка"
    assert cards[0]["card_staff_id"] == st.id and cards[0]["buttons"] is None


async def test_next_message_while_queued_rebroadcasts(s, clock_):
    c = await make_client(s)
    st = await make_staff(s, tg_id=1, name="Ерлан")
    t = await tickets.on_client_message(s, c, await _in(s, c))
    s.add(TgCard(ticket_id=t.id, staff_id=st.id, tg_chat_id=1, tg_message_id=500, has_button=True))
    await s.flush()
    await clear_outbox(s)
    t2 = await tickets.on_client_message(s, c, await _in(s, c, "И сканер тоже"))
    assert t2.id == t.id and t.broadcast_seq == 2
    p = await payloads(s, "tg")
    assert p[0]["op"] == "delete_card"
    assert [x["op"] for x in p[1:]] == ["send"]
    assert p[-1]["card_seq"] == 2
    assert "Не печатает принтер" in p[-1]["text"] and "И сканер тоже" in p[-1]["text"]
    assert await payloads(s, "wa") == []


async def test_message_in_progress_goes_to_assignee_only(s, clock_):
    c = await make_client(s)
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    await make_staff(s, tg_id=2, name="Тимур")
    t = await make_ticket(s, c, status="in_progress", assignee=erlan)
    await tickets.on_client_message(s, c, await _in(s, c, "Жду"))
    p = await payloads(s, "tg")
    assert len(p) == 1
    assert p[0]["chat_id"] == 1 and p[0]["ticket_id"] == t.id
    assert p[0]["text"] == f"#{t.id} · Айгерим Сериккызы\nЖду"
    assert p[0]["card_staff_id"] is None


async def test_file_in_progress_forwarded_as_file(s, clock_):
    c = await make_client(s)
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    t = await make_ticket(s, c, status="in_progress", assignee=erlan)
    m = await _in(s, c, None, kind="audio", file_path="/f/v.ogg", file_name="v.ogg")
    await tickets.on_client_message(s, c, m)
    p = (await payloads(s, "tg"))[0]
    assert (p["file_kind"], p["file_path"], p["text"]) == (
        "voice",
        "/f/v.ogg",
        f"#{t.id} · Айгерим Сериккызы",
    )


async def test_nobody_on_shift_warns_admins(s, clock_):
    c = await make_client(s)
    await make_staff(s, tg_id=9, name="Админ", is_admin=True, on_shift=False)
    t = await tickets.on_client_message(s, c, await _in(s, c))
    p = await payloads(s, "tg")
    assert len(p) == 1 and p[0]["chat_id"] == 9
    assert p[0]["text"] == f"⚠️ Заявка #{t.id}: на смене никого нет"


async def test_nobody_on_shift_warns_admins_only_once(s, clock_):
    c = await make_client(s)
    await make_staff(s, tg_id=9, name="Админ", is_admin=True, on_shift=False)
    await tickets.on_client_message(s, c, await _in(s, c))
    await tickets.on_client_message(s, c, await _in(s, c, "ещё"))
    await tickets.on_client_message(s, c, await _in(s, c, "и ещё"))
    assert len(await payloads(s, "tg")) == 1
