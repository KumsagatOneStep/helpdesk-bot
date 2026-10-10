from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, Update
from sqlalchemy import select

from helpdesk.db.models import Task
from helpdesk.telegram import menu, task_handlers
from helpdesk.telegram.task_handlers import TaskForm
from tests.factories import make_staff, make_task
from tests.fakes import FakeTranscriber


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


async def test_card_buttons(s, clock_):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    tima = await make_staff(s, tg_id=2, name="Тимур")
    t = await make_task(s, erlan)
    cq = Cq(f"tk_help:{t.id}")
    await task_handlers.cb_task_help(cq, s, erlan)
    assert cq.alerts == ["Попросили помощи — карточка ушла коллегам на смене"]
    cq = Cq(f"tk_take:{t.id}")
    await task_handlers.cb_task_take(cq, s, tima)
    assert cq.alerts == ["Задача ваша ✅"]
    cq = Cq(f"tk_take:{t.id}")
    await task_handlers.cb_task_take(cq, s, erlan)
    assert cq.alerts == ["Уже взял(а) Тимур"]
    cq = Cq(f"tk_close:{t.id}")
    await task_handlers.cb_task_close(cq, s, tima)
    assert cq.alerts == ["Закрыта ✅"]
    cq = Cq(f"tk_close:{t.id}")
    await task_handlers.cb_task_close(cq, s, tima)
    assert cq.alerts == ["Уже закрыта или нет прав"]


async def _matched_names(msg: Message, raw_state: str, staff) -> list[str]:
    out = []
    for h in task_handlers.router.message.handlers:
        ok, _ = await h.check(msg, staff=staff, raw_state=raw_state)
        if ok:
            out.append(h.callback.__name__)
    return out


async def test_menu_input_in_dialog_only_matched_by_leave_dialog():
    staff = SimpleNamespace(status="active", is_admin=False)
    photo = [{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}]
    for st in TaskForm.__all_states__:
        for text in (menu.MY, "/my", "/on"):
            assert await _matched_names(_message(text=text), st.state, staff) == ["leave_dialog"], (
                st,
                text,
            )
        assert await _matched_names(_message(text="что-то"), st.state, staff), st
        assert await _matched_names(_message(photo=photo), st.state, staff), st
        assert await _matched_names(_message(text=menu.CANCEL), st.state, staff) == [], st


async def test_menu_button_in_dialog_reaches_main_router(s, clock_):
    staff = await make_staff(s, tg_id=1, name="Ерлан")
    seen = []
    probe = Router()

    @probe.message(F.text == menu.MY)
    async def my(message: Message):
        seen.append(message.text)

    dp = Dispatcher()
    dp.include_routers(task_handlers.router, probe)
    try:
        bot = Bot("123:abc")
        key = StorageKey(bot_id=123, chat_id=1, user_id=1)
        await dp.fsm.storage.set_state(key, TaskForm.description.state)
        upd = Update.model_validate(
            {"update_id": 1, "message": _message(text=menu.MY).model_dump(by_alias=True)}
        )
        await dp.feed_update(bot, upd, staff=staff)
        assert seen == [menu.MY]
        assert await dp.fsm.storage.get_state(key) is None
    finally:
        task_handlers.router._parent_router = None


class _Progress:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))


class _VoiceMsg(Msg):
    def __init__(self, duration=5):
        super().__init__(voice=SimpleNamespace(duration=duration, file_size=10, file_unique_id="v"))
        self.progress = []

    async def answer(self, text, **kw):
        self.answers.append(text)
        p = _Progress()
        self.progress.append(p)
        return p


class _DlBot:
    async def download(self, obj, destination):
        Path(destination).write_bytes(b"ogg")


def _vs(tmp_path):
    return SimpleNamespace(files_dir=str(tmp_path), stt_max_seconds=120)


def _kb(markup_):
    if markup_ is None:
        return None
    return [[(b.text, b.callback_data) for b in row] for row in markup_.inline_keyboard]


async def test_voice_description_confirm_and_create(s, clock_, state, tmp_path):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    await state.set_state(TaskForm.description)
    await state.update_data(kind="helpdesk")
    m = _VoiceMsg()
    await task_handlers.got_voice(
        m, state, _DlBot(), _vs(tmp_path), FakeTranscriber("Заменить картридж")
    )
    assert await state.get_state() == TaskForm.description_confirm.state
    text, kb = m.progress[0].edits[-1]
    assert text == "🎤 Распознано:\n«Заменить картридж»"
    assert _kb(kb) == [[("✅ Подходит", "tkn_stt_ok"), ("✏️ Исправить", "tkn_stt_edit")]]
    await task_handlers.stt_ok(Cq("tkn_stt_ok"), state)
    assert await state.get_state() == TaskForm.requester.state
    data = await state.get_data()
    assert data["description"] == "Заменить картридж"
    assert "file_path" not in data  # голосовое не прикрепляется — только текст
    assert list(tmp_path.rglob("*.ogg")) == []  # скачанный файл удалён
    await state.update_data(requester=None, due=None)
    await state.set_state(TaskForm.confirm)
    await task_handlers.confirm_create(Cq("tkn_ok"), state, s, erlan)
    t = await s.scalar(select(Task))
    assert (t.description, t.file_kind, t.file_path) == ("Заменить картридж", None, None)


async def test_failed_voice_file_removed(state, tmp_path, clock_):
    await state.set_state(TaskForm.description)
    await task_handlers.got_voice(
        _VoiceMsg(), state, _DlBot(), _vs(tmp_path), FakeTranscriber(None)
    )
    assert list(tmp_path.rglob("*.ogg")) == []


async def test_voice_edit_goes_back(state, tmp_path, clock_):
    await state.set_state(TaskForm.description)
    await task_handlers.got_voice(_VoiceMsg(), state, _DlBot(), _vs(tmp_path), FakeTranscriber("x"))
    cq = Cq("tkn_stt_edit")
    await task_handlers.stt_edit(cq, state)
    assert await state.get_state() == TaskForm.description.state
    assert cq.message.answers == ["Отправьте описание текстом или запишите голосовое заново."]


async def test_text_in_confirm_state_is_description(state, clock_):
    await state.set_state(TaskForm.description_confirm)
    await state.update_data(stt_text="плохо распознано")
    await task_handlers.got_description(Msg("Правильный текст"), state)
    assert (await state.get_data())["description"] == "Правильный текст"
    assert await state.get_state() == TaskForm.requester.state


async def test_voice_failed_stays_on_description(state, tmp_path, clock_):
    await state.set_state(TaskForm.description)
    m = _VoiceMsg()
    await task_handlers.got_voice(m, state, _DlBot(), _vs(tmp_path), FakeTranscriber(None))
    assert await state.get_state() == TaskForm.description.state
    assert m.progress[0].edits[-1] == (
        "Не удалось распознать — напишите текстом или запишите ещё раз.",
        None,
    )


async def test_voice_too_long_and_disabled(state, tmp_path, clock_):
    await state.set_state(TaskForm.description)
    long = _VoiceMsg(duration=200)
    await task_handlers.got_voice(long, state, _DlBot(), _vs(tmp_path), FakeTranscriber("x"))
    assert long.answers == ["Голосовое длиннее 120 секунд — напишите текстом."]
    off = _VoiceMsg()
    await task_handlers.got_voice(
        off, state, _DlBot(), _vs(tmp_path), FakeTranscriber("x", enabled=False)
    )
    assert off.answers == ["Распознавание выключено — напишите текстом."]
    assert await state.get_state() == TaskForm.description.state


async def test_voice_keeps_existing_photo_attachment(state, tmp_path, clock_):
    await state.set_state(TaskForm.description)
    await state.update_data(file_path="/f/p.jpg", file_name="photo_p.jpg", file_kind="photo")
    await task_handlers.got_voice(_VoiceMsg(), state, _DlBot(), _vs(tmp_path), FakeTranscriber("x"))
    data = await state.get_data()
    assert (data["file_kind"], data["file_name"]) == ("photo", "photo_p.jpg")


async def test_voice_text_clipped_to_limit(state, tmp_path, clock_):
    await state.set_state(TaskForm.description)
    m = _VoiceMsg()
    await task_handlers.got_voice(m, state, _DlBot(), _vs(tmp_path), FakeTranscriber("я" * 2100))
    assert len((await state.get_data())["stt_text"]) == 2000
    assert m.progress[0].edits[-1][0].endswith("(обрезано до 2000 символов)")


async def test_voice_in_description_handled_by_got_voice():
    staff = SimpleNamespace(status="active", is_admin=False)
    v = {"file_id": "f", "file_unique_id": "u", "duration": 3}
    for st in (TaskForm.description, TaskForm.description_confirm):
        names = await _matched_names(_message(voice=v), st.state, staff)
        assert names and names[0] == "got_voice", (st, names)


async def test_stt_buttons_outside_step_are_stale():
    cq = Cq("tkn_stt_ok")
    await task_handlers.stale_dialog(cq)
    assert cq.alerts == ["Диалог уже завершён"]
