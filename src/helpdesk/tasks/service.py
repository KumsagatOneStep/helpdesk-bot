from dataclasses import dataclass
from datetime import date

from sqlalchemy import select

from helpdesk import clock
from helpdesk.db import queries
from helpdesk.db.models import Staff, Task, TaskEvent
from helpdesk.delivery import outbox
from helpdesk.delivery.outbox import Buttons
from helpdesk.tasks import render, rules


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
