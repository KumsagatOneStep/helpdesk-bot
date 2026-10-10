from datetime import date

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from helpdesk.config import Settings
from helpdesk.db.models import Staff
from helpdesk.tasks import render
from helpdesk.tasks import service as tasks
from helpdesk.telegram import menu, voice
from helpdesk.telegram.filters import ID_DATA, IsActive, arg_id
from helpdesk.telegram.gateway import markup
from helpdesk.telegram.media import FileTooBig, extract_content
from helpdesk.tickets.service import TG_FILE_KIND

router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


class TaskForm(StatesGroup):
    kind = State()
    description = State()
    description_confirm = State()
    requester = State()
    due = State()
    due_custom = State()
    confirm = State()


KIND_BUTTONS = [[("Helpdesk", "tkn_kind:helpdesk"), ("🧭 Координатор", "tkn_kind:coordinator")]]
SKIP_BUTTONS = [[("Пропустить", "tkn_skip")]]
DUE_BUTTONS = [
    [("Сегодня", "tkn_due:today"), ("Завтра", "tkn_due:tomorrow")],
    [("Через неделю", "tkn_due:week"), ("Своя дата", "tkn_due:custom")],
    [("Без срока", "tkn_due:none")],
]
CONFIRM_BUTTONS = [[("✅ Создать", "tkn_ok"), ("Отмена", "tkn_cancel")]]
STT_BUTTONS = [[("✅ Подходит", "tkn_stt_ok"), ("✏️ Исправить", "tkn_stt_edit")]]
NOT_CANCEL = F.text != menu.CANCEL
NOT_LEAVING = ~F.text.func(lambda text: leaves_dialog(text))


def leaves_dialog(text: str | None) -> bool:
    """Команда или кнопка меню посреди диалога: диалог завершаем, кнопку обрабатываем как обычно."""
    if not text:
        return False
    return text.startswith("/") or (
        text in menu.MENU_TEXTS and text not in (menu.CANCEL, menu.NEW_TASK)
    )


async def _drop_markup(cq: CallbackQuery) -> None:
    try:
        await cq.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


@router.message(F.text == menu.NEW_TASK, IsActive())
async def new_task(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(TaskForm.kind)
    await message.answer(
        "➕ Новая задача. Отменить — кнопка «✖️ Отмена».", reply_markup=menu.cancel_menu()
    )
    await message.answer("Тип задачи:", reply_markup=markup(KIND_BUTTONS))


@router.message(StateFilter(TaskForm), F.text.func(leaves_dialog))
async def leave_dialog(message: Message, state: FSMContext):
    await state.clear()
    raise SkipHandler()


@router.callback_query(
    TaskForm.kind, F.data.regexp(r"^tkn_kind:(helpdesk|coordinator)$"), IsActive()
)
async def pick_kind(cq: CallbackQuery, state: FSMContext):
    kind = cq.data.split(":", 1)[1]
    await state.update_data(kind=kind)
    await state.set_state(TaskForm.description)
    await cq.message.edit_text(f"Тип: {render.KIND_RU[kind]}")
    await cq.message.answer(
        "Опишите задачу текстом или голосовым. Можно прислать фото или файл с подписью."
    )
    await cq.answer()


async def _ask_requester(message, state: FSMContext) -> None:
    await state.set_state(TaskForm.requester)
    await message.answer(
        "Для кого задача? ФИО или кабинет — или «Пропустить».", reply_markup=markup(SKIP_BUTTONS)
    )


def _too_long(text: str) -> str | None:
    if len(text) > render.MAX_DESCRIPTION:
        return (
            f"Слишком длинно ({len(text)} символов), максимум {render.MAX_DESCRIPTION}. Сократите."
        )
    return None


@router.message(
    StateFilter(TaskForm.description, TaskForm.description_confirm),
    IsActive(),
    F.text.func(menu.is_client_reply_text),
)
async def got_description(message: Message, state: FSMContext):
    text = message.text.strip()
    if err := _too_long(text):
        await message.answer(err)
        return
    await state.update_data(description=text)
    await _ask_requester(message, state)


@router.message(TaskForm.description, IsActive(), F.photo | F.document)
async def got_attachment(message: Message, state: FSMContext, bot, settings: Settings):
    try:
        content = await extract_content(bot, message, settings.files_dir)
    except FileTooBig:
        await message.answer("Файл больше 20 МБ — бот не может его скачать.")
        return
    await state.update_data(
        file_path=content.file_path,
        file_name=content.file_name,
        file_kind=TG_FILE_KIND[content.kind],
    )
    caption = (content.text or "").strip()
    if not caption:
        await message.answer("Файл сохранён. Теперь опишите задачу текстом.")
        return
    if err := _too_long(caption):
        await message.answer(f"{err}\nФайл сохранён — пришлите описание текстом.")
        return
    await state.update_data(description=caption)
    await _ask_requester(message, state)


@router.message(
    StateFilter(TaskForm.description, TaskForm.description_confirm),
    IsActive(),
    F.voice | F.audio,
)
async def got_voice(message: Message, state: FSMContext, bot, settings: Settings, stt):
    try:
        r = await voice.recognize(message, bot, settings, stt, keep_file=False)
    except FileTooBig:
        await message.answer("Файл больше 20 МБ — бот не может его скачать.")
        return
    await state.set_state(TaskForm.description)
    if r.status == "disabled":
        await message.answer("Распознавание выключено — напишите текстом.")
        return
    if r.status == "too_long":
        await message.answer(
            f"Голосовое длиннее {settings.stt_max_seconds} секунд — напишите текстом."
        )
        return
    if r.status == "failed":
        await r.progress.edit_text("Не удалось распознать — напишите текстом или запишите ещё раз.")
        return
    if not (await state.get_data()).get("file_path"):
        await state.update_data(
            file_path=r.content.file_path, file_name=r.content.file_name, file_kind="voice"
        )
    text, note = r.text, ""
    if len(text) > render.MAX_DESCRIPTION:
        text = text[: render.MAX_DESCRIPTION]
        note = f"\n(обрезано до {render.MAX_DESCRIPTION} символов)"
    await state.update_data(stt_text=text)
    await state.set_state(TaskForm.description_confirm)
    await r.progress.edit_text(f"🎤 Распознано:\n«{text}»{note}", reply_markup=markup(STT_BUTTONS))


@router.callback_query(TaskForm.description_confirm, F.data == "tkn_stt_ok", IsActive())
async def stt_ok(cq: CallbackQuery, state: FSMContext):
    await state.update_data(description=(await state.get_data())["stt_text"])
    await _drop_markup(cq)
    await _ask_requester(cq.message, state)
    await cq.answer()


@router.callback_query(TaskForm.description_confirm, F.data == "tkn_stt_edit", IsActive())
async def stt_edit(cq: CallbackQuery, state: FSMContext):
    await state.set_state(TaskForm.description)
    await _drop_markup(cq)
    await cq.message.answer("Отправьте описание текстом или запишите голосовое заново.")
    await cq.answer()


@router.message(TaskForm.description, IsActive(), NOT_CANCEL, NOT_LEAVING)
async def bad_description(message: Message):
    await message.answer("Пришлите описание текстом, фото или файлом.")


async def _ask_due(message, state: FSMContext) -> None:
    await state.set_state(TaskForm.due)
    await message.answer("Срок:", reply_markup=markup(DUE_BUTTONS))


@router.message(TaskForm.requester, IsActive(), F.text.func(menu.is_client_reply_text))
async def got_requester(message: Message, state: FSMContext):
    text = message.text.strip()
    if len(text) > render.MAX_REQUESTER:
        await message.answer(f"Не длиннее {render.MAX_REQUESTER} символов.")
        return
    await state.update_data(requester=text or None)
    await _ask_due(message, state)


@router.callback_query(TaskForm.requester, F.data == "tkn_skip", IsActive())
async def skip_requester(cq: CallbackQuery, state: FSMContext):
    await state.update_data(requester=None)
    await _drop_markup(cq)
    await _ask_due(cq.message, state)
    await cq.answer()


async def _confirm(message, state: FSMContext) -> None:
    await state.set_state(TaskForm.confirm)
    await message.answer(
        render.summary(await state.get_data()), reply_markup=markup(CONFIRM_BUTTONS)
    )


@router.callback_query(
    TaskForm.due, F.data.regexp(r"^tkn_due:(today|tomorrow|week|custom|none)$"), IsActive()
)
async def pick_due(cq: CallbackQuery, state: FSMContext):
    code = cq.data.split(":", 1)[1]
    await _drop_markup(cq)
    if code == "custom":
        await state.set_state(TaskForm.due_custom)
        await cq.message.answer("Введите дату: ДД.ММ или ДД.ММ.ГГГГ")
        await cq.answer()
        return
    due = None if code == "none" else render.due_from_choice(code, tasks.today())
    await state.update_data(due=due.isoformat() if due else None)
    await _confirm(cq.message, state)
    await cq.answer()


@router.message(TaskForm.due_custom, IsActive(), F.text.func(menu.is_client_reply_text))
async def got_custom_due(message: Message, state: FSMContext):
    due = render.parse_due(message.text, tasks.today())
    if due is None:
        await message.answer("Не понял дату или она в прошлом. Пример: 15.10 или 15.10.2026")
        return
    await state.update_data(due=due.isoformat())
    await _confirm(message, state)


@router.callback_query(TaskForm.confirm, F.data == "tkn_ok", IsActive())
async def confirm_create(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    data = await state.get_data()
    await state.clear()
    await _drop_markup(cq)
    try:
        t = await tasks.create(
            s,
            staff,
            kind=data["kind"],
            description=data["description"],
            requester=data.get("requester"),
            due_date=date.fromisoformat(data["due"]) if data.get("due") else None,
            file_path=data.get("file_path"),
            file_name=data.get("file_name"),
            file_kind=data.get("file_kind"),
        )
    except tasks.NoCoordinator:
        await cq.message.answer(
            "Координатор не назначен. Обратитесь к админу (/staff).",
            reply_markup=menu.main_menu(staff),
        )
        await cq.answer()
        return
    await cq.message.answer(
        f"📌 Задача {render.number(t)} создана.", reply_markup=menu.main_menu(staff)
    )
    await cq.answer()


@router.callback_query(StateFilter(TaskForm), F.data == "tkn_cancel", IsActive())
async def cancel_create(cq: CallbackQuery, state: FSMContext, staff: Staff):
    await state.clear()
    await _drop_markup(cq)
    await cq.message.answer("Отменено.", reply_markup=menu.main_menu(staff))
    await cq.answer()


@router.message(StateFilter(TaskForm), IsActive(), NOT_CANCEL, NOT_LEAVING)
async def stray_input(message: Message):
    """Лишний ввод посреди диалога не должен уйти клиенту как ответ."""
    await message.answer("Используйте кнопки выше или «✖️ Отмена».")


HELP_ANSWERS = {
    "ok": ("Попросили помощи — карточка ушла коллегам на смене", False),
    "nobody": ("Сейчас никого на смене — задача осталась за вами", True),
    "denied": ("Задача уже не у вас", True),
}


@router.callback_query(F.data.regexp(ID_DATA.format("tk_take")), IsActive())
async def cb_task_take(cq: CallbackQuery, s, staff: Staff):
    r = await tasks.take(s, arg_id(cq), staff)
    if r.ok:
        await cq.answer("Задача ваша ✅")
    else:
        text = f"Уже взял(а) {r.holder_name}" if r.holder_name else "Задача недоступна"
        await cq.answer(text, show_alert=True)


@router.callback_query(F.data.regexp(ID_DATA.format("tk_help")), IsActive())
async def cb_task_help(cq: CallbackQuery, s, staff: Staff):
    text, alert = HELP_ANSWERS[await tasks.ask_help(s, arg_id(cq), staff)]
    await cq.answer(text, show_alert=alert)


@router.callback_query(F.data.regexp(ID_DATA.format("tk_close")), IsActive())
async def cb_task_close(cq: CallbackQuery, s, staff: Staff):
    if await tasks.close(s, arg_id(cq), staff):
        await cq.answer("Закрыта ✅")
    else:
        await cq.answer("Уже закрыта или нет прав", show_alert=True)


@router.callback_query(F.data.startswith("tkn_"))
async def stale_dialog(cq: CallbackQuery):
    await cq.answer("Диалог уже завершён", show_alert=True)
