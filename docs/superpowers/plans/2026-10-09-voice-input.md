# Голосовой ввод для задач — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Специалист может надиктовать описание задачи (с подтверждением распознанного текста) и голосовой комментарий к задаче (аудио + расшифровка), распознавание — локальный Whisper.

**Architecture:** Модуль `helpdesk/stt.py` (интерфейс `Transcriber`, `WhisperTranscriber` на `faster-whisper` в отдельном потоке под замком, `DisabledTranscriber`) кладётся в `dp["stt"]`. Общий шаг `telegram/voice.py::recognize` (проверка длительности, скачивание, «⏳ Распознаю…», распознавание) используют диалог создания задачи (новое состояние `description_confirm`) и комментарии (`_comment_task`). БД не меняется.

**Tech Stack:** Python 3.12, aiogram 3.31, faster-whisper (CTranslate2, int8 CPU), SQLAlchemy 2 async, pytest.

**Spec:** `docs/superpowers/specs/2026-10-09-voice-input-design.md`

## Global Constraints

- Ветка `voice-input` (от `internal-tasks`). Перед началом `make test-db && make check` — 237 тестов зелёные; после каждой задачи — тоже.
- Перед каждым `make check`: `uv run ruff format src tests`. Импорты в начале файлов (кроме ленивого импорта `faster_whisper` внутри `WhisperTranscriber._load` — он тяжёлый и не нужен в тестах).
- В тестах настоящая модель Whisper **никогда** не загружается: `FakeTranscriber` из `tests/fakes.py` или monkeypatch `WhisperTranscriber._load`.
- Telegram: без `parse_mode`. Описание ≤ 2000 символов (`render.MAX_DESCRIPTION`).
- Ни один путь голосового ввода не создаёт outbox `wa`.
- Новые `callback_data`: `tkn_stt_ok`, `tkn_stt_edit`.
- Настройки: `STT_ENABLED=true`, `STT_MODEL=small`, `STT_MAX_SECONDS=120`. Модели — в `<родитель FILES_DIR>/models` (в Docker `/data/models`).
- Сообщения коммитов на английском, трейлер `Co-Authored-By:` с моделью, которая писала коммит.

## Review Focus

1. **Голосовое на шаге «Описание» не должно уйти в `stray_input`/`bad_description` или клиенту.** Тест в задаче 3 (`test_voice_in_description_handled_by_got_voice`).
2. **Падение или пустой результат Whisper не роняет обработчик.** `transcribe` возвращает `None`, пользователь видит просьбу написать текстом. Тест в задаче 1 (`test_whisper_exception_returns_none`) и задаче 3 (`test_voice_failed_stays_on_description`).
3. **Голосовое длиннее лимита не скачивается в диалоге**, но в комментарии файл скачивается и уходит без расшифровки. Тесты в задаче 2.
4. **Голос после фото не затирает вложение-фото.** Тест в задаче 3 (`test_voice_keeps_existing_photo_attachment`).
5. **Голосовой комментарий не уходит в WhatsApp**, даже когда у специалиста есть активная заявка. Тест в задаче 4.
6. **Длинная расшифровка не обрезается молча.** Подпись к файлу в Telegram — максимум 1024 символа (`gateway.send` режет `text[:1024]`). Если подпись комментария длиннее, файл уходит с короткой подписью-заголовком, а полный текст — отдельным сообщением следом. Тест в задаче 4 (`test_long_caption_split_into_file_and_text`).
7. **Задачу закрыли, пока шло распознавание** (до минуты) — комментарий не добавляется. После распознавания задача перечитывается из БД. Тест в задаче 4 (`test_task_closed_during_recognition_no_comment`).

---

### Task 1: Настройки и модуль распознавания `stt.py`

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (через `uv add faster-whisper`), `src/helpdesk/config.py`, `.env.example`
- Create: `src/helpdesk/stt.py`
- Modify: `tests/fakes.py`, `tests/test_config.py`
- Test: `tests/test_stt.py`

**Interfaces:**
- Produces: `Settings.stt_enabled: bool = True`, `stt_model: str = "small"`, `stt_max_seconds: int = 120`; `stt.Transcriber` (Protocol: `enabled: bool`, `is_loaded: bool`, `async transcribe(path) -> str | None`); `stt.DisabledTranscriber`, `stt.WhisperTranscriber(model_name, download_root)`, `stt.make_transcriber(settings)`; `tests.fakes.FakeTranscriber(text=None, loaded=True, enabled=True)` с `.calls`.

- [ ] **Step 1: Зависимость**

Run: `uv add faster-whisper` — Expected: `pyproject.toml` и `uv.lock` обновлены.

- [ ] **Step 2: Тесты**

В `tests/test_config.py`, в `test_admin_ids_parsed_and_media_url_defaults`, добавить:

```python
    assert (s.stt_enabled, s.stt_model, s.stt_max_seconds) == (True, "small", 120)
```

В конец `tests/fakes.py`:

```python
class FakeTranscriber:
    def __init__(self, text=None, loaded=True, enabled=True):
        self.text = text
        self.is_loaded = loaded
        self.enabled = enabled
        self.calls: list[str] = []

    async def transcribe(self, path):
        self.calls.append(path)
        self.is_loaded = True
        return self.text
```

Create `tests/test_stt.py`:

```python
from types import SimpleNamespace

from helpdesk import stt


class FakeModel:
    def __init__(self, texts=(), error=None):
        self.texts = texts
        self.error = error
        self.calls = []

    def transcribe(self, path, **kw):
        self.calls.append((path, kw))
        if self.error:
            raise self.error
        return iter(SimpleNamespace(text=t) for t in self.texts), SimpleNamespace()


def _whisper(monkeypatch, model):
    loads = []

    def load(self):
        loads.append(1)
        return model

    monkeypatch.setattr(stt.WhisperTranscriber, "_load", load)
    return stt.WhisperTranscriber("small", "/tmp/models"), loads


async def test_disabled_transcriber():
    d = stt.DisabledTranscriber()
    assert (d.enabled, d.is_loaded) == (False, True)
    assert await d.transcribe("/x.ogg") is None


async def test_whisper_joins_segments_and_loads_once(monkeypatch):
    w, loads = _whisper(monkeypatch, FakeModel([" Заменить картридж", " в 214 "]))
    assert w.enabled and not w.is_loaded
    assert await w.transcribe("/a.ogg") == "Заменить картридж в 214"
    assert w.is_loaded
    assert await w.transcribe("/b.ogg") == "Заменить картридж в 214"
    assert loads == [1]


async def test_whisper_empty_result_is_none(monkeypatch):
    w, _ = _whisper(monkeypatch, FakeModel(["  ", ""]))
    assert await w.transcribe("/a.ogg") is None


async def test_whisper_exception_returns_none(monkeypatch):
    w, _ = _whisper(monkeypatch, FakeModel(error=RuntimeError("bad audio")))
    assert await w.transcribe("/a.ogg") is None


def test_make_transcriber(tmp_path):
    on = SimpleNamespace(stt_enabled=True, stt_model="base", files_dir=str(tmp_path / "files"))
    w = stt.make_transcriber(on)
    assert isinstance(w, stt.WhisperTranscriber)
    assert (w.model_name, w.download_root) == ("base", str(tmp_path / "models"))
    off = SimpleNamespace(stt_enabled=False, stt_model="base", files_dir="/data/files")
    assert isinstance(stt.make_transcriber(off), stt.DisabledTranscriber)
```

- [ ] **Step 3: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_stt.py tests/test_config.py`
Expected: FAIL (`ImportError: cannot import name 'stt'`, нет `stt_enabled`).

- [ ] **Step 4: Настройки**

`src/helpdesk/config.py` после `files_dir`:

```python
    stt_enabled: bool = True
    stt_model: str = "small"
    stt_max_seconds: int = 120
```

`.env.example` в конец:

```
# Распознавание голосовых (локальный Whisper). Модель small — нужно от 4 ГБ RAM.
# base — слабее и быстрее, medium — лучше для казахского, но медленнее (~2.5 ГБ).
STT_ENABLED=true
STT_MODEL=small
# Голосовые длиннее (секунд) не распознаются
STT_MAX_SECONDS=120
```

- [ ] **Step 5: `src/helpdesk/stt.py`**

```python
import asyncio
import os
from pathlib import Path
from typing import Protocol

import structlog

log = structlog.get_logger()


class Transcriber(Protocol):
    enabled: bool
    is_loaded: bool

    async def transcribe(self, path: str) -> str | None: ...


class DisabledTranscriber:
    enabled = False
    is_loaded = True

    async def transcribe(self, path: str) -> str | None:
        return None


class WhisperTranscriber:
    """Локальный Whisper: модель грузится при первом голосовом, распознаём по одному."""

    enabled = True

    def __init__(self, model_name: str, download_root: str):
        self.model_name = model_name
        self.download_root = download_root
        self._model = None
        self._lock = asyncio.Lock()

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def _load(self):
        # у пользователя контейнера нет домашней папки — кэш HF держим рядом с моделями
        os.environ.setdefault("HF_HOME", self.download_root)
        from faster_whisper import WhisperModel  # тяжёлый импорт, нужен только здесь

        return WhisperModel(
            self.model_name, device="cpu", compute_type="int8", download_root=self.download_root
        )

    def _run(self, path: str) -> str:
        if self._model is None:
            self._model = self._load()
        segments, _info = self._model.transcribe(path, vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()

    async def transcribe(self, path: str) -> str | None:
        async with self._lock:
            try:
                text = await asyncio.to_thread(self._run, path)
            except Exception:
                log.exception("stt failed")
                return None
        return text or None


def make_transcriber(settings) -> Transcriber:
    if not settings.stt_enabled:
        return DisabledTranscriber()
    root = str(Path(settings.files_dir).parent / "models")
    return WhisperTranscriber(settings.stt_model, root)
```

Примечание к тесту `test_whisper_joins_segments_and_loads_once`: `_load` подменён, поэтому `self._model` становится `FakeModel` и `" ".join(...)` склеивает `"Заменить картридж"` и `"в 214"`.

- [ ] **Step 6: Проверить и закоммитить**

Run: `uv run ruff format src tests && make check` — Expected: PASS.

```bash
git add pyproject.toml uv.lock src/helpdesk/config.py src/helpdesk/stt.py .env.example tests/fakes.py tests/test_config.py tests/test_stt.py
git commit -m "feat(stt): local Whisper transcriber behind a small interface, STT settings"
```

---

### Task 2: Общий шаг распознавания `telegram/voice.py`

**Files:**
- Create: `src/helpdesk/telegram/voice.py`
- Test: `tests/test_voice.py`

**Interfaces:**
- Consumes: `extract_content(bot, message, files_dir) -> Content` и `FileTooBig` из `telegram/media.py`; `Transcriber` (Task 1).
- Produces: `@dataclass VoiceResult(status: str, content: Content | None, text: str | None, progress)` — `status` ∈ `ok | failed | too_long | disabled`; `progress` — сообщение «⏳ …» (или `None`), которое вызывающий редактирует; `duration(message) -> int`; `async recognize(message, bot, settings, stt, *, keep_file: bool) -> VoiceResult` (пробрасывает `FileTooBig`).

- [ ] **Step 1: Тесты**

Create `tests/test_voice.py`:

```python
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpdesk.telegram import voice
from helpdesk.telegram.media import FileTooBig
from tests.fakes import FakeTranscriber


class Progress:
    def __init__(self, text):
        self.text = text
        self.edits = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))


class VoiceMsg:
    def __init__(self, duration=5, size=10):
        self.text = None
        self.caption = None
        for k in ("photo", "document", "audio", "video", "video_note"):
            setattr(self, k, None)
        self.voice = SimpleNamespace(duration=duration, file_size=size, file_unique_id="v")
        self.sent = []

    async def answer(self, text, **kw):
        p = Progress(text)
        self.sent.append(p)
        return p


class DlBot:
    def __init__(self):
        self.downloads = 0

    async def download(self, obj, destination):
        self.downloads += 1
        Path(destination).write_bytes(b"ogg")


def _settings(tmp_path, max_seconds=120):
    return SimpleNamespace(files_dir=str(tmp_path), stt_max_seconds=max_seconds)


async def test_ok(tmp_path, clock_):
    m, bot, stt = VoiceMsg(), DlBot(), FakeTranscriber("Заменить картридж")
    r = await voice.recognize(m, bot, _settings(tmp_path), stt, keep_file=False)
    assert (r.status, r.text) == ("ok", "Заменить картридж")
    assert r.content.kind == "audio" and r.content.file_name == "voice.ogg"
    assert stt.calls == [r.content.file_path]
    assert [p.text for p in m.sent] == ["⏳ Распознаю…"] and r.progress is m.sent[0]


async def test_first_call_warns_about_model_download(tmp_path, clock_):
    m = VoiceMsg()
    await voice.recognize(m, DlBot(), _settings(tmp_path), FakeTranscriber("x", loaded=False),
                          keep_file=False)
    assert m.sent[0].text == "⏳ Загружаю модель распознавания (один раз)…"


async def test_failed(tmp_path, clock_):
    r = await voice.recognize(VoiceMsg(), DlBot(), _settings(tmp_path), FakeTranscriber(None),
                              keep_file=False)
    assert (r.status, r.text) == ("failed", None) and r.content is not None


async def test_too_long_not_downloaded_in_dialog(tmp_path, clock_):
    m, bot, stt = VoiceMsg(duration=121), DlBot(), FakeTranscriber("x")
    r = await voice.recognize(m, bot, _settings(tmp_path), stt, keep_file=False)
    assert (r.status, r.content, r.progress) == ("too_long", None, None)
    assert bot.downloads == 0 and stt.calls == [] and m.sent == []


async def test_too_long_downloaded_for_comment(tmp_path, clock_):
    bot, stt = DlBot(), FakeTranscriber("x")
    r = await voice.recognize(VoiceMsg(duration=500), bot, _settings(tmp_path), stt,
                              keep_file=True)
    assert r.status == "too_long" and r.content is not None
    assert bot.downloads == 1 and stt.calls == []


async def test_disabled(tmp_path, clock_):
    bot = DlBot()
    r = await voice.recognize(VoiceMsg(), bot, _settings(tmp_path),
                              FakeTranscriber("x", enabled=False), keep_file=False)
    assert (r.status, r.content) == ("disabled", None) and bot.downloads == 0


async def test_file_too_big_propagates(tmp_path, clock_):
    with pytest.raises(FileTooBig):
        await voice.recognize(VoiceMsg(size=30 * 1024 * 1024), DlBot(), _settings(tmp_path),
                              FakeTranscriber("x"), keep_file=False)


def test_duration_reads_voice_or_audio():
    m = VoiceMsg(duration=7)
    assert voice.duration(m) == 7
    m.voice, m.audio = None, SimpleNamespace(duration=None)
    assert voice.duration(m) == 0
```

- [ ] **Step 2: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_voice.py`
Expected: FAIL (`ImportError: cannot import name 'voice'`).

- [ ] **Step 3: `src/helpdesk/telegram/voice.py`**

```python
from dataclasses import dataclass

from helpdesk.telegram.media import Content, extract_content


@dataclass
class VoiceResult:
    status: str  # ok | failed | too_long | disabled
    content: Content | None
    text: str | None
    progress: object | None  # сообщение «⏳ …», вызывающий его редактирует


def duration(message) -> int:
    obj = message.voice or message.audio
    return (obj.duration or 0) if obj else 0


async def recognize(message, bot, settings, stt, *, keep_file: bool) -> VoiceResult:
    """keep_file=True — файл нужен и без расшифровки (комментарий уходит аудио)."""
    too_long = duration(message) > settings.stt_max_seconds
    if too_long or not stt.enabled:
        status = "too_long" if too_long else "disabled"
        content = await extract_content(bot, message, settings.files_dir) if keep_file else None
        return VoiceResult(status, content, None, None)
    content = await extract_content(bot, message, settings.files_dir)
    note = "⏳ Распознаю…" if stt.is_loaded else "⏳ Загружаю модель распознавания (один раз)…"
    progress = await message.answer(note)
    text = await stt.transcribe(content.file_path)
    return VoiceResult("ok" if text else "failed", content, text, progress)
```

- [ ] **Step 4: Проверить и закоммитить**

Run: `uv run ruff format src tests && make check` — Expected: PASS.

```bash
git add src/helpdesk/telegram/voice.py tests/test_voice.py
git commit -m "feat(telegram): shared voice recognition step with length limit and progress message"
```

---

### Task 3: Голос в описании при создании задачи

**Files:**
- Modify: `src/helpdesk/telegram/task_handlers.py`, `src/helpdesk/main.py`
- Test: `tests/test_task_handlers.py`

**Interfaces:**
- Consumes: `voice.recognize`, `VoiceResult` (Task 2); `stt.make_transcriber` (Task 1); `FakeTranscriber`.
- Produces: `TaskForm.description_confirm`; обработчики `got_voice(message, state, bot, settings, stt)`, `stt_ok(cq, state)`, `stt_edit(cq, state)`; `STT_BUTTONS`; `dp["stt"]` в `main.py`.

- [ ] **Step 1: Тесты**

В начало `tests/test_task_handlers.py` добавить импорты: `from pathlib import Path`, `from tests.fakes import FakeTranscriber`. В конец файла:

```python
class _Progress:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))


class _VoiceMsg(Msg):
    def __init__(self, duration=5):
        super().__init__(voice=SimpleNamespace(duration=duration, file_size=10,
                                               file_unique_id="v"))
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
    await task_handlers.got_voice(m, state, _DlBot(), _vs(tmp_path), FakeTranscriber("Заменить картридж"))
    assert await state.get_state() == TaskForm.description_confirm.state
    text, kb = m.progress[0].edits[-1]
    assert text == "🎤 Распознано:\n«Заменить картридж»"
    assert _kb(kb) == [[("✅ Подходит", "tkn_stt_ok"), ("✏️ Исправить", "tkn_stt_edit")]]
    await task_handlers.stt_ok(Cq("tkn_stt_ok"), state)
    assert await state.get_state() == TaskForm.requester.state
    data = await state.get_data()
    assert data["description"] == "Заменить картридж"
    assert data["file_kind"] == "voice" and data["file_name"] == "voice.ogg"
    await state.update_data(requester=None, due=None)
    await state.set_state(TaskForm.confirm)
    await task_handlers.confirm_create(Cq("tkn_ok"), state, s, erlan)
    t = await s.scalar(select(Task))
    assert (t.description, t.file_kind) == ("Заменить картридж", "voice")


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
        "Не удалось распознать — напишите текстом или запишите ещё раз.", None
    )


async def test_voice_too_long_and_disabled(state, tmp_path, clock_):
    await state.set_state(TaskForm.description)
    long = _VoiceMsg(duration=200)
    await task_handlers.got_voice(long, state, _DlBot(), _vs(tmp_path), FakeTranscriber("x"))
    assert long.answers == ["Голосовое длиннее 120 секунд — напишите текстом."]
    off = _VoiceMsg()
    await task_handlers.got_voice(off, state, _DlBot(), _vs(tmp_path),
                                  FakeTranscriber("x", enabled=False))
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
```

(`_message`, `_matched_names`, `Msg`, `Cq`, `state` уже есть в файле. `Cq.message` — это `Msg` с `answer`/`edit_text`/`edit_reply_markup`.)

- [ ] **Step 2: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_task_handlers.py`
Expected: FAIL (`AttributeError: ... 'got_voice'`, нет `description_confirm`).

- [ ] **Step 3: Реализация в `task_handlers.py`**

Импорт: `from helpdesk.telegram import menu, voice`.

`TaskForm` — новое состояние сразу после `description`:

```python
    description_confirm = State()
```

Константа рядом с остальными кнопками:

```python
STT_BUTTONS = [[("✅ Подходит", "tkn_stt_ok"), ("✏️ Исправить", "tkn_stt_edit")]]
```

`got_description` — принимать и в шаге подтверждения:

```python
@router.message(
    StateFilter(TaskForm.description, TaskForm.description_confirm),
    IsActive(),
    F.text.func(menu.is_client_reply_text),
)
async def got_description(message: Message, state: FSMContext):
    ...  # тело без изменений
```

Новые обработчики — **сразу после `got_attachment`** (до `bad_description` и `stray_input`):

```python
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
```

Текст подсказки шага «Описание» в `pick_kind`: `"Опишите задачу текстом или голосовым. Можно прислать фото или файл с подписью."`

- [ ] **Step 4: Transcriber в Dispatcher (`main.py`)**

Импорт `from helpdesk import clock, reminders, stt` и после `dp["settings"] = settings`:

```python
    dp["stt"] = stt.make_transcriber(settings)
```

- [ ] **Step 5: Проверить и закоммитить**

Run: `uv run ruff format src tests && make check` — Expected: PASS (включая существующий `test_menu_input_in_dialog_only_matched_by_leave_dialog`, который теперь проходит и по `description_confirm`).

```bash
git add src/helpdesk/telegram/task_handlers.py src/helpdesk/main.py tests/test_task_handlers.py
git commit -m "feat(tasks): dictate task description by voice with confirm/edit step"
```

---

### Task 4: Голосовые комментарии к задаче

**Files:**
- Modify: `src/helpdesk/tasks/service.py` (`comment(..., voice=False)`), `src/helpdesk/telegram/handlers.py` (`_comment_task`, `_reply_to_client`, `staff_text`, `staff_media`)
- Test: `tests/test_tasks_queries.py`, `tests/test_telegram.py`

**Interfaces:**
- Consumes: `voice.recognize` (Task 2), `FakeTranscriber`.
- Produces: `tasks.comment(s, t, staff, *, kind, text, file_path=None, file_name=None, voice=False)`; `_reply_to_client(message, s, staff, bot, settings, stt=None)`; `_comment_task(message, s, staff, bot, settings, t, stt=None)`.

- [ ] **Step 1: Тесты**

В конец `tests/test_tasks_queries.py`:

```python
async def test_voice_comment_caption(s, clock_):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    await make_staff(s, tg_id=2, name="Тимур")
    t = await make_task(s, erlan)
    await tasks.comment(s, t, erlan, kind="audio", text="Заменил", file_path="/f/v.ogg",
                        file_name="voice.ogg", voice=True)
    [p] = await payloads(s, "tg")
    assert (p["text"], p["file_kind"]) == (f"💬 З-{t.id} · Ерлан (🎤): Заменил", "voice")
    await clear_outbox(s)
    await tasks.comment(s, t, erlan, kind="audio", text=None, file_path="/f/v.ogg",
                        file_name="voice.ogg", voice=True)
    [p] = await payloads(s, "tg")
    assert p["text"] == f"💬 З-{t.id} · Ерлан (🎤)"


async def test_long_caption_split_into_file_and_text(s, clock_):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    await make_staff(s, tg_id=2, name="Тимур")
    t = await make_task(s, erlan)
    long = "я" * 1500
    await tasks.comment(s, t, erlan, kind="audio", text=long, file_path="/f/v.ogg",
                        file_name="voice.ogg", voice=True)
    head = f"💬 З-{t.id} · Ерлан (🎤)"
    assert [(p["file_kind"], p["text"]) for p in await payloads(s, "tg")] == [
        ("voice", head),
        (None, f"{head}: {long}"),
    ]
```

В `tests/test_telegram.py` — импорты в начало: `from tests.fakes import FakeTranscriber`. В конец файла:

```python
class _VoiceChatMessage(FakeChatMessage):
    def __init__(self, chat_id, duration=5, **kw):
        super().__init__(chat_id, voice=SimpleNamespace(duration=duration, file_size=10,
                                                         file_unique_id="v"), **kw)
        self.edits = []

    async def answer(self, text, **kw):
        self.answers.append(text)
        sent = _Sent()
        outer = self

        async def edit_text(t, reply_markup=None, **k):
            outer.edits.append(t)

        sent.edit_text = edit_text
        return sent


async def test_voice_comment_with_transcript_never_whatsapp(s, clock_, tmp_path):
    c = await make_client(s)
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    await make_staff(s, tg_id=7, name="Тимур")
    ticket = await make_ticket(s, c, status="in_progress", assignee=erlan)
    erlan.active_ticket_id = ticket.id
    task = await make_task(s, erlan)
    await tasks_svc.link(s, 6, 3333, task.id)
    await s.flush()
    msg = _VoiceChatMessage(6, message_id=4444, reply_to_message=SimpleNamespace(message_id=3333))
    settings = SimpleNamespace(files_dir=str(tmp_path), stt_max_seconds=120)
    await handlers._reply_to_client(msg, s, erlan, FakeBot(), settings, FakeTranscriber("Заменил"))
    assert msg.answers == ["⏳ Распознаю…"]
    assert msg.edits == [f"💬 → З-{task.id} ✓\n🎤 Заменил"]
    [cm] = (await s.scalars(select(TaskComment))).all()
    assert (cm.kind, cm.text) == ("audio", "Заменил")
    tg = [o.payload for o in (await s.scalars(select(Outbox))).all()]
    assert [o["text"] for o in tg if o.get("file_kind") == "voice"] == [
        f"💬 З-{task.id} · Ерлан (🎤): Заменил"
    ]
    assert (await s.scalars(select(Outbox).where(Outbox.channel == "wa"))).all() == []


async def test_voice_comment_too_long_sent_as_audio(s, clock_, tmp_path):
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    await make_staff(s, tg_id=7, name="Тимур")
    task = await make_task(s, erlan)
    await tasks_svc.link(s, 6, 3333, task.id)
    msg = _VoiceChatMessage(6, duration=500, reply_to_message=SimpleNamespace(message_id=3333))
    stt = FakeTranscriber("не должен вызываться")
    settings = SimpleNamespace(files_dir=str(tmp_path), stt_max_seconds=120)
    await handlers._reply_to_client(msg, s, erlan, FakeBot(), settings, stt)
    assert stt.calls == [] and msg.answers == [f"💬 → З-{task.id} ✓"]
    [cm] = (await s.scalars(select(TaskComment))).all()
    assert (cm.kind, cm.text) == ("audio", None)


class _ClosingTranscriber(FakeTranscriber):
    """Пока «распознаём», задачу закрывают другим запросом."""

    def __init__(self, s, task_id):
        super().__init__("Заменил")
        self.s, self.task_id = s, task_id

    async def transcribe(self, path):
        await self.s.execute(update(Task).where(Task.id == self.task_id).values(status="closed"))
        return await super().transcribe(path)


async def test_task_closed_during_recognition_no_comment(s, clock_, tmp_path):
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    await make_staff(s, tg_id=7, name="Тимур")
    task = await make_task(s, erlan)
    await tasks_svc.link(s, 6, 3333, task.id)
    await s.flush()
    msg = _VoiceChatMessage(6, reply_to_message=SimpleNamespace(message_id=3333))
    settings = SimpleNamespace(files_dir=str(tmp_path), stt_max_seconds=120)
    await handlers._reply_to_client(msg, s, erlan, FakeBot(), settings,
                                    _ClosingTranscriber(s, task.id))
    assert msg.edits == [f"З-{task.id} уже закрыта — комментарий не отправлен."]
    assert (await s.scalars(select(TaskComment))).all() == []
    assert (await s.scalars(select(Outbox))).all() == []
```

(`update` из `sqlalchemy` и `Task` из `helpdesk.db.models` — добавить в импорты, если их нет; путь модели сверить с остальными импортами файла.)

(`FakeBot`, `FakeChatMessage`, `_Sent`, `make_*` уже есть в `test_telegram.py`. `FakeBot.download` пишет файл.)

- [ ] **Step 2: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_tasks_queries.py tests/test_telegram.py`
Expected: FAIL (`TypeError: comment() got an unexpected keyword argument 'voice'`, `_reply_to_client() takes 5 positional arguments`).

- [ ] **Step 3: `tasks.service.comment`**

Новый параметр `voice: bool = False` в конце сигнатуры; константа модуля `MAX_CAPTION = 1024` (лимит подписи Telegram) и рассылка:

```python
    head = f"💬 {render.number(t)} · {staff.name}{' (🎤)' if voice else ''}"
    body = f"{head}: {text}" if text else head
    # подпись к файлу Telegram режет на 1024 — длинный текст отдельным сообщением
    split = bool(file_path) and len(body) > MAX_CAPTION
    for st in await rules.participants(s, t):
        if st.id == staff.id:
            continue
        if file_path:
            outbox.tg_send(
                s,
                st.tg_chat_id,
                text=head if split else body,
                file_path=file_path,
                file_kind=TG_FILE_KIND[kind],
                task_id=t.id,
            )
        if split or not file_path:
            outbox.tg_send(s, st.tg_chat_id, text=body, task_id=t.id)
```

- [ ] **Step 4: `handlers.py`**

Импорт: `from helpdesk.telegram import menu, task_handlers, voice`.

`_comment_task` — новая сигнатура и обработка голоса (проверки `can_see` и «закрыта» в начале не меняются):

```python
async def _comment_task(
    message: Message, s, staff: Staff, bot: Bot, settings: Settings, t, stt=None
) -> None:
    n = task_render.number(t)
    if not task_rules.can_see(staff, t):
        await message.answer("Задача недоступна.")
        return
    if t.status == "closed":
        await message.answer(f"{n} уже закрыта — комментарий не отправлен.")
        return
    is_voice = bool(message.voice or message.audio)
    progress = None
    try:
        if is_voice and stt is not None:
            r = await voice.recognize(message, bot, settings, stt, keep_file=True)
            content, progress = r.content, r.progress
            text = r.text or content.text
        else:
            content = await extract_content(bot, message, settings.files_dir)
            text = content.text
    except FileTooBig:
        await message.answer("Файл больше 20 МБ — бот не может его скачать. Отправьте ссылкой.")
        return
    # распознавание идёт до минуты — задачу могли закрыть, перечитываем
    await s.refresh(t)
    c = await tasks.comment(
        s,
        t,
        staff,
        kind=content.kind,
        text=text,
        file_path=content.file_path,
        file_name=content.file_name,
        voice=is_voice,
    )
    if c is None:
        reply = f"{n} уже закрыта — комментарий не отправлен."
    else:
        await tasks.link(s, staff.tg_chat_id, message.message_id, t.id)
        reply = f"💬 → {n} ✓" + (f"\n🎤 {text}" if is_voice and text else "")
    if progress is not None:
        await progress.edit_text(reply)
    else:
        await message.answer(reply)
```

`_reply_to_client` — параметр `stt=None` в конце, передать дальше:

```python
async def _reply_to_client(
    message: Message, s, staff: Staff, bot: Bot, settings: Settings, stt=None
):
    reply_to = ...
    if reply_to is not None:
        task = await tasks.task_for_reply(s, staff.tg_chat_id, reply_to)
        if task is not None:
            await _comment_task(message, s, staff, bot, settings, task, stt)
            return
    ...  # дальше без изменений
```

`staff_text` и `staff_media` — добавить параметр `stt` и передать его: `await _reply_to_client(message, s, staff, bot, settings, stt)`.

- [ ] **Step 5: Проверить и закоммитить**

Run: `uv run ruff format src tests && make check` — Expected: PASS.

```bash
git add src/helpdesk/tasks/service.py src/helpdesk/telegram/handlers.py tests/test_tasks_queries.py tests/test_telegram.py
git commit -m "feat(tasks): voice comments carry a transcript in the caption"
```

---

### Task 5: Документация и проверка на настоящем Whisper

**Files:**
- Modify: `README.md`

- [ ] **Step 1: README**

В разделе «Установка», в списке переменных `.env`, добавить:

```markdown
- `STT_ENABLED`, `STT_MODEL`, `STT_MAX_SECONDS` — распознавание голосовых (локальный Whisper). Для `small` нужно от 4 ГБ RAM; первое голосовое скачивает модель (~500 МБ) в `data/models`.
```

В конец «Чек-листа ручной приёмки»:

```markdown
### Голосовой ввод

27. [ ] «➕ Новая задача» → тип → голосовое «Заменить картридж в 214 кабинете» → первое голосовое: «⏳ Загружаю модель…», затем «🎤 Распознано: …» с кнопками.
28. [ ] «✅ Подходит» → шаг «Для кого»; задача создана с этим описанием, голосовое — вложением.
29. [ ] «✏️ Исправить» → можно прислать текст или новое голосовое.
30. [ ] Голосовое на казахском — текст распознан (оценить качество; при плохом — попробовать `STT_MODEL=medium`).
31. [ ] Голосовое длиннее 2 минут в описании → «напишите текстом».
32. [ ] «Ответить» голосовым на карточку задачи → остальным аудио с подписью «💬 З-… · Имя (🎤): текст», себе — «💬 → З-… ✓» и текст.
33. [ ] `STT_ENABLED=false` → голосовое в описании: «Распознавание выключено»; комментарий уходит аудио.
```

- [ ] **Step 2: Финальная проверка**

Run: `make test-db && make check` — Expected: PASS.

- [ ] **Step 3: Проверка с настоящей моделью (вручную, на Mac)**

Run: `docker compose up -d --build bot` и пройти пункты 27–33 чек-листа; в логах (`docker compose logs bot`) не должно быть `stt failed`. Записать в отчёт время распознавания 10–20-секундного голосового и размер `data/models`.

- [ ] **Step 4: Коммит**

```bash
git add README.md
git commit -m "docs: voice input settings and acceptance checklist"
```
