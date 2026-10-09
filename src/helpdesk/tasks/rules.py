from sqlalchemy import or_, select, true

from helpdesk.db.models import Staff, Task

KINDS = ("helpdesk", "coordinator")


def can_do(staff: Staff, kind: str) -> bool:
    return staff.status == "active" and (kind == "helpdesk" or staff.role == "coordinator")


async def eligible(s, kind: str) -> list[Staff]:
    q = select(Staff).where(Staff.status == "active").order_by(Staff.id)
    if kind == "coordinator":
        q = q.where(Staff.role == "coordinator")
    return list((await s.scalars(q)).all())


async def coordinators(s) -> list[Staff]:
    return await eligible(s, "coordinator")


async def participants(s, t: Task) -> list[Staff]:
    """Кто может выполнять + создатель + исполнитель (только активные), считается на момент вызова."""
    people = {st.id: st for st in await eligible(s, t.kind)}
    for sid in (t.creator_id, t.assignee_id):
        if sid and sid not in people:
            st = await s.get(Staff, sid)
            if st is not None and st.status == "active":
                people[sid] = st
    return [people[k] for k in sorted(people)]


def _sees_all(staff: Staff) -> bool:
    return staff.is_admin or staff.role == "coordinator"


def can_see(staff: Staff, t: Task) -> bool:
    return _sees_all(staff) or t.kind == "helpdesk" or staff.id in (t.creator_id, t.assignee_id)


def visible_cond(staff: Staff):
    if _sees_all(staff):
        return true()
    return or_(Task.kind == "helpdesk", Task.creator_id == staff.id, Task.assignee_id == staff.id)
