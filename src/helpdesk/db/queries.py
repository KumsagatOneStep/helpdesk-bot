from sqlalchemy import select

from helpdesk.db.models import Staff


async def on_shift_staff(s) -> list[Staff]:
    q = select(Staff).where(Staff.status == "active", Staff.on_shift.is_(True)).order_by(Staff.id)
    return list((await s.scalars(q)).all())


async def active_admins(s) -> list[Staff]:
    q = select(Staff).where(Staff.status == "active", Staff.is_admin.is_(True)).order_by(Staff.id)
    return list((await s.scalars(q)).all())


async def staff_names(s) -> dict[int, str]:
    return {sid: name for sid, name in (await s.execute(select(Staff.id, Staff.name))).all()}
