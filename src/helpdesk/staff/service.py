from sqlalchemy import select

from helpdesk.db import queries
from helpdesk.db.models import Staff, Ticket
from helpdesk.delivery import outbox
from helpdesk.delivery.outbox import Buttons
from helpdesk.tickets import service as tickets

STATUS_RU = {"pending": "ждёт одобрения", "active": "активен", "disabled": "отключён"}


async def register(s, *, tg_user_id, chat_id, name, username, admin_ids: set[int]) -> Staff:
    is_env_admin = tg_user_id in admin_ids
    st = await s.scalar(select(Staff).where(Staff.tg_user_id == tg_user_id))
    if st is not None:
        st.tg_chat_id = chat_id
        if is_env_admin:
            st.is_admin, st.status = True, "active"
        return st
    st = Staff(
        tg_user_id=tg_user_id,
        tg_chat_id=chat_id,
        name=name,
        username=username,
        is_admin=is_env_admin,
        status="active" if is_env_admin else "pending",
    )
    s.add(st)
    await s.flush()
    if not is_env_admin:
        who = f"{name} (@{username})" if username else name
        for a in await queries.active_admins(s):
            outbox.tg_send(
                s,
                a.tg_chat_id,
                text=f"🙋 {who} просит доступ к helpdesk-боту",
                buttons=[
                    [("✅ Одобрить", f"approve:{st.id}"), ("❌ Отклонить", f"reject:{st.id}")]
                ],
            )
    return st


async def _target(s, admin: Staff, staff_id: int) -> Staff | None:
    if not (admin.is_admin and admin.status == "active"):
        return None
    return await s.get(Staff, staff_id)


async def approve(s, admin: Staff, staff_id: int) -> Staff | None:
    st = await _target(s, admin, staff_id)
    if st is None or st.status != "pending":
        return None
    st.status = "active"
    outbox.tg_send(s, st.tg_chat_id, text="✅ Доступ открыт. /help — список команд.")
    return st


async def reject(s, admin: Staff, staff_id: int) -> Staff | None:
    st = await _target(s, admin, staff_id)
    if st is None or st.status != "pending":
        return None
    st.status = "disabled"
    outbox.tg_send(s, st.tg_chat_id, text="Доступ отклонён.")
    return st


async def disable(s, admin: Staff, staff_id: int) -> bool:
    st = await _target(s, admin, staff_id)
    if st is None or st.id == admin.id or st.status == "disabled":
        return False
    st.status = "disabled"
    await s.flush()
    q = select(Ticket.id).where(Ticket.status == "in_progress", Ticket.assignee_id == st.id)
    for tid in (await s.scalars(q)).all():
        await tickets.return_ticket(s, tid, admin)
    return True


async def enable(s, admin: Staff, staff_id: int) -> bool:
    st = await _target(s, admin, staff_id)
    if st is None or st.status == "active":
        return False
    st.status = "active"
    return True


async def toggle_admin(s, admin: Staff, staff_id: int) -> bool:
    st = await _target(s, admin, staff_id)
    if st is None or st.id == admin.id:
        return False
    st.is_admin = not st.is_admin
    return True


async def set_shift(s, staff: Staff, on: bool) -> int | None:
    if staff.on_shift == on:
        return None
    staff.on_shift = on
    if not on:
        return 0
    return await tickets.deliver_queue_to(s, staff)


async def list_staff(s) -> list[Staff]:
    return list((await s.scalars(select(Staff).order_by(Staff.id))).all())


def staff_line(st: Staff) -> str:
    crown = "👑 " if st.is_admin else ""
    nick = f" (@{st.username})" if st.username else ""
    shift = ", на смене" if st.status == "active" and st.on_shift else ""
    return f"{crown}{st.name}{nick} — {STATUS_RU[st.status]}{shift}"


def staff_buttons(st: Staff, me: Staff) -> Buttons | None:
    if st.id == me.id:
        return None
    if st.status == "active":
        adm = "Снять админа" if st.is_admin else "👑 Сделать админом"
        return [[("⛔ Отключить", f"stf_dis:{st.id}"), (adm, f"stf_adm:{st.id}")]]
    return [[("✅ Включить", f"stf_en:{st.id}")]]
