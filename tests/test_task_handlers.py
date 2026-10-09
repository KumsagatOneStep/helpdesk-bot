from datetime import date
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from sqlalchemy import select

from helpdesk.db.models import Task
from helpdesk.telegram import menu, task_handlers
from helpdesk.telegram.task_handlers import TaskForm
from tests.factories import make_staff


class Msg:
    def __init__(self, text=None, **kw):
        self.text = text
        self.caption = kw.get("caption")
        for k in ("photo", "document", "voice", "audio", "video", "video_note"):
            setattr(self, k, kw.get(k))
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)
        return SimpleNamespace(message_id=1)

    async def edit_text(self, text, **kw):
        self.answers.append(text)

    async def edit_reply_markup(self, **kw):
        pass


class Cq:
    def __init__(self, data):
        self.data = data
        self.message = Msg()
        self.alerts = []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append(text)


@pytest.fixture
def state():
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))


async def test_full_dialog_creates_task(s, clock_, state):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    await task_handlers.new_task(Msg(menu.NEW_TASK), state)
    assert await state.get_state() == TaskForm.kind.state
    await task_handlers.pick_kind(Cq("tkn_kind:helpdesk"), state)
    m = Msg("Заменить картридж")
    await task_handlers.got_description(m, state)
    assert await state.get_state() == TaskForm.requester.state
    await task_handlers.skip_requester(Cq("tkn_skip"), state)
    await task_handlers.pick_due(Cq("tkn_due:custom"), state)
    bad = Msg("вчера")
    await task_handlers.got_custom_due(bad, state)
    assert "Не понял дату" in bad.answers[0]
    ok = Msg("10.10")
    await task_handlers.got_custom_due(ok, state)
    assert ok.answers[0].startswith("Проверьте задачу:") and "Срок: 10.10.2026" in ok.answers[0]
    cq = Cq("tkn_ok")
    await task_handlers.confirm_create(cq, state, s, erlan)
    t = await s.scalar(select(Task))
    assert (t.kind, t.description, t.requester, t.due_date) == (
        "helpdesk",
        "Заменить картридж",
        None,
        date(2026, 10, 10),
    )
    assert cq.message.answers == [f"📌 Задача З-{t.id} создана."]
    assert await state.get_state() is None


async def test_description_limits_and_photo_without_caption(s, clock_, state, tmp_path):
    await state.set_state(TaskForm.description)
    long = Msg("x" * 2001)
    await task_handlers.got_description(long, state)
    assert "максимум 2000" in long.answers[0]
    assert await state.get_state() == TaskForm.description.state

    class Bot:
        async def download(self, obj, destination):
            destination.write_bytes(b"img")

    photo = SimpleNamespace(file_unique_id="p", file_size=10)
    m = Msg(photo=[photo])
    await task_handlers.got_attachment(m, state, Bot(), SimpleNamespace(files_dir=str(tmp_path)))
    assert m.answers == ["Файл сохранён. Теперь опишите задачу текстом."]
    data = await state.get_data()
    assert data["file_kind"] == "photo" and data["file_name"] == "photo_p.jpg"
    assert await state.get_state() == TaskForm.description.state


async def test_coordinator_kind_without_coordinator(s, clock_, state):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    await state.set_state(TaskForm.confirm)
    await state.update_data(kind="coordinator", description="Ноутбук", due=None)
    cq = Cq("tkn_ok")
    await task_handlers.confirm_create(cq, state, s, erlan)
    assert cq.message.answers == ["Координатор не назначен. Обратитесь к админу (/staff)."]
    assert await s.scalar(select(Task)) is None


def test_menu_text_leaves_dialog_but_cancel_does_not_match():
    f = task_handlers.leaves_dialog
    assert f(menu.MY) and f(menu.SEARCH) and f("/my")
    assert not f(menu.CANCEL) and not f(menu.NEW_TASK) and not f("Заменить картридж")
    assert not f(None)


async def test_stale_dialog_button():
    cq = Cq("tkn_due:today")
    await task_handlers.stale_dialog(cq)
    assert cq.alerts == ["Диалог уже завершён"]


def _message(**kw) -> Message:
    return Message.model_validate(
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 1, "is_bot": False, "first_name": "E"},
            **kw,
        }
    )


async def _matched(msg: Message, raw_state: str, staff) -> bool:
    for h in task_handlers.router.message.handlers:
        ok, _ = await h.check(msg, staff=staff, raw_state=raw_state)
        if ok:
            return True
    return False


@pytest.mark.parametrize(
    "st", [TaskForm.kind, TaskForm.requester, TaskForm.due, TaskForm.due_custom, TaskForm.confirm]
)
async def test_stray_input_never_reaches_client_reply(st):
    staff = SimpleNamespace(status="active", is_admin=False)
    assert await _matched(_message(text="опечатка"), st.state, staff)
    photo = {"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}
    assert await _matched(_message(photo=[photo]), st.state, staff)
    assert not await _matched(_message(text=menu.CANCEL), st.state, staff)
