from helpdesk.staff import service as staff_svc
from tests.factories import clear_outbox, make_client, make_staff, make_ticket, payloads


async def test_admin_from_env_is_active(s, clock_):
    st = await staff_svc.register(
        s, tg_user_id=9, chat_id=9, name="Админ", username=None, admin_ids={9}
    )
    assert (st.status, st.is_admin) == ("active", True)
    assert await payloads(s) == []


async def test_newcomer_is_pending_and_admins_are_asked(s, clock_):
    await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    st = await staff_svc.register(
        s, tg_user_id=5, chat_id=5, name="Ерлан", username="erlan", admin_ids={9}
    )
    assert (st.status, st.is_admin) == ("pending", False)
    p = await payloads(s, "tg")
    assert p[0]["chat_id"] == 9
    assert p[0]["text"] == (
        "🙋 Ерлан (@erlan, Telegram id 5) просит доступ к helpdesk-боту.\n"
        "Имя в профиле может быть чужим — сверьте id с коллегой лично."
    )
    assert p[0]["buttons"] == [
        [["✅ Одобрить", f"approve:{st.id}"], ["❌ Отклонить", f"reject:{st.id}"]]
    ]


async def test_register_is_idempotent_and_promotes_env_admin(s, clock_):
    a = await staff_svc.register(
        s, tg_user_id=5, chat_id=5, name="E", username=None, admin_ids=set()
    )
    b = await staff_svc.register(
        s, tg_user_id=5, chat_id=55, name="E", username=None, admin_ids=set()
    )
    assert a.id == b.id and b.tg_chat_id == 55 and b.status == "pending"
    c = await staff_svc.register(
        s, tg_user_id=5, chat_id=55, name="E", username=None, admin_ids={5}
    )
    assert (c.status, c.is_admin) == ("active", True)


async def test_approve_and_reject_require_admin(s, clock_):
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    user = await make_staff(s, tg_id=1, name="Тимур")
    p1 = await make_staff(s, tg_id=5, name="Ерлан", status="pending")
    p2 = await make_staff(s, tg_id=6, name="Гость", status="pending")
    assert await staff_svc.approve(s, user, p1.id) is None
    assert (await staff_svc.approve(s, admin, p1.id)).status == "active"
    assert await staff_svc.approve(s, admin, p1.id) is None  # уже одобрен
    assert (await staff_svc.reject(s, admin, p2.id)).status == "disabled"
    texts_ = {p["chat_id"]: p["text"] for p in await payloads(s, "tg")}
    assert texts_ == {
        5: "✅ Доступ открыт. Нажмите /start, чтобы открыть меню.",
        6: "Доступ отклонён.",
    }


async def test_disable_returns_tickets_to_queue(s, clock_):
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True, on_shift=False)
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    await make_staff(s, tg_id=2, name="Тимур")
    c = await make_client(s)
    t = await make_ticket(s, c, status="in_progress", assignee=erlan)
    erlan.active_ticket_id = t.id
    assert await staff_svc.disable(s, admin, erlan.id)
    await s.refresh(t)
    assert (erlan.status, t.status, t.assignee_id, erlan.active_ticket_id) == (
        "disabled",
        "new",
        None,
        None,
    )
    sends = [p for p in await payloads(s, "tg") if p["op"] == "send" and p.get("buttons")]
    assert [p["chat_id"] for p in sends] == [2]


async def test_admin_cannot_disable_self(s, clock_):
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    assert not await staff_svc.disable(s, admin, admin.id)
    assert not await staff_svc.toggle_admin(s, admin, admin.id)


async def test_enable_and_toggle_admin(s, clock_):
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    u = await make_staff(s, tg_id=1, name="Тимур", status="disabled")
    assert await staff_svc.enable(s, admin, u.id) and u.status == "active"
    assert await staff_svc.toggle_admin(s, admin, u.id) and u.is_admin
    assert await staff_svc.toggle_admin(s, admin, u.id) and not u.is_admin


async def test_shift_on_delivers_queue(s, clock_):
    tima = await make_staff(s, tg_id=2, name="Тимур", on_shift=False)
    c = await make_client(s)
    t = await make_ticket(s, c)
    t.broadcast_seq = 3
    assert await staff_svc.set_shift(s, tima, True) == 1
    p = await payloads(s, "tg")
    assert len(p) == 1 and p[0]["chat_id"] == 2 and p[0]["card_seq"] == 3
    assert await staff_svc.set_shift(s, tima, True) is None  # уже на смене — без дублей
    await clear_outbox(s)
    assert await staff_svc.set_shift(s, tima, False) == 0
    assert await payloads(s) == []


async def test_staff_line_and_buttons(s, clock_):
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    u = await make_staff(s, tg_id=1, name="Тимур")
    assert staff_svc.staff_line(u) == "Тимур — активен, на смене"
    assert staff_svc.staff_line(admin) == "👑 Админ — активен, на смене"
    assert staff_svc.staff_buttons(u, admin) == [
        [("⛔ Отключить", f"stf_dis:{u.id}"), ("👑 Сделать админом", f"stf_adm:{u.id}")]
    ]
    assert staff_svc.staff_buttons(admin, admin) is None


async def test_shift_off_removes_own_queue_cards(s, clock_):
    from sqlalchemy import select

    from helpdesk.db.models import TgCard

    tima = await make_staff(s, tg_id=2, name="Тимур")
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    t = await make_ticket(s, await make_client(s))
    for st, mid in ((tima, 201), (erlan, 101)):
        s.add(TgCard(ticket_id=t.id, staff_id=st.id, tg_chat_id=st.tg_chat_id, tg_message_id=mid))
    await s.flush()
    assert await staff_svc.set_shift(s, tima, False) == 0
    states = dict((await s.execute(select(TgCard.tg_message_id, TgCard.state))).all())
    assert states == {201: "deleting", 101: "open"}
    [p] = await payloads(s, "tg")
    assert p["op"] == "delete_card" and p["fallback_text"] == f"#{t.id} — вы не на смене"
