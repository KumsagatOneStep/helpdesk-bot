from datetime import date

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
