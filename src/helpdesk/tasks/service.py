from dataclasses import dataclass
from datetime import date

from sqlalchemy import or_, select

from helpdesk import clock
from helpdesk.db import queries
from helpdesk.db.models import Staff, Task, TaskCard, TaskComment, TaskEvent, TaskLink
from helpdesk.delivery import outbox
from helpdesk.delivery.outbox import Buttons
from helpdesk.tasks import render, rules
from helpdesk.tickets.service import TG_FILE_KIND


class NoCoordinator(Exception):
    """Задача типа «Координатор», а активных координаторов нет."""


def today() -> date:
    return clock.local(clock.utcnow()).date()


async def can_help(s, t: Task, staff: Staff) -> bool:
    return any(st.id != staff.id for st in await rules.eligible(s, t.kind))


async def _card_parts(s, t: Task, viewer: Staff, names) -> tuple[str, Buttons | None]:
    help_ok = viewer.id == t.assignee_id and await can_help(s, t, viewer)
    return render.card(t, names, today()), render.buttons_for(t, viewer, can_help=help_ok)


async def _send_card(s, t: Task, st: Staff, names, *, header=None, with_file=True) -> None:
    if with_file and t.file_path:
        outbox.tg_send(
            s,
            st.tg_chat_id,
            text=f"📎 {render.number(t)}",
            file_path=t.file_path,
            file_kind=t.file_kind,
            task_id=t.id,
        )
    text, buttons = await _card_parts(s, t, st, names)
    if header:
        text = f"{header}\n{text}"
    outbox.tg_send(
        s, st.tg_chat_id, text=text, buttons=buttons, task_id=t.id, task_card_staff_id=st.id
    )


async def _push(s, t: Task) -> None:
    """Перерисовать все карточки задачи у всех участников."""
    names = await queries.staff_names(s)
    for st in await rules.participants(s, t):
        text, buttons = await _card_parts(s, t, st, names)
        outbox.tg_task_edit(
            s, st.tg_chat_id, task_id=t.id, staff_id=st.id, text=text, buttons=buttons
        )


def notify(s, t: Task, people, text: str, *, skip_id: int | None = None) -> None:
    sent: set[int] = set()
    for st in people:
        if st is None or st.status != "active" or st.id == skip_id or st.id in sent:
            continue
        sent.add(st.id)
        outbox.tg_send(s, st.tg_chat_id, text=text, task_id=t.id)


async def create(
    s,
    creator: Staff,
    *,
    kind: str,
    description: str,
    requester: str | None = None,
    due_date: date | None = None,
    file_path: str | None = None,
    file_name: str | None = None,
    file_kind: str | None = None,
) -> Task:
    if kind not in rules.KINDS:
        raise ValueError(kind)
    mine = rules.can_do(creator, kind)
    if not mine and not await rules.eligible(s, kind):
        raise NoCoordinator()
    t = Task(
        kind=kind,
        status="in_progress" if mine else "queued",
        description=description,
        requester=requester,
        due_date=due_date,
        file_path=file_path,
        file_name=file_name,
        file_kind=file_kind,
        creator_id=creator.id,
        assignee_id=creator.id if mine else None,
        taken_at=clock.utcnow() if mine else None,
    )
    s.add(t)
    await s.flush()
    s.add(TaskEvent(task_id=t.id, type="created", staff_id=creator.id))
    names = await queries.staff_names(s)
    for st in await rules.participants(s, t):
        await _send_card(s, t, st, names)
    return t


@dataclass
class TakeResult:
    ok: bool
    holder_name: str | None


async def _locked(s, task_id: int) -> Task | None:
    return await s.get(Task, task_id, with_for_update=True, populate_existing=True)


async def _last_helper(s, task_id: int) -> int | None:
    q = (
        select(TaskEvent.staff_id)
        .where(TaskEvent.task_id == task_id, TaskEvent.type.in_(("help", "released")))
        .order_by(TaskEvent.id.desc())
        .limit(1)
    )
    return await s.scalar(q)


async def take(s, task_id: int, staff: Staff) -> TakeResult:
    t = await _locked(s, task_id)
    if t is None or t.status == "closed" or not rules.can_do(staff, t.kind):
        return TakeResult(False, None)
    if t.status != "queued":
        holder = await s.get(Staff, t.assignee_id) if t.assignee_id else None
        return TakeResult(False, holder.name if holder else None)
    first = t.taken_at is None
    helper_id = await _last_helper(s, t.id)
    t.status, t.assignee_id, t.taken_at = "in_progress", staff.id, clock.utcnow()
    s.add(TaskEvent(task_id=t.id, type="accepted" if first else "taken", staff_id=staff.id))
    await s.flush()
    n = render.number(t)
    text = f"✅ {staff.name} принял(а) {n}" if first else f"🤝 {n} взял(а) {staff.name}"
    people = [await s.get(Staff, t.creator_id)]
    if helper_id:
        people.append(await s.get(Staff, helper_id))
    notify(s, t, people, text, skip_id=staff.id)
    await _push(s, t)
    return TakeResult(True, staff.name)


async def _to_queue(s, t: Task, *, event: str, staff_id: int, header: str, recipients) -> None:
    t.status, t.assignee_id = "queued", None
    s.add(TaskEvent(task_id=t.id, type=event, staff_id=staff_id))
    await s.flush()
    await _push(s, t)
    names = await queries.staff_names(s)
    for st in recipients:
        await _send_card(s, t, st, names, header=header, with_file=False)


async def _on_shift_others(s, t: Task, staff_id: int) -> list[Staff]:
    return [st for st in await rules.eligible(s, t.kind) if st.id != staff_id and st.on_shift]


async def ask_help(s, task_id: int, staff: Staff) -> str:
    t = await _locked(s, task_id)
    if t is None or t.status != "in_progress" or t.assignee_id != staff.id:
        return "denied"
    others = await _on_shift_others(s, t, staff.id)
    if not others:
        return "nobody"
    await _to_queue(
        s,
        t,
        event="help",
        staff_id=staff.id,
        header=f"🆘 {staff.name} просит помощи",
        recipients=others,
    )
    return "ok"


async def release(s, task_id: int, former: Staff) -> bool:
    """Исполнителя отключили — задача уходит в очередь. В событии staff_id = бывший исполнитель."""
    t = await _locked(s, task_id)
    if t is None or t.status != "in_progress" or t.assignee_id != former.id:
        return False
    await _to_queue(
        s,
        t,
        event="released",
        staff_id=former.id,
        header=f"🆘 {former.name} отключён — задаче нужен исполнитель",
        recipients=await _on_shift_others(s, t, former.id),
    )
    return True


async def close(s, task_id: int, staff: Staff) -> bool:
    t = await _locked(s, task_id)
    if t is None or t.status == "closed":
        return False
    if not (staff.is_admin or staff.id in (t.assignee_id, t.creator_id)):
        return False
    t.status, t.closed_at, t.closed_by_id = "closed", clock.utcnow(), staff.id
    s.add(TaskEvent(task_id=t.id, type="closed", staff_id=staff.id))
    await s.flush()
    await _push(s, t)
    text = f"✅ {render.number(t)} закрыта ({staff.name})"
    notify(s, t, await rules.participants(s, t), text, skip_id=staff.id)
    return True


async def comment(
    s,
    t: Task,
    staff: Staff,
    *,
    kind: str,
    text: str | None,
    file_path: str | None = None,
    file_name: str | None = None,
) -> TaskComment | None:
    if t.status == "closed":
        return None
    c = TaskComment(
        task_id=t.id,
        staff_id=staff.id,
        kind=kind,
        text=text,
        file_path=file_path,
        file_name=file_name,
    )
    s.add(c)
    head = f"💬 {render.number(t)} · {staff.name}"
    body = f"{head}: {text}" if text else head
    for st in await rules.participants(s, t):
        if st.id == staff.id:
            continue
        if file_path:
            outbox.tg_send(
                s,
                st.tg_chat_id,
                text=body,
                file_path=file_path,
                file_kind=TG_FILE_KIND[kind],
                task_id=t.id,
            )
        else:
            outbox.tg_send(s, st.tg_chat_id, text=body, task_id=t.id)
    await s.flush()
    return c


async def task_for_reply(s, chat_id: int, message_id: int) -> Task | None:
    link_ = await s.get(TaskLink, (chat_id, message_id))
    return await s.get(Task, link_.task_id) if link_ else None


async def link(s, chat_id: int, message_id: int, task_id: int) -> None:
    await s.merge(TaskLink(tg_chat_id=chat_id, tg_message_id=message_id, task_id=task_id))


_BY_DUE = (Task.due_date.asc().nulls_last(), Task.id)


async def my_tasks(s, staff: Staff) -> list[Task]:
    q = (
        select(Task)
        .where(Task.status == "in_progress", Task.assignee_id == staff.id)
        .order_by(*_BY_DUE)
    )
    return list((await s.scalars(q)).all())


async def queued_for(s, staff: Staff) -> list[Task]:
    q = select(Task).where(Task.status == "queued").order_by(*_BY_DUE)
    return [t for t in (await s.scalars(q)).all() if rules.can_do(staff, t.kind)]


async def deliver_queue_to(s, staff: Staff) -> int:
    carded = set(await s.scalars(select(TaskCard.task_id).where(TaskCard.staff_id == staff.id)))
    rows = [t for t in await queued_for(s, staff) if t.id not in carded]
    names = await queries.staff_names(s)
    for t in rows:
        await _send_card(s, t, staff, names, header="🆘 Задаче нужен исполнитель", with_file=False)
    return len(rows)


async def open_tasks(s, staff: Staff) -> list[Task]:
    q = select(Task).where(Task.status != "closed", rules.visible_cond(staff)).order_by(Task.id)
    return list((await s.scalars(q)).all())


async def search(s, staff: Staff, q: str, limit: int = 10) -> list[Task]:
    like = f"%{q.strip()}%"
    query = (
        select(Task)
        .where(
            or_(Task.description.ilike(like), Task.requester.ilike(like)),
            rules.visible_cond(staff),
        )
        .order_by(Task.id.desc())
        .limit(limit)
    )
    return list((await s.scalars(query)).all())


async def get_visible(s, staff: Staff, task_id: int) -> Task | None:
    t = await s.get(Task, task_id)
    return t if t is not None and rules.can_see(staff, t) else None


async def recent_comments(s, task_id: int, limit: int = 5) -> list[TaskComment]:
    q = (
        select(TaskComment)
        .where(TaskComment.task_id == task_id)
        .order_by(TaskComment.id.desc())
        .limit(limit)
    )
    return list(reversed((await s.scalars(q)).all()))
