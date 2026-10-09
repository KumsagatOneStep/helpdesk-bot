# Внутренние задачи специалистов — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Специалисты создают в боте внутренние задачи (тип Helpdesk/Координатор, описание, для кого, срок), передают их друг другу, просят помощи, обсуждают комментариями и получают напоминания о сроках.

**Architecture:** Новый пакет `helpdesk/tasks/` (`rules`, `render`, `service`, `reminders`) со своими таблицами; код заявок (`tickets/`) не меняется. Вся отправка — через существующий outbox: `tg_send` получает `task_id`/`task_card_staff_id` (воркер пишет `task_links`/`task_cards`), новая операция `task_edit` перерисовывает карточки задачи у получателя. В Telegram — отдельный router `telegram/task_handlers.py` (диалог создания на aiogram FSM и кнопки карточек), подключённый раньше общего.

**Tech Stack:** Python 3.12, aiogram 3.31 (FSM, `SkipHandler`), SQLAlchemy 2 async + asyncpg, Alembic, openpyxl, pytest + pytest-asyncio, PostgreSQL 16.

**Spec:** `docs/superpowers/specs/2026-10-09-internal-tasks-design.md`

## Global Constraints

- Ветка `internal-tasks` (от `security-fixes`). Перед началом: `make test-db && make check` — 155 тестов зелёные; после каждой задачи — снова зелёные.
- Текущее время только через `clock.utcnow()` с импортом модуля (`from helpdesk import clock`); «сегодня» по местному времени — `tasks.service.today()` (= `clock.local(clock.utcnow()).date()`).
- Сервисы **не делают commit**: транзакцией владеет вызывающий код (middleware Telegram, воркер, таймер).
- Любая отправка специалисту — через `helpdesk.delivery.outbox`. Исключения — `callback.answer` и прямые `message.answer` на действие специалиста.
- Telegram: без `parse_mode` (простой текст), HTML-экранирование не нужно; текст ≤ 4096, подпись ≤ 1024.
- Номер задачи в интерфейсе: `З-<id>` (кириллическая «З»).
- Типы задачи (`tasks.kind`): `helpdesk`, `coordinator`. Роли (`staff.role`): `helpdesk` (по умолчанию), `coordinator`. Статусы задачи: `queued`, `in_progress`, `closed`. События (`task_events.type`): `created`, `accepted`, `taken`, `help`, `released`, `closed`, `reminded_due`, `reminded_overdue`.
- `callback_data`: карточки — `tk_take:<id>`, `tk_close:<id>`, `tk_help:<id>`; диалог создания — `tkn_kind:<helpdesk|coordinator>`, `tkn_skip`, `tkn_due:<today|tomorrow|week|custom|none>`, `tkn_ok`, `tkn_cancel`; `/staff` — `stf_role:<id>`. id в данных кнопок — только `\d{1,9}`.
- Лимиты: описание ≤ 2000 символов, «для кого» ≤ 200, вложение ≤ 20 МБ (существующий `extract_content`).
- Новый параметр `.env`: `TASK_REMIND_HOUR` (по умолчанию `9`).
- Тексты интерфейса — на русском, как в спецификации.
- Перед каждым `make check` выполнять `uv run ruff format src tests` (в `make check` есть `ruff format --check`). Импорты в тестах — только в начале файла (ruff E402).
- Сообщения коммитов на английском, в конце строка `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Reply на карточку задачи, когда у специалиста есть активная заявка WhatsApp.** Сообщение должно стать комментарием к задаче и ни при каких условиях не уйти клиенту в WhatsApp. Тест в задаче 11 (`test_reply_to_task_card_is_comment_never_whatsapp`).
2. **Кнопка меню или команда посреди диалога создания.** Диалог завершается, кнопка срабатывает как обычно; «✖️ Отмена» отменяет диалог, а не становится описанием. Тест в задаче 10 (`test_menu_text_leaves_dialog_but_cancel_does_not_match`).
3. **Задача со сроком «Сегодня», созданная после часа напоминаний.** Мгновенного «⏰ Сегодня срок» быть не должно — создатель и так знает. Тест в задаче 7 (`test_due_today_created_today_is_not_reminded`).
4. **Админ отключает специалиста, у которого задачи в работе.** Задачи не должны зависнуть на отключённом: уходят в очередь, коллегам на смене — карточка с «Взять». Тест в задаче 8 (`test_disable_releases_tasks_to_queue`).
5. **Helpdesk-специалист ищет или открывает чужую задачу типа «Координатор».** Не видит её ни в поиске, ни по `З-12`, ни в «Открытые». Тест в задаче 6 (`test_helpdesk_cannot_see_foreign_coordinator_tasks`).

---

## Карта файлов

| Файл | Что делает |
|---|---|
| `src/helpdesk/db/models.py` | + `Staff.role`, `Task`, `TaskComment`, `TaskEvent`, `TaskCard`, `TaskLink` |
| `migrations/versions/c4d2e8f1a6b7_internal_tasks.py` | миграция |
| `src/helpdesk/config.py`, `.env.example` | `task_remind_hour` |
| `src/helpdesk/delivery/outbox.py`, `delivery/worker.py` | `task_id`, `task_card_staff_id`, операция `task_edit` |
| `src/helpdesk/tasks/rules.py` | кто может выполнять, участники, видимость |
| `src/helpdesk/tasks/render.py` | номер, карточка, кнопки, разбор даты/номера, строки списков |
| `src/helpdesk/tasks/service.py` | создание, взять, помощь, освобождение, закрытие, комментарии, выборки |
| `src/helpdesk/tasks/reminders.py` | напоминания о сроках |
| `src/helpdesk/reminders.py`, `main.py` | вызов напоминаний о задачах |
| `src/helpdesk/staff/service.py` | роль, `/on` для задач, отключение освобождает задачи |
| `src/helpdesk/stats/service.py`, `stats/export.py` | блок задач в `/stats`, лист «Задачи» |
| `src/helpdesk/telegram/filters.py` | `IsActive`, `IsAdmin`, `ID_DATA`, `arg_id` (вынесены из `handlers.py`) |
| `src/helpdesk/telegram/menu.py` | кнопка «➕ Новая задача» |
| `src/helpdesk/telegram/task_handlers.py` | диалог создания, кнопки карточек |
| `src/helpdesk/telegram/handlers.py` | `/my`, `/open`, поиск, reply → комментарий, роль в `/staff`, `/stats`, `setup` |
| `tests/factories.py` | `make_staff(role=)`, `make_task` |
| `tests/test_tasks_*.py` | новые тесты |

---

### Task 1: Модель данных, миграция, настройка

**Files:**
- Modify: `src/helpdesk/db/models.py`
- Create: `migrations/versions/c4d2e8f1a6b7_internal_tasks.py`
- Modify: `src/helpdesk/config.py`, `.env.example`
- Modify: `tests/factories.py`, `tests/test_models.py`, `tests/test_config.py`

**Interfaces:**
- Produces: модели `Task`, `TaskComment`, `TaskEvent`, `TaskCard`, `TaskLink`; `Staff.role: str`; `Settings.task_remind_hour: int`; фабрики `make_staff(..., role="helpdesk")`, `make_task(s, creator, *, kind="helpdesk", status="in_progress", assignee="creator", description="Заменить картридж", requester=None, due_date=None) -> Task`.

- [ ] **Step 1: Тесты**

В `tests/factories.py`: импорт `Task` из моделей, параметр `role` у `make_staff` и новая фабрика:

```python
async def make_staff(
    s, *, tg_id, name, is_admin=False, status="active", on_shift=True, role="helpdesk"
) -> Staff:
    st = Staff(
        tg_user_id=tg_id,
        tg_chat_id=tg_id,
        name=name,
        is_admin=is_admin,
        status=status,
        on_shift=on_shift,
        role=role,
    )
    s.add(st)
    await s.flush()
    return st


async def make_task(
    s,
    creator,
    *,
    kind="helpdesk",
    status="in_progress",
    assignee="creator",
    description="Заменить картридж",
    requester=None,
    due_date=None,
) -> Task:
    a = creator if assignee == "creator" else assignee
    t = Task(
        kind=kind,
        status=status,
        description=description,
        requester=requester,
        due_date=due_date,
        creator_id=creator.id,
        assignee_id=a.id if a else None,
        taken_at=clock.utcnow() if a else None,
    )
    s.add(t)
    await s.flush()
    return t
```

(В начало `tests/factories.py` — `from helpdesk import clock`.)

В конец `tests/test_models.py`:

```python
async def test_staff_role_defaults_to_helpdesk(s, clock_):
    from helpdesk.db.models import Staff

    st = Staff(tg_user_id=1, tg_chat_id=1, name="Ерлан")
    s.add(st)
    await s.flush()
    await s.refresh(st)
    assert st.role == "helpdesk"


async def test_task_defaults_and_relations(s, clock_):
    from datetime import date

    from helpdesk.db.models import TaskCard, TaskComment, TaskEvent, TaskLink
    from tests.factories import make_staff, make_task

    st = await make_staff(s, tg_id=1, name="Ерлан")
    t = await make_task(s, st, due_date=date(2026, 10, 10))
    s.add_all(
        [
            TaskComment(task_id=t.id, staff_id=st.id, kind="text", text="ок"),
            TaskEvent(task_id=t.id, type="created", staff_id=st.id),
            TaskCard(task_id=t.id, staff_id=st.id, tg_chat_id=1, tg_message_id=10),
            TaskLink(tg_chat_id=1, tg_message_id=10, task_id=t.id),
        ]
    )
    await s.flush()
    assert t.created_at == clock_.now
    assert (t.due_date, t.last_reminded_on, t.taken_at) == (date(2026, 10, 10), None, clock_.now)
```

В `tests/test_config.py`, в `test_admin_ids_parsed_and_media_url_defaults`, добавить строку:

```python
    assert s.task_remind_hour == 9
```

- [ ] **Step 2: Запустить — падают**

Run: `make test-db && TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_models.py tests/test_config.py`
Expected: FAIL (`ImportError: cannot import name 'Task'`).

- [ ] **Step 3: Модели**

В `src/helpdesk/db/models.py`: `from datetime import date, datetime`; в `Staff` после `on_shift`:

```python
    role: Mapped[str] = mapped_column(String(16), default="helpdesk", server_default="helpdesk")
```

В конец файла:

```python
class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_status_due", "status", "due_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # helpdesk | coordinator
    status: Mapped[str] = mapped_column(String(16))  # queued | in_progress | closed
    description: Mapped[str] = mapped_column(Text)
    requester: Mapped[str | None] = mapped_column(String(200))
    due_date: Mapped[date | None]
    last_reminded_on: Mapped[date | None]
    file_path: Mapped[str | None] = mapped_column(String(500))
    file_name: Mapped[str | None] = mapped_column(String(255))
    file_kind: Mapped[str | None] = mapped_column(String(16))  # photo | document
    creator_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    closed_by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(default=_now)
    taken_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]


class TaskComment(Base):
    __tablename__ = "task_comments"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    kind: Mapped[str] = mapped_column(String(16))  # text|image|document|audio|video
    text: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(String(500))
    file_name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(default=_now)


class TaskEvent(Base):
    __tablename__ = "task_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    type: Mapped[str] = mapped_column(String(32))
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(default=_now)


class TaskCard(Base):
    __tablename__ = "task_cards"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    tg_chat_id: Mapped[int] = mapped_column(BigInteger)
    tg_message_id: Mapped[int] = mapped_column(BigInteger)
    sent_at: Mapped[datetime] = mapped_column(default=_now)


class TaskLink(Base):
    __tablename__ = "task_links"
    tg_chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tg_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
```

- [ ] **Step 4: Миграция**

Create `migrations/versions/c4d2e8f1a6b7_internal_tasks.py`:

```python
"""internal tasks

Revision ID: c4d2e8f1a6b7
Revises: b3c9d1e2f4a5
Create Date: 2026-10-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d2e8f1a6b7"
down_revision: str | Sequence[str] | None = "b3c9d1e2f4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column(
        "staff",
        sa.Column("role", sa.String(16), server_default="helpdesk", nullable=False),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("requester", sa.String(200), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("last_reminded_on", sa.Date(), nullable=True),
        sa.Column("file_path", sa.String(500), nullable=True),
        sa.Column("file_name", sa.String(255), nullable=True),
        sa.Column("file_kind", sa.String(16), nullable=True),
        sa.Column("creator_id", sa.Integer(), sa.ForeignKey("staff.id"), nullable=False),
        sa.Column("assignee_id", sa.Integer(), sa.ForeignKey("staff.id"), nullable=True),
        sa.Column("closed_by_id", sa.Integer(), sa.ForeignKey("staff.id"), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("taken_at", TS, nullable=True),
        sa.Column("closed_at", TS, nullable=True),
    )
    op.create_index("ix_tasks_status_due", "tasks", ["status", "due_date"])
    op.create_table(
        "task_comments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("staff_id", sa.Integer(), sa.ForeignKey("staff.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("file_path", sa.String(500), nullable=True),
        sa.Column("file_name", sa.String(255), nullable=True),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_task_comments_task_id", "task_comments", ["task_id"])
    op.create_table(
        "task_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("staff_id", sa.Integer(), sa.ForeignKey("staff.id"), nullable=True),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_task_events_task_id", "task_events", ["task_id"])
    op.create_table(
        "task_cards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("staff_id", sa.Integer(), sa.ForeignKey("staff.id"), nullable=False),
        sa.Column("tg_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_message_id", sa.BigInteger(), nullable=False),
        sa.Column("sent_at", TS, nullable=False),
    )
    op.create_index("ix_task_cards_task_id", "task_cards", ["task_id"])
    op.create_table(
        "task_links",
        sa.Column("tg_chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("tg_message_id", sa.BigInteger(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("task_links")
    op.drop_index("ix_task_cards_task_id", table_name="task_cards")
    op.drop_table("task_cards")
    op.drop_index("ix_task_events_task_id", table_name="task_events")
    op.drop_table("task_events")
    op.drop_index("ix_task_comments_task_id", table_name="task_comments")
    op.drop_table("task_comments")
    op.drop_index("ix_tasks_status_due", table_name="tasks")
    op.drop_table("tasks")
    op.drop_column("staff", "role")
```

(Фикстура `_migrated` в `conftest.py` делает `downgrade base` → `upgrade head` — это и есть проверка миграции вверх/вниз.)

- [ ] **Step 5: Настройка**

В `src/helpdesk/config.py` после `remind_after_min`:

```python
    task_remind_hour: int = 9
```

В `.env.example` после `REMIND_AFTER_MIN=10`:

```
# Час (по TZ), в который приходят напоминания о сроках задач
TASK_REMIND_HOUR=9
```

- [ ] **Step 6: Проверить и закоммитить**

Run: `make check`
Expected: все тесты PASS, ruff чисто. Дополнительно: `uv run alembic check` с `DATABASE_URL` тестовой базы — «No new upgrade operations detected» (модели совпадают с миграцией).

```bash
git add src/helpdesk/db/models.py src/helpdesk/config.py .env.example migrations/versions/c4d2e8f1a6b7_internal_tasks.py tests/factories.py tests/test_models.py tests/test_config.py
git commit -m "feat(tasks): data model, migration and TASK_REMIND_HOUR setting

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Outbox и воркер — ссылки, карточки и редактирование задач

**Files:**
- Modify: `src/helpdesk/delivery/outbox.py`, `src/helpdesk/delivery/worker.py`
- Test: `tests/test_outbox_worker.py`

**Interfaces:**
- Consumes: `TaskCard`, `TaskLink` (Task 1).
- Produces: `outbox.tg_send(..., task_id: int | None = None, task_card_staff_id: int | None = None)` — ключи попадают в payload, только если не `None`; `outbox.tg_task_edit(s, chat_id: int, *, task_id: int, staff_id: int, text: str, buttons: Buttons | None) -> None` (op `task_edit`).

- [ ] **Step 1: Тесты**

В конец `tests/test_outbox_worker.py` (импорты: `from helpdesk.db.models import TaskCard, TaskLink`, `from tests.factories import make_task`):

```python
async def test_task_send_creates_link_and_card(sf, clock_):
    async with sf() as s, s.begin():
        st = await make_staff(s, tg_id=7, name="Ерлан")
        t = await make_task(s, st)
        outbox.tg_send(s, 7, text="карточка", task_id=t.id, task_card_staff_id=st.id)
        outbox.tg_send(s, 7, text="уведомление", task_id=t.id)
        tid, sid = t.id, st.id
    tg = FakeTg()
    await drain(sf, FakeWa(), tg)
    async with sf() as s:
        links = (await s.scalars(select(TaskLink))).all()
        cards = (await s.scalars(select(TaskCard))).all()
    assert sorted(lk.tg_message_id for lk in links) == [m["mid"] for m in tg.sent]
    assert all(lk.task_id == tid for lk in links)
    assert [(c.task_id, c.staff_id, c.tg_message_id) for c in cards] == [
        (tid, sid, tg.sent[0]["mid"])
    ]


async def test_ticket_payload_has_no_task_keys():
    class S:
        def add(self, obj):
            self.obj = obj

    s = S()
    outbox.tg_send(s, 1, text="x", ticket_id=5)
    assert "task_id" not in s.obj.payload and "task_card_staff_id" not in s.obj.payload


async def test_task_edit_updates_all_cards_of_recipient(sf, clock_):
    async with sf() as s, s.begin():
        st = await make_staff(s, tg_id=7, name="Ерлан")
        other = await make_staff(s, tg_id=8, name="Тимур")
        t = await make_task(s, st)
        s.add_all(
            [
                TaskCard(task_id=t.id, staff_id=st.id, tg_chat_id=7, tg_message_id=11),
                TaskCard(task_id=t.id, staff_id=st.id, tg_chat_id=7, tg_message_id=12),
                TaskCard(task_id=t.id, staff_id=other.id, tg_chat_id=8, tg_message_id=13),
            ]
        )
        outbox.tg_task_edit(
            s, 7, task_id=t.id, staff_id=st.id, text="новый", buttons=[[("A", "tk_close:1")]]
        )
        outbox.tg_task_edit(s, 9, task_id=t.id, staff_id=999, text="никому", buttons=None)
    tg = FakeTg()
    await drain(sf, FakeWa(), tg)
    assert tg.edited == [
        (7, 11, "новый", [["A", "tk_close:1"]]),
        (7, 12, "новый", [["A", "tk_close:1"]]),
    ]
    async with sf() as s:
        statuses = [o.status for o in (await s.scalars(select(Outbox).order_by(Outbox.id))).all()]
    assert statuses == ["sent", "skipped"]


async def test_task_edit_skips_card_with_permanent_error(sf, clock_):
    async with sf() as s, s.begin():
        st = await make_staff(s, tg_id=7, name="Ерлан")
        t = await make_task(s, st)
        s.add_all(
            [
                TaskCard(task_id=t.id, staff_id=st.id, tg_chat_id=7, tg_message_id=11),
                TaskCard(task_id=t.id, staff_id=st.id, tg_chat_id=7, tg_message_id=12),
            ]
        )
        outbox.tg_task_edit(s, 7, task_id=t.id, staff_id=st.id, text="новый", buttons=None)

    class Tg(FakeTg):
        async def edit(self, chat_id, message_id, text, buttons):
            if message_id == 11:
                raise worker.PermanentSendError("message to edit not found")
            await super().edit(chat_id, message_id, text, buttons)

    tg = Tg()
    await drain(sf, FakeWa(), tg)
    assert [e[1] for e in tg.edited] == [12]
    async with sf() as s:
        assert (await s.scalar(select(Outbox))).status == "sent"
```

- [ ] **Step 2: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_outbox_worker.py`
Expected: FAIL (`TypeError: tg_send() got an unexpected keyword argument 'task_id'`).

- [ ] **Step 3: outbox.py**

Сигнатура `tg_send` — два новых именованных параметра в конце; после сборки `payload`, перед `_add`:

```python
    task_id: int | None = None,
    task_card_staff_id: int | None = None,
) -> None:
    payload = {
        ...  # как было
    }
    if task_id is not None:
        payload["task_id"] = task_id
    if task_card_staff_id is not None:
        payload["task_card_staff_id"] = task_card_staff_id
    _add(s, "tg", f"tg:{chat_id}", payload)
```

Новая функция после `tg_edit`:

```python
def tg_task_edit(
    s, chat_id: int, *, task_id: int, staff_id: int, text: str, buttons: Buttons | None
) -> None:
    """Перерисовать все карточки задачи у получателя (message_id узнаёт воркер)."""
    payload = {
        "op": "task_edit",
        "chat_id": chat_id,
        "task_id": task_id,
        "staff_id": staff_id,
        "text": text,
        "buttons": _buttons(buttons),
    }
    _add(s, "tg", f"tg:{chat_id}", payload)
```

- [ ] **Step 4: worker.py**

Импорт: `from helpdesk.db.models import Outbox, Staff, TaskCard, TaskLink, TgCard, TgLink, Ticket, TicketEvent`.

В `_deliver`, ветка `op == "send"`, после блока `if p.get("card_staff_id") is not None: ...`:

```python
        if p.get("task_id") is not None:
            await s.merge(
                TaskLink(tg_chat_id=p["chat_id"], tg_message_id=mid, task_id=p["task_id"])
            )
        if p.get("task_card_staff_id") is not None:
            s.add(
                TaskCard(
                    task_id=p["task_id"],
                    staff_id=p["task_card_staff_id"],
                    tg_chat_id=p["chat_id"],
                    tg_message_id=mid,
                )
            )
```

Новая ветка после `elif op == "edit": ...`:

```python
    elif op == "task_edit":
        q = (
            select(TaskCard)
            .where(TaskCard.task_id == p["task_id"], TaskCard.staff_id == p["staff_id"])
            .order_by(TaskCard.id)
        )
        cards = (await s.scalars(q)).all()
        if not cards:
            item.status = "skipped"
            return
        for card in cards:
            try:
                await tg.edit(card.tg_chat_id, card.tg_message_id, p["text"], p.get("buttons"))
            except PermanentSendError as e:  # карточку удалили в чате — остальные всё равно правим
                log.info("task card edit failed", card_id=card.id, error=str(e))
```

(Заказ по `chat_key` гарантирует: карточка, отправленная раньше в тот же чат, уже записана в `task_cards`, когда до неё дойдёт `task_edit`.)

- [ ] **Step 5: Проверить и закоммитить**

Run: `make check` — Expected: PASS.

```bash
git add src/helpdesk/delivery/outbox.py src/helpdesk/delivery/worker.py tests/test_outbox_worker.py
git commit -m "feat(outbox): link task messages, record task cards and edit them in place

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Правила и отображение задач (`tasks/rules.py`, `tasks/render.py`)

**Files:**
- Create: `src/helpdesk/tasks/__init__.py` (пустой), `src/helpdesk/tasks/rules.py`, `src/helpdesk/tasks/render.py`
- Test: `tests/test_tasks_render.py`

**Interfaces:**
- Produces (`rules`): `KINDS = ("helpdesk", "coordinator")`; `can_do(staff, kind) -> bool`; `async eligible(s, kind) -> list[Staff]`; `async coordinators(s) -> list[Staff]`; `async participants(s, t) -> list[Staff]` (по id); `can_see(staff, t) -> bool`; `visible_cond(staff)` — SQL-условие для `select(Task)`.
- Produces (`render`): `KIND_RU`, `KIND_PLAIN`, `STATUS_RU`, `MAX_DESCRIPTION = 2000`, `MAX_REQUESTER = 200`; `number(t) -> str`; `parse_task_id(q) -> int | None`; `parse_due(text, today) -> date | None`; `due_from_choice(code, today) -> date`; `is_overdue(t, today) -> bool`; `short(text, limit=100) -> str`; `card(t, names, today) -> str`; `buttons_for(t, viewer, *, can_help) -> Buttons | None`; `summary(data: dict) -> str`; `list_line(t, today) -> str`; `open_line(t, names, today) -> str`; `search_line(t, names) -> str`; `details(t, names, comments, today) -> str`.

- [ ] **Step 1: Тесты**

Create `tests/test_tasks_render.py`:

```python
from datetime import date

from helpdesk.db.models import TaskComment
from helpdesk.tasks import render, rules
from tests.factories import make_staff, make_task

TODAY = date(2026, 10, 7)


def test_parse_task_id():
    for q in ("З-12", "з12", "З 12", " Z-12 ", "z12"):
        assert render.parse_task_id(q) == 12, q
    for q in ("12", "#12", "З-", "З-1234567", "Зина", "З-12а"):
        assert render.parse_task_id(q) is None, q


def test_parse_due():
    assert render.parse_due("15.10", TODAY) == date(2026, 10, 15)
    assert render.parse_due("07.10", TODAY) == TODAY
    assert render.parse_due("01.03", TODAY) == date(2027, 3, 1)  # без года — ближайшая будущая
    assert render.parse_due("15.10.2027", TODAY) == date(2027, 10, 15)
    assert render.parse_due("06.10.2026", TODAY) is None  # прошлое
    assert render.parse_due("31.02", TODAY) is None
    assert render.parse_due("завтра", TODAY) is None
    assert render.parse_due("1.1.26", TODAY) is None


def test_due_from_choice():
    assert render.due_from_choice("today", TODAY) == TODAY
    assert render.due_from_choice("tomorrow", TODAY) == date(2026, 10, 8)
    assert render.due_from_choice("week", TODAY) == date(2026, 10, 14)


async def test_card_text(s, clock_):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    t = await make_task(
        s, erlan, requester="Иванова, каб. 214", due_date=date(2026, 10, 10),
        description="Заменить картридж в принтере HP",
    )
    names = {erlan.id: "Ерлан"}
    assert render.card(t, names, TODAY) == (
        f"📌 З-{t.id} · Helpdesk · срок 10.10\n"
        "Для: Иванова, каб. 214\n"
        "Заменить картридж в принтере HP\n"
        "Создал: Ерлан · Исполнитель: Ерлан"
    )
    t.kind, t.status, t.assignee_id, t.due_date, t.requester = (
        "coordinator", "queued", None, date(2026, 10, 6), None,
    )
    assert render.card(t, names, TODAY).splitlines() == [
        f"📌 З-{t.id} · 🧭 Координатор · ⚠️ срок 06.10",
        "Заменить картридж в принтере HP",
        "Создал: Ерлан · Исполнитель: — (ищем)",
    ]
    t.status, t.closed_by_id, t.due_date = "closed", erlan.id, None
    text = render.card(t, names, TODAY)
    assert text.splitlines()[0] == f"📌 З-{t.id} · 🧭 Координатор"
    assert text.endswith("✔️ Закрыта: Ерлан")


async def test_buttons_for(s, clock_):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    tima = await make_staff(s, tg_id=2, name="Тимур")
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    t = await make_task(s, erlan)
    n = t.id
    close, help_ = ("✅ Закрыть", f"tk_close:{n}"), ("🆘 Нужна помощь", f"tk_help:{n}")
    assert render.buttons_for(t, erlan, can_help=True) == [[close, help_]]
    assert render.buttons_for(t, erlan, can_help=False) == [[close]]
    assert render.buttons_for(t, tima, can_help=False) is None
    t.assignee_id = tima.id  # создатель, но не исполнитель
    assert render.buttons_for(t, erlan, can_help=False) == [[close]]
    t.status, t.assignee_id, t.taken_at = "queued", None, None
    assert render.buttons_for(t, tima, can_help=False) == [[("✅ Принять", f"tk_take:{n}")]]
    t.taken_at = clock_.now  # уже кто-то брал — теперь это «Взять»
    assert render.buttons_for(t, erlan, can_help=False) == [
        [("✋ Взять", f"tk_take:{n}")],
        [close],
    ]
    t.kind = "coordinator"
    assert render.buttons_for(t, tima, can_help=False) is None
    assert render.buttons_for(t, coord, can_help=False) == [[("✋ Взять", f"tk_take:{n}")]]
    t.status = "closed"
    assert render.buttons_for(t, coord, can_help=False) is None


async def test_rules(s, clock_):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    off = await make_staff(s, tg_id=4, name="Ушёл", role="coordinator", status="disabled")
    assert rules.can_do(erlan, "helpdesk") and not rules.can_do(erlan, "coordinator")
    assert rules.can_do(coord, "coordinator") and not rules.can_do(off, "coordinator")
    assert [st.id for st in await rules.eligible(s, "helpdesk")] == [erlan.id, coord.id]
    assert [st.id for st in await rules.coordinators(s)] == [coord.id]
    t = await make_task(s, erlan, kind="coordinator", status="queued", assignee=None)
    assert [st.id for st in await rules.participants(s, t)] == [erlan.id, coord.id]
    assert rules.can_see(erlan, t) and rules.can_see(coord, t)
    tima = await make_staff(s, tg_id=2, name="Тимур")
    assert not rules.can_see(tima, t)


async def test_summary_and_lines(s, clock_):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    assert render.summary(
        {"kind": "coordinator", "description": "Ноутбук", "requester": "Бухгалтерия",
         "due": "2026-10-10", "file_name": "photo_x.jpg"}
    ) == (
        "Проверьте задачу:\nТип: 🧭 Координатор\nОписание: Ноутбук\nДля: Бухгалтерия\n"
        "Срок: 10.10.2026\nВложение: photo_x.jpg"
    )
    assert render.summary({"kind": "helpdesk", "description": "X", "due": None}).endswith(
        "Срок: без срока"
    )
    t = await make_task(s, erlan, due_date=date(2026, 10, 6), description="a" * 80)
    assert render.list_line(t, TODAY) == f"⚠️ З-{t.id} · до 06.10 · {'a' * 49}…"
    t.due_date = None
    assert render.list_line(t, TODAY) == f"З-{t.id} · без срока · {'a' * 49}…"
    names = {erlan.id: "Ерлан"}
    assert render.open_line(t, names, TODAY) == f"З-{t.id} · Helpdesk · Ерлан · {'a' * 49}…"
    assert render.search_line(t, names) == f"З-{t.id} · 07.10 · {'a' * 49}… · в работе · Ерлан"
    c = TaskComment(task_id=t.id, staff_id=erlan.id, kind="image", text="скрин",
                    created_at=clock_.now)
    d = render.details(t, names, [c], TODAY)
    assert d.startswith(f"📌 З-{t.id} · Helpdesk")
    assert d.endswith("Комментарии:\n07.10 14:00 Ерлан: [🖼 фото] скрин")
```

- [ ] **Step 2: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_tasks_render.py`
Expected: FAIL (`ModuleNotFoundError: helpdesk.tasks`).

- [ ] **Step 3: rules.py**

```python
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
    return (
        _sees_all(staff)
        or t.kind == "helpdesk"
        or staff.id in (t.creator_id, t.assignee_id)
    )


def visible_cond(staff: Staff):
    if _sees_all(staff):
        return true()
    return or_(Task.kind == "helpdesk", Task.creator_id == staff.id, Task.assignee_id == staff.id)
```

- [ ] **Step 4: render.py**

```python
import re
from datetime import date, timedelta

from helpdesk import clock
from helpdesk.db.models import Staff, Task, TaskComment
from helpdesk.delivery.outbox import Buttons
from helpdesk.tasks import rules
from helpdesk.tickets.render import KIND_LABEL

KIND_RU = {"helpdesk": "Helpdesk", "coordinator": "🧭 Координатор"}
KIND_PLAIN = {"helpdesk": "Helpdesk", "coordinator": "Координатор"}
STATUS_RU = {"queued": "ждёт исполнителя", "in_progress": "в работе", "closed": "закрыта"}
MAX_DESCRIPTION = 2000
MAX_REQUESTER = 200
DUE_DAYS = {"today": 0, "tomorrow": 1, "week": 7}

_TASK_ID = re.compile(r"^\s*[ЗзZz]\s*-?\s*(\d{1,6})\s*$")
_DATE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s*$")


def number(t: Task) -> str:
    return f"З-{t.id}"


def parse_task_id(q: str) -> int | None:
    m = _TASK_ID.match(q)
    return int(m.group(1)) if m else None


def parse_due(text: str, today: date) -> date | None:
    m = _DATE.match(text)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        if y:
            res = date(int(y), mo, d)
        else:
            res = date(today.year, mo, d)
            if res < today:
                res = date(today.year + 1, mo, d)
    except ValueError:
        return None
    return res if res >= today else None


def due_from_choice(code: str, today: date) -> date:
    return today + timedelta(days=DUE_DAYS[code])


def is_overdue(t: Task, today: date) -> bool:
    return t.status != "closed" and t.due_date is not None and t.due_date < today


def short(text: str, limit: int = 100) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def card(t: Task, names: dict[int, str], today: date) -> str:
    head = f"📌 {number(t)} · {KIND_RU[t.kind]}"
    if t.due_date:
        mark = "⚠️ " if is_overdue(t, today) else ""
        head += f" · {mark}срок {t.due_date:%d.%m}"
    lines = [head]
    if t.requester:
        lines.append(f"Для: {t.requester}")
    lines.append(t.description)
    who = names.get(t.assignee_id, "?") if t.assignee_id else "— (ищем)"
    lines.append(f"Создал: {names.get(t.creator_id, '?')} · Исполнитель: {who}")
    if t.status == "closed":
        lines.append(f"✔️ Закрыта: {names.get(t.closed_by_id, '?')}")
    return "\n".join(lines)


def buttons_for(t: Task, viewer: Staff, *, can_help: bool) -> Buttons | None:
    if t.status == "closed":
        return None
    close = ("✅ Закрыть", f"tk_close:{t.id}")
    if t.status == "queued":
        rows = []
        if rules.can_do(viewer, t.kind):
            label = "✅ Принять" if t.taken_at is None else "✋ Взять"
            rows.append([(label, f"tk_take:{t.id}")])
        if viewer.id == t.creator_id:
            rows.append([close])
        return rows or None
    if viewer.id == t.assignee_id:
        row = [close]
        if can_help:
            row.append(("🆘 Нужна помощь", f"tk_help:{t.id}"))
        return [row]
    if viewer.id == t.creator_id:
        return [[close]]
    return None


def summary(data: dict) -> str:
    lines = [
        "Проверьте задачу:",
        f"Тип: {KIND_RU[data['kind']]}",
        f"Описание: {data['description']}",
    ]
    if data.get("requester"):
        lines.append(f"Для: {data['requester']}")
    due = data.get("due")
    lines.append(f"Срок: {date.fromisoformat(due):%d.%m.%Y}" if due else "Срок: без срока")
    if data.get("file_name"):
        lines.append(f"Вложение: {data['file_name']}")
    return "\n".join(lines)


def list_line(t: Task, today: date) -> str:
    mark = "⚠️ " if is_overdue(t, today) else ""
    due = f"до {t.due_date:%d.%m}" if t.due_date else "без срока"
    return f"{mark}{number(t)} · {due} · {short(t.description, 50)}"


def open_line(t: Task, names: dict[int, str], today: date) -> str:
    mark = "⚠️ " if is_overdue(t, today) else ""
    who = names.get(t.assignee_id, "?") if t.assignee_id else "ждёт исполнителя"
    return f"{mark}{number(t)} · {KIND_PLAIN[t.kind]} · {who} · {short(t.description, 50)}"


def search_line(t: Task, names: dict[int, str]) -> str:
    who = f" · {names.get(t.assignee_id, '?')}" if t.assignee_id else ""
    return (
        f"{number(t)} · {clock.local(t.created_at):%d.%m} · {short(t.description, 50)}"
        f" · {STATUS_RU[t.status]}{who}"
    )


def _comment_line(c: TaskComment, names: dict[int, str]) -> str:
    parts = [f"[{KIND_LABEL[c.kind]}]"] if c.kind in KIND_LABEL else []
    if c.text:
        parts.append(c.text)
    body = " ".join(parts) or "[пусто]"
    return f"{clock.local(c.created_at):%d.%m %H:%M} {names.get(c.staff_id, '?')}: {body}"


def details(t: Task, names: dict[int, str], comments: list[TaskComment], today: date) -> str:
    text = card(t, names, today)
    if comments:
        text += "\n\nКомментарии:\n" + "\n".join(_comment_line(c, names) for c in comments)
    return text[-4000:] if len(text) > 4000 else text
```

Проверка `open_line` в тесте: исполнитель Ерлан (задача из `make_task` закреплена за создателем). Проверка `search_line`: `07.10` — дата `created_at` по Алматы (14:00 7 октября).

- [ ] **Step 5: Проверить и закоммитить**

Run: `make check` — Expected: PASS.

```bash
git add src/helpdesk/tasks tests/test_tasks_render.py
git commit -m "feat(tasks): eligibility rules, card rendering, task number and due date parsing

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Создание задачи и рассылка карточек

**Files:**
- Create: `src/helpdesk/tasks/service.py`
- Test: `tests/test_tasks_service.py`

**Interfaces:**
- Consumes: `rules.*`, `render.*` (Task 3); `outbox.tg_send(..., task_id, task_card_staff_id)`, `outbox.tg_task_edit` (Task 2).
- Produces: `class NoCoordinator(Exception)`; `today() -> date`; `async create(s, creator, *, kind, description, requester=None, due_date=None, file_path=None, file_name=None, file_kind=None) -> Task`; `async can_help(s, t, staff) -> bool`; внутренние `_send_card(s, t, st, names, *, header=None, with_file=True)`, `_push(s, t)`; `notify(s, t, people, text, *, skip_id=None) -> None`.

- [ ] **Step 1: Тесты**

Create `tests/test_tasks_service.py`:

```python
from datetime import date

import pytest
from sqlalchemy import select

from helpdesk.db.models import Task, TaskEvent
from helpdesk.tasks import service as tasks
from tests.factories import clear_outbox, make_staff, payloads


async def _team(s):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    tima = await make_staff(s, tg_id=2, name="Тимур")
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    return erlan, tima, coord


def _cards(ps):
    return {p["chat_id"]: p for p in ps if p.get("task_card_staff_id")}


async def test_helpdesk_creates_helpdesk_task_for_self(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await tasks.create(
        s, erlan, kind="helpdesk", description="Картридж", requester="каб. 214",
        due_date=date(2026, 10, 10),
    )
    assert (t.status, t.assignee_id, t.creator_id, t.taken_at) == (
        "in_progress", erlan.id, erlan.id, clock_.now,
    )
    cards = _cards(await payloads(s, "tg"))
    assert set(cards) == {1, 2, 3}
    assert cards[1]["buttons"] == [[["✅ Закрыть", f"tk_close:{t.id}"],
                                    ["🆘 Нужна помощь", f"tk_help:{t.id}"]]]
    assert cards[2]["buttons"] is None and cards[3]["buttons"] is None
    assert cards[2]["text"].startswith(f"📌 З-{t.id} · Helpdesk · срок 10.10")
    assert all(p["task_id"] == t.id for p in cards.values())
    assert (await s.scalars(select(TaskEvent.type))).all() == ["created"]


async def test_helpdesk_sends_coordinator_task(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await tasks.create(s, erlan, kind="coordinator", description="Нужен ноутбук")
    assert (t.status, t.assignee_id, t.taken_at) == ("queued", None, None)
    cards = _cards(await payloads(s, "tg"))
    assert set(cards) == {1, 3}  # Тимур не видит
    assert cards[3]["buttons"] == [[["✅ Принять", f"tk_take:{t.id}"]]]
    assert cards[1]["buttons"] == [[["✅ Закрыть", f"tk_close:{t.id}"]]]


async def test_coordinator_creates_both_kinds_for_self(s, clock_):
    erlan, tima, coord = await _team(s)
    own = await tasks.create(s, coord, kind="coordinator", description="Закупка")
    assert (own.status, own.assignee_id) == ("in_progress", coord.id)
    assert set(_cards(await payloads(s, "tg"))) == {3}
    await clear_outbox(s)
    hd = await tasks.create(s, coord, kind="helpdesk", description="Мышь")
    assert (hd.status, hd.assignee_id) == ("in_progress", coord.id)
    assert set(_cards(await payloads(s, "tg"))) == {1, 2, 3}


async def test_coordinator_task_without_coordinator_is_refused(s, clock_):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    await make_staff(s, tg_id=3, name="Ушла", role="coordinator", status="disabled")
    with pytest.raises(tasks.NoCoordinator):
        await tasks.create(s, erlan, kind="coordinator", description="X")
    assert (await s.scalars(select(Task))).all() == []
    assert await payloads(s) == []


async def test_attachment_goes_before_card(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await tasks.create(
        s, erlan, kind="helpdesk", description="Ошибка", file_path="/f/a.jpg",
        file_name="photo_a.jpg", file_kind="photo",
    )
    to_tima = [p for p in await payloads(s, "tg") if p["chat_id"] == 2]
    assert [(p["file_path"], p.get("task_card_staff_id")) for p in to_tima] == [
        ("/f/a.jpg", None), (None, tima.id),
    ]
    assert to_tima[0]["text"] == f"📎 З-{t.id}" and to_tima[0]["file_kind"] == "photo"


async def test_help_button_hidden_when_nobody_else_can_do(s, clock_):
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    await make_staff(s, tg_id=1, name="Ерлан")
    t = await tasks.create(s, coord, kind="coordinator", description="Закупка")
    assert _cards(await payloads(s, "tg"))[3]["buttons"] == [[["✅ Закрыть", f"tk_close:{t.id}"]]]
```

- [ ] **Step 2: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_tasks_service.py`
Expected: FAIL (`ModuleNotFoundError: helpdesk.tasks.service`).

- [ ] **Step 3: service.py (создание и рассылка)**

```python
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
```

(Остальные импорты добавляются в задачах 5–6, когда появляется код, который их использует: задача 5 — `from dataclasses import dataclass`, `from sqlalchemy import select`; задача 6 — `or_`, `TaskComment`, `TaskLink`, `from helpdesk.tickets.service import TG_FILE_KIND`.)

- [ ] **Step 4: Проверить и закоммитить**

Run: `make check` — Expected: PASS.

```bash
git add src/helpdesk/tasks/service.py tests/test_tasks_service.py
git commit -m "feat(tasks): create task and send cards per delivery rules

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Взять/принять, «Нужна помощь», освобождение, закрытие

**Files:**
- Modify: `src/helpdesk/tasks/service.py`
- Test: `tests/test_tasks_work.py`

**Interfaces:**
- Consumes: Task 4.
- Produces: `@dataclass TakeResult(ok: bool, holder_name: str | None)`; `async take(s, task_id, staff) -> TakeResult`; `async ask_help(s, task_id, staff) -> str` (`"ok" | "nobody" | "denied"`); `async release(s, task_id, former: Staff) -> bool`; `async close(s, task_id, staff) -> bool`.

- [ ] **Step 1: Тесты**

Create `tests/test_tasks_work.py`:

```python
import asyncio

from sqlalchemy import select

from helpdesk.db.models import Staff, Task, TaskEvent
from helpdesk.tasks import service as tasks
from tests.factories import clear_outbox, make_staff, make_task, payloads


async def _team(s):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    tima = await make_staff(s, tg_id=2, name="Тимур")
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    return erlan, tima, coord


def _texts(ps, chat_id):
    return [p["text"] for p in ps if p["op"] == "send" and p["chat_id"] == chat_id]


async def test_coordinator_accepts_and_creator_is_notified(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await make_task(s, erlan, kind="coordinator", status="queued", assignee=None)
    r = await tasks.take(s, t.id, coord)
    assert r == tasks.TakeResult(True, "Айжан")
    await s.refresh(t)
    assert (t.status, t.assignee_id, t.taken_at) == ("in_progress", coord.id, clock_.now)
    ps = await payloads(s, "tg")
    assert _texts(ps, 1) == [f"✅ Айжан принял(а) З-{t.id}"]
    edits = {p["chat_id"]: p for p in ps if p["op"] == "task_edit"}
    assert set(edits) == {1, 3}
    assert edits[3]["buttons"] == [[["✅ Закрыть", f"tk_close:{t.id}"]]]
    assert (await s.scalars(select(TaskEvent.type))).all() == ["accepted"]


async def test_take_refusals(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await make_task(s, erlan, kind="coordinator", status="queued", assignee=None)
    assert await tasks.take(s, t.id, tima) == tasks.TakeResult(False, None)  # не может
    await tasks.take(s, t.id, coord)
    assert await tasks.take(s, t.id, coord) == tasks.TakeResult(False, "Айжан")
    assert await tasks.take(s, 999, coord) == tasks.TakeResult(False, None)


async def test_help_goes_to_on_shift_colleagues_only(s, clock_):
    erlan, tima, coord = await _team(s)
    coord.on_shift = False
    t = await make_task(s, erlan)
    await clear_outbox(s)
    assert await tasks.ask_help(s, t.id, erlan) == "ok"
    await s.refresh(t)
    assert (t.status, t.assignee_id) == ("queued", None)
    ps = await payloads(s, "tg")
    new_cards = [p for p in ps if p["op"] == "send" and p.get("task_card_staff_id")]
    assert [p["chat_id"] for p in new_cards] == [2]
    assert new_cards[0]["text"].startswith(f"🆘 Ерлан просит помощи\n📌 З-{t.id}")
    assert new_cards[0]["buttons"] == [[["✋ Взять", f"tk_take:{t.id}"]]]
    edits = {p["chat_id"]: p["buttons"] for p in ps if p["op"] == "task_edit"}
    assert edits[1] == [[["✋ Взять", f"tk_take:{t.id}"]], [["✅ Закрыть", f"tk_close:{t.id}"]]]
    await clear_outbox(s)
    assert await tasks.take(s, t.id, tima) == tasks.TakeResult(True, "Тимур")
    assert _texts(await payloads(s, "tg"), 1) == [f"🤝 З-{t.id} взял(а) Тимур"]
    types = (await s.scalars(select(TaskEvent.type).order_by(TaskEvent.id))).all()
    assert types == ["help", "taken"]


async def test_help_when_nobody_on_shift_keeps_task(s, clock_):
    erlan, tima, coord = await _team(s)
    tima.on_shift = coord.on_shift = False
    t = await make_task(s, erlan)
    await clear_outbox(s)
    assert await tasks.ask_help(s, t.id, erlan) == "nobody"
    await s.refresh(t)
    assert (t.status, t.assignee_id) == ("in_progress", erlan.id)
    assert await payloads(s) == []


async def test_help_denied_for_non_assignee(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await make_task(s, erlan)
    assert await tasks.ask_help(s, t.id, tima) == "denied"


async def test_release_moves_task_to_queue(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await make_task(s, erlan)
    await clear_outbox(s)
    erlan.status = "disabled"
    assert await tasks.release(s, t.id, erlan) is True
    await s.refresh(t)
    assert (t.status, t.assignee_id) == ("queued", None)
    new_cards = [p for p in await payloads(s, "tg") if p.get("task_card_staff_id")]
    assert sorted(p["chat_id"] for p in new_cards) == [2, 3]
    assert new_cards[0]["text"].startswith("🆘 Ерлан отключён — задаче нужен исполнитель")
    assert await tasks.release(s, t.id, erlan) is False


async def test_close_rights_and_notifications(s, clock_):
    erlan, tima, coord = await _team(s)
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    t = await make_task(s, erlan, assignee=tima)
    assert await tasks.close(s, t.id, coord) is False  # не исполнитель, не создатель, не админ
    await clear_outbox(s)
    assert await tasks.close(s, t.id, erlan) is True  # создатель
    await s.refresh(t)
    assert (t.status, t.closed_by_id, t.closed_at) == ("closed", erlan.id, clock_.now)
    ps = await payloads(s, "tg")
    notified = sorted(p["chat_id"] for p in ps if p["op"] == "send")
    assert notified == [2, 3, 9]
    assert _texts(ps, 2) == [f"✅ З-{t.id} закрыта (Ерлан)"]
    assert all(p["buttons"] is None for p in ps if p["op"] == "task_edit")
    assert await tasks.close(s, t.id, erlan) is False
    t2 = await make_task(s, erlan)
    assert await tasks.close(s, t2.id, admin) is True


async def test_concurrent_take_has_one_winner(sf, clock_):
    async with sf() as s, s.begin():
        erlan, tima, coord = await _team(s)
        t = await make_task(s, erlan, status="queued", assignee=None)
        t.taken_at = clock_.now
        ids = (erlan.id, tima.id, t.id)

    async def attempt(staff_id):
        async with sf() as s, s.begin():
            st = await s.get(Staff, staff_id)
            return await tasks.take(s, ids[2], st)

    r1, r2 = await asyncio.gather(attempt(ids[0]), attempt(ids[1]))
    assert sorted([r1.ok, r2.ok]) == [False, True]
    async with sf() as s:
        t = await s.get(Task, ids[2])
        winner = r1 if r1.ok else r2
        loser = r2 if r1.ok else r1
        assert loser.holder_name == winner.holder_name
        assert t.assignee_id in ids[:2]
```

(Админ получает «закрыта», потому что он активный helpdesk-специалист по роли — участник задачи типа Helpdesk.)

- [ ] **Step 2: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_tasks_work.py`
Expected: FAIL (`AttributeError: module 'helpdesk.tasks.service' has no attribute 'take'`).

- [ ] **Step 3: Реализация (дописать в service.py)**

```python
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
        s, t, event="help", staff_id=staff.id,
        header=f"🆘 {staff.name} просит помощи", recipients=others,
    )
    return "ok"


async def release(s, task_id: int, former: Staff) -> bool:
    """Исполнителя отключили — задача уходит в очередь. В событии staff_id = бывший исполнитель."""
    t = await _locked(s, task_id)
    if t is None or t.status != "in_progress" or t.assignee_id != former.id:
        return False
    await _to_queue(
        s, t, event="released", staff_id=former.id,
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
```

Важно: в `test_help_goes_to_on_shift_colleagues_only` бывший исполнитель (Ерлан, создатель) после «помощи» видит в отредактированной карточке и «Взять», и «Закрыть» — это ожидаемо (`buttons_for` для `queued`).

- [ ] **Step 4: Проверить и закоммитить**

Run: `make check` — Expected: PASS.

```bash
git add src/helpdesk/tasks/service.py tests/test_tasks_work.py
git commit -m "feat(tasks): take/accept with row lock, ask for help, release and close

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Комментарии и выборки (мои, очередь, открытые, поиск, видимость)

**Files:**
- Modify: `src/helpdesk/tasks/service.py`
- Test: `tests/test_tasks_queries.py`

**Interfaces:**
- Consumes: Tasks 3–5.
- Produces: `async comment(s, t, staff, *, kind, text, file_path=None, file_name=None) -> TaskComment | None`; `async task_for_reply(s, chat_id, message_id) -> Task | None`; `async link(s, chat_id, message_id, task_id) -> None`; `async my_tasks(s, staff) -> list[Task]`; `async queued_for(s, staff) -> list[Task]`; `async deliver_queue_to(s, staff) -> int`; `async open_tasks(s, staff) -> list[Task]`; `async search(s, staff, q, limit=10) -> list[Task]`; `async get_visible(s, staff, task_id) -> Task | None`; `async recent_comments(s, task_id, limit=5) -> list[TaskComment]`.

- [ ] **Step 1: Тесты**

Create `tests/test_tasks_queries.py`:

```python
from datetime import date

from helpdesk.db.models import TaskComment
from helpdesk.tasks import service as tasks
from tests.factories import clear_outbox, make_staff, make_task, payloads


async def _team(s):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    tima = await make_staff(s, tg_id=2, name="Тимур")
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    return erlan, tima, coord


async def test_comment_goes_to_other_participants(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await make_task(s, erlan)
    c = await tasks.comment(s, t, tima, kind="text", text="Я посмотрю")
    assert c.id and c.task_id == t.id
    ps = await payloads(s, "tg")
    assert sorted(p["chat_id"] for p in ps) == [1, 3]
    assert {p["text"] for p in ps} == {f"💬 З-{t.id} · Тимур: Я посмотрю"}
    assert all(p["task_id"] == t.id for p in ps)
    assert await payloads(s, "wa") == []


async def test_comment_with_file_and_on_closed_task(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await make_task(s, erlan, kind="coordinator", assignee=coord)
    await tasks.comment(s, t, coord, kind="image", text=None, file_path="/f/x.jpg",
                        file_name="x.jpg")
    [p] = await payloads(s, "tg")
    assert (p["chat_id"], p["text"], p["file_kind"]) == (1, f"💬 З-{t.id} · Айжан", "photo")
    await clear_outbox(s)
    t.status = "closed"
    assert await tasks.comment(s, t, coord, kind="text", text="ещё") is None
    assert await payloads(s) == []


async def test_reply_lookup(s, clock_):
    erlan, *_ = await _team(s)
    t = await make_task(s, erlan)
    await tasks.link(s, 1, 555, t.id)
    assert (await tasks.task_for_reply(s, 1, 555)).id == t.id
    assert await tasks.task_for_reply(s, 1, 556) is None
    assert await tasks.task_for_reply(s, 2, 555) is None


async def test_my_and_queued(s, clock_):
    erlan, tima, coord = await _team(s)
    late = await make_task(s, erlan, due_date=date(2026, 10, 20))
    soon = await make_task(s, erlan, due_date=date(2026, 10, 8))
    nodue = await make_task(s, erlan)
    await make_task(s, erlan, status="closed")
    await make_task(s, tima)
    q_hd = await make_task(s, tima, status="queued", assignee=None)
    q_co = await make_task(s, erlan, kind="coordinator", status="queued", assignee=None)
    assert [t.id for t in await tasks.my_tasks(s, erlan)] == [soon.id, late.id, nodue.id]
    assert [t.id for t in await tasks.queued_for(s, erlan)] == [q_hd.id]
    assert [t.id for t in await tasks.queued_for(s, coord)] == [q_hd.id, q_co.id]


async def test_deliver_queue_on_shift(s, clock_):
    erlan, tima, coord = await _team(s)
    q = await make_task(s, erlan, status="queued", assignee=None)
    q.taken_at = clock_.now
    await clear_outbox(s)
    assert await tasks.deliver_queue_to(s, tima) == 1
    [p] = await payloads(s, "tg")
    assert p["chat_id"] == 2 and p["task_card_staff_id"] == tima.id
    assert p["text"].startswith(f"🆘 Задаче нужен исполнитель\n📌 З-{q.id}")


async def test_helpdesk_cannot_see_foreign_coordinator_tasks(s, clock_):
    erlan, tima, coord = await _team(s)
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    foreign = await make_task(s, erlan, kind="coordinator", assignee=coord,
                              description="Ноутбук для бухгалтерии")
    shared = await make_task(s, erlan, description="Ноутбук не включается")
    assert [t.id for t in await tasks.search(s, tima, "ноутбук")] == [shared.id]
    assert [t.id for t in await tasks.search(s, erlan, "ноутбук")] == [shared.id, foreign.id]
    assert [t.id for t in await tasks.search(s, coord, "НОУТБУК")] == [shared.id, foreign.id]
    assert await tasks.get_visible(s, tima, foreign.id) is None
    assert (await tasks.get_visible(s, admin, foreign.id)).id == foreign.id
    assert [t.id for t in await tasks.open_tasks(s, tima)] == [shared.id]
    assert [t.id for t in await tasks.open_tasks(s, coord)] == [foreign.id, shared.id]


async def test_search_by_requester_and_recent_comments(s, clock_):
    erlan, tima, coord = await _team(s)
    t = await make_task(s, erlan, requester="Иванова, каб. 214")
    assert [x.id for x in await tasks.search(s, tima, "каб. 214")] == [t.id]
    for i in range(7):
        s.add(TaskComment(task_id=t.id, staff_id=erlan.id, kind="text", text=f"c{i}"))
    await s.flush()
    assert [c.text for c in await tasks.recent_comments(s, t.id)] == ["c2", "c3", "c4", "c5", "c6"]
```

- [ ] **Step 2: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_tasks_queries.py`
Expected: FAIL (`AttributeError: ... has no attribute 'comment'`).

- [ ] **Step 3: Реализация (дописать в service.py)**

```python
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
        task_id=t.id, staff_id=staff.id, kind=kind, text=text,
        file_path=file_path, file_name=file_name,
    )
    s.add(c)
    head = f"💬 {render.number(t)} · {staff.name}"
    body = f"{head}: {text}" if text else head
    for st in await rules.participants(s, t):
        if st.id == staff.id:
            continue
        if file_path:
            outbox.tg_send(
                s, st.tg_chat_id, text=body, file_path=file_path,
                file_kind=TG_FILE_KIND[kind], task_id=t.id,
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
    rows = await queued_for(s, staff)
    names = await queries.staff_names(s)
    for t in rows:
        await _send_card(s, t, staff, names, header="🆘 Задаче нужен исполнитель", with_file=False)
    return len(rows)


async def open_tasks(s, staff: Staff) -> list[Task]:
    q = (
        select(Task)
        .where(Task.status != "closed", rules.visible_cond(staff))
        .order_by(Task.id)
    )
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
```

Импорт: `from sqlalchemy import or_, select`.

В `test_helpdesk_cannot_see_foreign_coordinator_tasks` поиск отсортирован по `id desc`: `shared` создан позже `foreign`, поэтому идёт первым.

- [ ] **Step 4: Проверить и закоммитить**

Run: `make check` — Expected: PASS.

```bash
git add src/helpdesk/tasks/service.py tests/test_tasks_queries.py
git commit -m "feat(tasks): comments, reply lookup, my/queued/open lists and visibility-aware search

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Напоминания о сроках

**Files:**
- Create: `src/helpdesk/tasks/reminders.py`
- Modify: `src/helpdesk/reminders.py`, `src/helpdesk/main.py`
- Test: `tests/test_tasks_reminders.py`

**Interfaces:**
- Consumes: `service.notify`, `rules.coordinators`, `render.number`, `render.short`.
- Produces: `async tasks.reminders.run_once(sf, remind_hour: int) -> int` (сколько задач обработано); `reminders.run(sf, after_min, stop, interval=60.0, task_remind_hour=9)`.

- [ ] **Step 1: Тесты**

Create `tests/test_tasks_reminders.py` (фикстура `clock_` стартует в 09:00 UTC = 14:00 Алматы, 07.10.2026):

```python
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from helpdesk.db.models import Task, TaskEvent
from helpdesk.tasks import reminders
from tests.factories import make_staff, make_task, payloads

TODAY = date(2026, 10, 7)


async def _setup(sf, clock_):
    async with sf() as s, s.begin():
        erlan = await make_staff(s, tg_id=1, name="Ерлан")
        await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
        due = await make_task(s, erlan, due_date=TODAY, description="Сегодня")
        due.created_at = clock_.now - timedelta(days=2)
        over = await make_task(s, erlan, due_date=TODAY - timedelta(days=1), description="Вчера")
        await make_task(s, erlan, due_date=TODAY + timedelta(days=1))
        await make_task(s, erlan, status="closed", due_date=TODAY - timedelta(days=3))
        return due.id, over.id


async def test_no_reminders_before_hour(sf, clock_):
    await _setup(sf, clock_)
    clock_.now = datetime(2026, 10, 7, 2, 0, tzinfo=UTC)  # 07:00 Алматы
    assert await reminders.run_once(sf, 9) == 0


async def test_due_and_overdue_once_per_day(sf, clock_):
    due_id, over_id = await _setup(sf, clock_)
    assert await reminders.run_once(sf, 9) == 2
    async with sf() as s:
        ps = await payloads(s, "tg")
        texts = {(p["chat_id"], p["text"]) for p in ps}
        assert texts == {
            (1, f"⏰ Сегодня срок: З-{due_id}\nСегодня"),
            (1, f"⚠️ Просрочена З-{over_id} (срок был 06.10)\nВчера"),
            (3, f"⚠️ Просрочена З-{over_id} (срок был 06.10)\nВчера"),
        }
        assert all(p["task_id"] in (due_id, over_id) for p in ps)
        assert (await s.get(Task, due_id)).last_reminded_on == TODAY
        types = sorted((await s.scalars(select(TaskEvent.type))).all())
        assert types == ["reminded_due", "reminded_overdue"]
    assert await reminders.run_once(sf, 9) == 0  # повтор в тот же день — тишина
    clock_.advance(days=1)
    # назавтра: две просрочены + задача «на завтра» стала «сегодня» (создана вчера)
    assert await reminders.run_once(sf, 9) == 3


async def test_overdue_without_assignee_goes_to_creator_and_no_duplicates(sf, clock_):
    async with sf() as s, s.begin():
        coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
        t = await make_task(s, coord, status="queued", assignee=None,
                            due_date=TODAY - timedelta(days=1))
        tid = t.id
    assert await reminders.run_once(sf, 9) == 1
    async with sf() as s:
        ps = await payloads(s, "tg")
    assert [(p["chat_id"], p["task_id"]) for p in ps] == [(3, tid)]  # создатель = координатор, один раз


async def test_due_today_created_today_is_not_reminded(sf, clock_):
    async with sf() as s, s.begin():
        erlan = await make_staff(s, tg_id=1, name="Ерлан")
        t = await make_task(s, erlan, due_date=TODAY)
        tid = t.id
    assert await reminders.run_once(sf, 9) == 1
    async with sf() as s:
        assert await payloads(s, "tg") == []
        assert (await s.get(Task, tid)).last_reminded_on == TODAY
```

В `tests/test_reminders.py` ничего не меняется — `run_once` для заявок прежний.

- [ ] **Step 2: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_tasks_reminders.py`
Expected: FAIL (`ImportError: cannot import name 'reminders' from 'helpdesk.tasks'`).

- [ ] **Step 3: tasks/reminders.py**

```python
from sqlalchemy import or_, select

from helpdesk import clock
from helpdesk.db.models import Staff, Task, TaskEvent
from helpdesk.tasks import render, rules
from helpdesk.tasks import service as tasks


async def run_once(sf, remind_hour: int) -> int:
    local_now = clock.local(clock.utcnow())
    if local_now.hour < remind_hour:
        return 0
    today = local_now.date()
    async with sf() as s, s.begin():
        q = (
            select(Task)
            .where(
                Task.status != "closed",
                Task.due_date <= today,
                or_(Task.last_reminded_on.is_(None), Task.last_reminded_on < today),
            )
            .order_by(Task.id)
            .with_for_update(skip_locked=True)
        )
        due = list((await s.scalars(q)).all())
        coords = await rules.coordinators(s)
        for t in due:
            t.last_reminded_on = today
            # создал сегодня со сроком «сегодня» — человек и так в курсе
            if t.due_date == today and clock.local(t.created_at).date() == today:
                continue
            owner = await s.get(Staff, t.assignee_id or t.creator_id)
            body = render.short(t.description)
            if t.due_date == today:
                text = f"⏰ Сегодня срок: {render.number(t)}\n{body}"
                people, event = [owner], "reminded_due"
            else:
                text = f"⚠️ Просрочена {render.number(t)} (срок был {t.due_date:%d.%m})\n{body}"
                people, event = [owner, *coords], "reminded_overdue"
            tasks.notify(s, t, people, text)
            s.add(TaskEvent(task_id=t.id, type=event))
    return len(due)
```

- [ ] **Step 4: Подключить к общему циклу**

`src/helpdesk/reminders.py` — импорт `from helpdesk.tasks import reminders as task_reminders`, и `run`:

```python
async def run(
    sf, after_min: int, stop: asyncio.Event, interval: float = 60.0, task_remind_hour: int = 9
) -> None:
    while not stop.is_set():
        try:
            await run_once(sf, after_min)
        except Exception:
            log.exception("reminders failed")
        try:
            await task_reminders.run_once(sf, task_remind_hour)
        except Exception:
            log.exception("task reminders failed")
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except TimeoutError:
            pass
```

`src/helpdesk/main.py`:

```python
        asyncio.create_task(
            reminders.run(
                sf, settings.remind_after_min, stop, task_remind_hour=settings.task_remind_hour
            )
        ),
```

- [ ] **Step 5: Проверить и закоммитить**

Run: `make check` — Expected: PASS.

```bash
git add src/helpdesk/tasks/reminders.py src/helpdesk/reminders.py src/helpdesk/main.py tests/test_tasks_reminders.py
git commit -m "feat(tasks): morning due/overdue reminders, once per day

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Специалисты — роль, смена, отключение

**Files:**
- Modify: `src/helpdesk/staff/service.py`
- Test: `tests/test_staff.py`

**Interfaces:**
- Consumes: `tasks.deliver_queue_to`, `tasks.release` (Tasks 5–6).
- Produces: `async toggle_role(s, admin, staff_id) -> bool`; `staff_line` показывает «🧭 координатор»; `staff_buttons` с кнопкой `stf_role:<id>` (и для себя); `set_shift(on=True)` доставляет и очередь задач; `disable` освобождает задачи.

- [ ] **Step 1: Тесты**

В `tests/test_staff.py` заменить `test_staff_line_and_buttons` и добавить новые (импорт `from tests.factories import make_task`, `from helpdesk.db.models import Task` где нужно):

```python
async def test_staff_line_and_buttons(s, clock_):
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    u = await make_staff(s, tg_id=1, name="Тимур")
    c = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    assert staff_svc.staff_line(u) == "Тимур — активен, на смене"
    assert staff_svc.staff_line(admin) == "👑 Админ — активен, на смене"
    assert staff_svc.staff_line(c) == "Айжан · 🧭 координатор — активен, на смене"
    assert staff_svc.staff_buttons(u, admin) == [
        [("⛔ Отключить", f"stf_dis:{u.id}"), ("👑 Сделать админом", f"stf_adm:{u.id}")],
        [("🧭 Сделать координатором", f"stf_role:{u.id}")],
    ]
    assert staff_svc.staff_buttons(c, admin)[1] == [("Сделать helpdesk", f"stf_role:{c.id}")]
    assert staff_svc.staff_buttons(admin, admin) == [
        [("🧭 Сделать координатором", f"stf_role:{admin.id}")]
    ]


async def test_toggle_role(s, clock_):
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    u = await make_staff(s, tg_id=1, name="Тимур")
    off = await make_staff(s, tg_id=2, name="Ушёл", status="disabled")
    assert await staff_svc.toggle_role(s, admin, u.id) and u.role == "coordinator"
    assert await staff_svc.toggle_role(s, admin, u.id) and u.role == "helpdesk"
    assert await staff_svc.toggle_role(s, admin, admin.id) and admin.role == "coordinator"
    assert not await staff_svc.toggle_role(s, admin, off.id)
    assert not await staff_svc.toggle_role(s, u, admin.id)  # не админ


async def test_shift_on_delivers_task_queue_too(s, clock_):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    tima = await make_staff(s, tg_id=2, name="Тимур", on_shift=False)
    await make_task(s, erlan, status="queued", assignee=None)
    assert await staff_svc.set_shift(s, tima, True) == 0  # заявок в очереди нет
    ps = await payloads(s, "tg")
    assert [p["chat_id"] for p in ps if p.get("task_card_staff_id")] == [2]


async def test_disable_releases_tasks_to_queue(s, clock_):
    from helpdesk.db.models import Task

    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    tima = await make_staff(s, tg_id=2, name="Тимур")
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    t = await make_task(s, tima)
    await clear_outbox(s)
    assert await staff_svc.disable(s, admin, tima.id)
    await s.refresh(t)
    assert (t.status, t.assignee_id) == ("queued", None)
    cards = [p for p in await payloads(s, "tg") if p.get("task_card_staff_id")]
    assert sorted(p["chat_id"] for p in cards) == [1, 9]
    assert (await s.get(Task, t.id)).status == "queued"
```

- [ ] **Step 2: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_staff.py`
Expected: FAIL (`AttributeError: ... 'toggle_role'`, неверные кнопки).

- [ ] **Step 3: Реализация**

В `src/helpdesk/staff/service.py`: импорты `from helpdesk.db.models import Staff, Task, TgCard, Ticket` и `from helpdesk.tasks import service as tasks`.

В `disable` после цикла по заявкам:

```python
    tq = select(Task.id).where(Task.status == "in_progress", Task.assignee_id == st.id)
    for task_id in (await s.scalars(tq)).all():
        await tasks.release(s, task_id, st)
```

Новая функция после `toggle_admin`:

```python
async def toggle_role(s, admin: Staff, staff_id: int) -> bool:
    st = await _target(s, admin, staff_id)
    if st is None or st.status != "active":
        return False
    st.role = "helpdesk" if st.role == "coordinator" else "coordinator"
    return True
```

`set_shift`, ветка `on`:

```python
    n = await tickets.deliver_queue_to(s, staff)
    await tasks.deliver_queue_to(s, staff)
    return n
```

`staff_line` и `staff_buttons`:

```python
def staff_line(st: Staff) -> str:
    crown = "👑 " if st.is_admin else ""
    nick = f" (@{st.username})" if st.username else ""
    role = " · 🧭 координатор" if st.role == "coordinator" else ""
    shift = ", на смене" if st.status == "active" and st.on_shift else ""
    return f"{crown}{st.name}{nick}{role} — {STATUS_RU[st.status]}{shift}"


def _role_button(st: Staff) -> tuple[str, str]:
    if st.role == "coordinator":
        return ("Сделать helpdesk", f"stf_role:{st.id}")
    return ("🧭 Сделать координатором", f"stf_role:{st.id}")


def staff_buttons(st: Staff, me: Staff) -> Buttons | None:
    if st.id == me.id:
        return [[_role_button(st)]]
    if st.status == "active":
        adm = "Снять админа" if st.is_admin else "👑 Сделать админом"
        return [
            [("⛔ Отключить", f"stf_dis:{st.id}"), (adm, f"stf_adm:{st.id}")],
            [_role_button(st)],
        ]
    return [[("✅ Включить", f"stf_en:{st.id}")]]
```

- [ ] **Step 4: Проверить и закоммитить**

Run: `make check` — Expected: PASS.

```bash
git add src/helpdesk/staff/service.py tests/test_staff.py
git commit -m "feat(staff): coordinator role toggle, task queue on shift start, release tasks on disable

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Статистика и выгрузка

**Files:**
- Modify: `src/helpdesk/stats/service.py`, `src/helpdesk/stats/export.py`
- Test: `tests/test_stats.py`

**Interfaces:**
- Consumes: `Task`, `render.KIND_RU`, `render.KIND_PLAIN`, `render.STATUS_RU`.
- Produces: `@dataclass TaskStats(created, closed, overdue_now, closed_late, by_kind: dict[str, tuple[int, int]], per_staff: list[tuple[str, int]])`; `async compute_tasks(s, start, end, today) -> TaskStats`; `render_task_stats(ts) -> str`; `export.TASK_HEADERS`; лист «Задачи» в `export_xlsx`.

- [ ] **Step 1: Тесты**

В конец `tests/test_stats.py` (импорты: `from datetime import date`, `from helpdesk.db.models import Task`, `from tests.factories import make_task`):

```python
async def _tasks_dataset(s, now):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    on_time = await make_task(s, erlan, status="closed", due_date=date(2026, 10, 7))
    on_time.closed_at, on_time.closed_by_id = now, erlan.id
    late = await make_task(s, erlan, kind="coordinator", assignee=coord, status="closed",
                           due_date=date(2026, 10, 6), description="=SUM(A1)")
    late.closed_at, late.closed_by_id = now, coord.id
    overdue = await make_task(s, erlan, due_date=date(2026, 10, 5), requester="каб. 1")
    old = await make_task(s, erlan)
    old.created_at = now - timedelta(days=40)
    await s.flush()
    return on_time, late, overdue, old


async def test_compute_tasks(s, clock_):
    await _tasks_dataset(s, clock_.now)
    start, end = stats.period_bounds("today", clock_.now)
    ts = await stats.compute_tasks(s, start, end, date(2026, 10, 7))
    assert (ts.created, ts.closed, ts.overdue_now, ts.closed_late) == (3, 2, 1, 1)
    assert ts.by_kind == {"helpdesk": (2, 1), "coordinator": (1, 1)}
    assert ts.per_staff == [("Айжан", 1), ("Ерлан", 1)]
    text = stats.render_task_stats(ts)
    assert text.splitlines()[:3] == [
        "📌 Задачи",
        "Создано: 3 · Закрыто: 2 (с опозданием: 1)",
        "Просрочено сейчас: 1",
    ]
    assert "• 🧭 Координатор — создано 1, закрыто 1" in text


async def test_export_has_tasks_sheet(s, clock_):
    on_time, late, overdue, old = await _tasks_dataset(s, clock_.now)
    data = await export.export_xlsx(s, *stats.period_bounds("today", clock_.now))
    ws = load_workbook(BytesIO(data))["Задачи"]
    rows = list(ws.iter_rows(values_only=True))
    assert list(rows[0]) == export.TASK_HEADERS
    assert [r[0] for r in rows[1:]] == [f"З-{on_time.id}", f"З-{late.id}", f"З-{overdue.id}"]
    assert list(rows[2]) == [
        f"З-{late.id}", "Координатор", "закрыта", "'=SUM(A1)", None, "Ерлан", "Айжан",
        datetime(2026, 10, 6), datetime(2026, 10, 7, 14, 0), datetime(2026, 10, 7, 14, 0), "да",
    ]
    assert rows[1][-1] == "нет" and rows[3][-1] == "да" and rows[3][4] == "каб. 1"
```

(openpyxl читает дату срока как `datetime` с нулевым временем — это ожидаемо.)

- [ ] **Step 2: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_stats.py`
Expected: FAIL (`AttributeError: ... 'compute_tasks'`).

- [ ] **Step 3: stats/service.py**

Импорты: `from collections import Counter`, `from datetime import date, datetime, timedelta`, `from helpdesk.db.models import Client, Task, Ticket, TicketEvent`, `from helpdesk.tasks.render import KIND_RU`, `from helpdesk.tasks.rules import KINDS`.

```python
@dataclass
class TaskStats:
    created: int
    closed: int
    overdue_now: int
    closed_late: int
    by_kind: dict[str, tuple[int, int]]
    per_staff: list[tuple[str, int]]


async def compute_tasks(s, start, end, today: date) -> TaskStats:
    cq = (
        select(Task.kind, func.count())
        .where(*between(Task.created_at, start, end))
        .group_by(Task.kind)
    )
    created = dict((await s.execute(cq)).all())
    closed = list(
        (
            await s.scalars(
                select(Task).where(Task.status == "closed", *between(Task.closed_at, start, end))
            )
        ).all()
    )
    overdue = await s.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.status != "closed", Task.due_date < today)
    )
    closed_kind = Counter(t.kind for t in closed)
    by_kind = {
        k: (created.get(k, 0), closed_kind.get(k, 0))
        for k in KINDS
        if created.get(k) or closed_kind.get(k)
    }
    late = sum(1 for t in closed if t.due_date and clock.local(t.closed_at).date() > t.due_date)
    names = await queries.staff_names(s)
    per = Counter(t.closed_by_id for t in closed)
    per_staff = sorted(((names.get(i, "?"), n) for i, n in per.items()), key=lambda x: (-x[1], x[0]))
    return TaskStats(sum(created.values()), len(closed), overdue, late, by_kind, per_staff)


def render_task_stats(ts: TaskStats) -> str:
    lines = [
        "📌 Задачи",
        f"Создано: {ts.created} · Закрыто: {ts.closed} (с опозданием: {ts.closed_late})",
        f"Просрочено сейчас: {ts.overdue_now}",
    ]
    lines += [f"• {KIND_RU[k]} — создано {c}, закрыто {cl}" for k, (c, cl) in ts.by_kind.items()]
    if ts.per_staff:
        lines += ["Закрыли:"] + [f"• {name} — {n}" for name, n in ts.per_staff]
    return "\n".join(lines)
```

- [ ] **Step 4: stats/export.py**

Импорты: `from datetime import date, datetime`, `from helpdesk.db.models import Client, Message, Task, Ticket`, `from helpdesk.tasks.render import KIND_PLAIN`, `from helpdesk.tasks.render import STATUS_RU as TASK_STATUS_RU`.

```python
TASK_HEADERS = [
    "№", "Тип", "Статус", "Описание", "Для кого", "Создал", "Исполнитель",
    "Срок", "Создана", "Закрыта", "С опозданием",
]
TASK_WIDTHS = [8, 14, 16, 50, 24, 16, 16, 11, 17, 17, 13]


def _layout(ws, widths: list[int]) -> None:
    for i, width in enumerate(widths):
        ws.column_dimensions[chr(ord("A") + i)].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, datetime):
                cell.number_format = "DD.MM.YYYY HH:MM"
            elif isinstance(cell.value, date):
                cell.number_format = "DD.MM.YYYY"


def _late(t: Task, today: date) -> str | None:
    if t.due_date is None:
        return None
    day = clock.local(t.closed_at).date() if t.closed_at else today
    return "да" if day > t.due_date else "нет"
```

В `export_xlsx` заменить цикл ширин/форматов листа «Заявки» на `_layout(ws, WIDTHS)` и перед `buf = BytesIO()` добавить:

```python
    tq = select(Task).where(*between(Task.created_at, start, end)).order_by(Task.id)
    today = clock.local(clock.utcnow()).date()
    ws2 = wb.create_sheet("Задачи")
    ws2.append(TASK_HEADERS)
    for t in (await s.scalars(tq)).all():
        ws2.append(
            [
                f"З-{t.id}",
                KIND_PLAIN[t.kind],
                TASK_STATUS_RU[t.status],
                _safe(t.description),
                _safe(t.requester),
                names.get(t.creator_id),
                names.get(t.assignee_id or t.closed_by_id),
                t.due_date,
                _naive(t.created_at),
                _naive(t.closed_at),
                _late(t, today),
            ]
        )
    _layout(ws2, TASK_WIDTHS)
```

Для незакрытой задачи «С опозданием» = «да», если срок уже прошёл (сравнение с сегодня); в спецификации это колонка «с опозданием (да/нет)».

- [ ] **Step 5: Проверить и закоммитить**

Run: `make check` — Expected: PASS (старые тесты выгрузки читают `.active` — это по-прежнему лист «Заявки»).

```bash
git add src/helpdesk/stats tests/test_stats.py
git commit -m "feat(stats): task block in /stats and Tasks sheet in Excel export

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Меню и диалог создания задачи

**Files:**
- Create: `src/helpdesk/telegram/filters.py`, `src/helpdesk/telegram/task_handlers.py`
- Modify: `src/helpdesk/telegram/handlers.py` (импорт фильтров, `setup`), `src/helpdesk/telegram/menu.py`
- Test: `tests/test_menu.py`, `tests/test_task_handlers.py`

**Interfaces:**
- Consumes: `tasks.create`, `tasks.NoCoordinator`, `tasks.today`, `render.*`, `extract_content`, `TG_FILE_KIND`.
- Produces: `filters.IsActive`, `filters.IsAdmin`, `filters.ID_DATA`, `filters.arg_id`; `menu.NEW_TASK = "➕ Новая задача"`; `task_handlers.router`, `task_handlers.TaskForm`; `handlers.setup` подключает `task_handlers.router` раньше общего.

- [ ] **Step 1: Вынести фильтры**

Create `src/helpdesk/telegram/filters.py` — перенести из `handlers.py` без изменений:

```python
from aiogram.filters import Filter
from aiogram.types import CallbackQuery

from helpdesk.db.models import Staff

# id из callback_data: только цифры и не больше int4 — подделка уходит в cb_fallback
ID_DATA = r"^{}:\d{{1,9}}$"


class IsActive(Filter):
    async def __call__(self, event, staff: Staff | None = None) -> bool:
        return staff is not None and staff.status == "active"


class IsAdmin(Filter):
    async def __call__(self, event, staff: Staff | None = None) -> bool:
        return staff is not None and staff.status == "active" and staff.is_admin


def arg_id(cq: CallbackQuery) -> int:
    return int(cq.data.split(":", 1)[1])
```

В `handlers.py` удалить классы `IsActive`, `IsAdmin`, `_ID_DATA`, `_arg_id` и импортировать:

```python
from helpdesk.telegram.filters import ID_DATA as _ID_DATA
from helpdesk.telegram.filters import IsActive, IsAdmin
from helpdesk.telegram.filters import arg_id as _arg_id
```

(убрать `Filter` из импорта `aiogram.filters`, если больше не нужен). Run: `make check` — PASS (рефакторинг без изменения поведения).

- [ ] **Step 2: Тесты меню и диалога**

`tests/test_menu.py` — новые ожидания:

```python
def test_specialist_menu_on_shift():
    assert menu.menu_rows(_staff()) == [
        ["📋 Мои заявки", "📂 Открытые"],
        ["➕ Новая задача"],
        ["🔍 Поиск", "📊 Статистика"],
        ["📥 Выгрузка", "🔴 Уйти со смены"],
    ]


def test_shift_button_follows_state_and_admin_row():
    rows = menu.menu_rows(_staff(on_shift=False, is_admin=True))
    assert rows[3][1] == "🟢 Выйти на смену"
    assert rows[-1] == ["👥 Специалисты"]
```

Create `tests/test_task_handlers.py`:

```python
from datetime import date
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
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
        "helpdesk", "Заменить картридж", None, date(2026, 10, 10),
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
```

- [ ] **Step 3: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_menu.py tests/test_task_handlers.py`
Expected: FAIL (`ImportError: cannot import name 'task_handlers'`).

- [ ] **Step 4: menu.py**

```python
NEW_TASK = "➕ Новая задача"

MENU_TEXTS = frozenset(
    {MY, OPEN, NEW_TASK, SEARCH, STATS, EXPORT, SHIFT_OFF, SHIFT_ON, STAFF, CANCEL}
)


def menu_rows(staff: Staff) -> list[list[str]]:
    rows = [
        [MY, OPEN],
        [NEW_TASK],
        [SEARCH, STATS],
        [EXPORT, SHIFT_OFF if staff.on_shift else SHIFT_ON],
    ]
    if staff.is_admin:
        rows.append([STAFF])
    return rows
```

- [ ] **Step 5: task_handlers.py (диалог)**

```python
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
from helpdesk.telegram import menu
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
NOT_CANCEL = F.text != menu.CANCEL


def leaves_dialog(text: str | None) -> bool:
    """Команда или кнопка меню посреди диалога: диалог завершаем, кнопку обрабатываем как обычно."""
    if not text:
        return False
    return text.startswith("/") or (text in menu.MENU_TEXTS and text not in (menu.CANCEL, menu.NEW_TASK))


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
    await cq.message.answer("Опишите задачу текстом. Можно прислать фото или файл с подписью.")
    await cq.answer()


async def _ask_requester(message, state: FSMContext) -> None:
    await state.set_state(TaskForm.requester)
    await message.answer(
        "Для кого задача? ФИО или кабинет — или «Пропустить».", reply_markup=markup(SKIP_BUTTONS)
    )


def _too_long(text: str) -> str | None:
    if len(text) > render.MAX_DESCRIPTION:
        return f"Слишком длинно ({len(text)} символов), максимум {render.MAX_DESCRIPTION}. Сократите."
    return None


@router.message(TaskForm.description, IsActive(), F.text.func(menu.is_client_reply_text))
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


@router.message(TaskForm.description, IsActive(), NOT_CANCEL)
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
    await message.answer(render.summary(await state.get_data()), reply_markup=markup(CONFIRM_BUTTONS))


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


@router.callback_query(F.data.startswith("tkn_"))
async def stale_dialog(cq: CallbackQuery):
    await cq.answer("Диалог уже завершён", show_alert=True)
```

Порядок регистрации важен: `new_task` → `leave_dialog` → обработчики состояний → `stale_dialog`. «✖️ Отмена» не ловится ни одним обработчиком этого router'а (`is_client_reply_text` и `NOT_CANCEL` её исключают) и доходит до существующего `menu_cancel` в общем router'е, который чистит состояние и возвращает меню. Обработчики кнопок карточек (`ID_DATA`, `arg_id`) добавляются в задаче 11.

Заметка для теста `test_full_dialog_creates_task`: `pick_kind`, `skip_requester`, `pick_due` вызываются с `Cq`, у которого `message` — `Msg` с методами `edit_text`/`edit_reply_markup`/`answer`.

- [ ] **Step 6: Подключить router**

`handlers.py`: импорт `from helpdesk.telegram import menu, task_handlers` и:

```python
def setup(dp: Dispatcher, sf) -> None:
    mw = DbMiddleware(sf)
    for r in (task_handlers.router, router):
        r.message.outer_middleware(mw)
        r.callback_query.outer_middleware(mw)
    dp.include_router(task_handlers.router)  # раньше общего: у общего есть catch-all
    dp.include_router(router)
```

- [ ] **Step 7: Проверить и закоммитить**

Run: `make check` — Expected: PASS.

```bash
git add src/helpdesk/telegram tests/test_menu.py tests/test_task_handlers.py
git commit -m "feat(telegram): New task menu button and step-by-step creation dialog

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Кнопки карточек, комментарии, «Мои», «Открытые», поиск, роль в /staff, /stats

**Files:**
- Modify: `src/helpdesk/telegram/task_handlers.py`, `src/helpdesk/telegram/handlers.py`
- Test: `tests/test_task_handlers.py`, `tests/test_telegram.py`

**Interfaces:**
- Consumes: `tasks.take/ask_help/close/comment/task_for_reply/link/my_tasks/queued_for/open_tasks/search/get_visible/recent_comments/can_help/today`, `render.*`, `rules.can_see`, `stats.compute_tasks/render_task_stats`, `staff_svc.toggle_role`.
- Produces: обработчики `cb_task_take`, `cb_task_help`, `cb_task_close` (task_handlers); в `handlers.py` — `_comment_task`, `_send_task_details`, обновлённые `cmd_my`, `cmd_open`, `_find`, `cb_stats`, `_reply_to_client`, `HELP`, роль в `_STAFF_ACTIONS`.

- [ ] **Step 1: Тесты**

В импорты в начале `tests/test_task_handlers.py` добавить `from tests.factories import make_staff, make_task`, в конец файла:

```python
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
```

В импорты в начале `tests/test_telegram.py` добавить: `from sqlalchemy import select`, `from helpdesk.db.models import Outbox, TaskComment`, `from helpdesk.tasks import service as tasks_svc`, `from tests.factories import make_task`. В конец файла:

```python
async def test_reply_to_task_card_is_comment_never_whatsapp(s, clock_, tmp_path):
    c = await make_client(s)
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    tima = await make_staff(s, tg_id=7, name="Тимур")
    ticket = await make_ticket(s, c, status="in_progress", assignee=erlan)
    erlan.active_ticket_id = ticket.id
    task = await make_task(s, tima)
    await tasks_svc.link(s, 6, 3333, task.id)
    await s.flush()
    msg = FakeChatMessage(6, text="Сейчас подойду", message_id=4343,
                          reply_to_message=SimpleNamespace(message_id=3333))
    await handlers._reply_to_client(msg, s, erlan, FakeBot(), SimpleNamespace(files_dir=str(tmp_path)))
    assert msg.answers == [f"💬 → З-{task.id} ✓"]
    assert (await s.scalars(select(Outbox).where(Outbox.channel == "wa"))).all() == []
    [cm] = (await s.scalars(select(TaskComment))).all()
    assert (cm.task_id, cm.text) == (task.id, "Сейчас подойду")
    assert (await tasks_svc.task_for_reply(s, 6, 4343)).id == task.id


async def test_reply_to_closed_or_hidden_task_is_refused(s, clock_, tmp_path):
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    coord = await make_staff(s, tg_id=8, name="Айжан", role="coordinator")
    closed = await make_task(s, erlan, status="closed")
    hidden = await make_task(s, coord, kind="coordinator")
    await tasks_svc.link(s, 6, 1, closed.id)
    await tasks_svc.link(s, 6, 2, hidden.id)
    settings = SimpleNamespace(files_dir=str(tmp_path))
    m1 = FakeChatMessage(6, text="a", reply_to_message=SimpleNamespace(message_id=1))
    await handlers._reply_to_client(m1, s, erlan, FakeBot(), settings)
    assert m1.answers == [f"З-{closed.id} уже закрыта — комментарий не отправлен."]
    m2 = FakeChatMessage(6, text="b", reply_to_message=SimpleNamespace(message_id=2))
    await handlers._reply_to_client(m2, s, erlan, FakeBot(), settings)
    assert m2.answers == ["Задача недоступна."]
    assert (await s.scalars(select(TaskComment))).all() == []


async def test_my_lists_tickets_and_tasks(s, clock_):
    from datetime import date

    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    mine = await make_task(s, erlan, due_date=date(2026, 10, 6), description="Картридж")
    queued = await make_task(s, erlan, status="queued", assignee=None, description="Мышь")
    msg = FakeChatMessage(6)
    await handlers.cmd_my(msg, s, erlan)
    assert msg.answers == [
        f"Мои задачи:\n⚠️ З-{mine.id} · до 06.10 · Картридж\n\n"
        f"Ждут исполнителя:\nЗ-{queued.id} · без срока · Мышь"
    ]
    empty = FakeChatMessage(7)
    other = await make_staff(s, tg_id=7, name="Тимур", role="coordinator")
    queued.status = "closed"
    await s.flush()
    await handlers.cmd_my(empty, s, other)
    assert empty.answers == ["У вас нет заявок и задач в работе."]


async def test_find_task_by_number_and_text(s, clock_):
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    tima = await make_staff(s, tg_id=7, name="Тимур")
    t = await make_task(s, erlan, description="Ноутбук не включается")
    m = FakeChatMessage(7)
    await handlers._find(m, s, tima, f"з-{t.id}")
    assert m.answers[0].startswith(f"📌 З-{t.id} · Helpdesk")
    assert (await tasks_svc.task_for_reply(s, 7, _Sent._next)).id == t.id
    m2 = FakeChatMessage(7)
    await handlers._find(m2, s, tima, "ноутбук")
    assert m2.answers == [
        f"Задачи:\nЗ-{t.id} · 07.10 · Ноутбук не включается · в работе · Ерлан\n\n"
        "Подробнее: /find З-<номер>"
    ]
    m3 = FakeChatMessage(7)
    await handlers._find(m3, s, tima, "З-999")
    assert m3.answers == ["Задача не найдена."]


async def test_open_lists_visible_tasks(s, clock_):
    erlan = await make_staff(s, tg_id=6, name="Ерлан")
    t = await make_task(s, erlan, description="Картридж")
    msg = FakeChatMessage(6)
    await handlers.cmd_open(msg, s, erlan)
    assert msg.answers == [
        f"Открытых заявок нет 🎉\n\nОткрытые задачи:\nЗ-{t.id} · Helpdesk · Ерлан · Картридж"
    ]


@pytest.mark.parametrize(
    "data,name",
    [("tk_take:12", "cb_task_take"), ("tk_help:3", "cb_task_help"), ("tk_close:4", "cb_task_close")],
)
async def test_task_callbacks_reach_handlers(data, name):
    from helpdesk.telegram import task_handlers

    admin = SimpleNamespace(status="active", is_admin=True)
    found = ""
    for h in task_handlers.router.callback_query.handlers:
        passed, _ = await h.check(_cq(data), staff=admin)
        if passed:
            found = h.callback.__name__
            break
    assert found == name


@pytest.mark.parametrize("data", ["tk_take:abc", "tk_close:1:2", "tk_help:99999999999"])
async def test_malformed_task_callbacks_not_handled_by_task_router(data):
    from helpdesk.telegram import task_handlers

    admin = SimpleNamespace(status="active", is_admin=True)
    for h in task_handlers.router.callback_query.handlers:
        passed, _ = await h.check(_cq(data), staff=admin)
        assert not passed, h.callback.__name__
    assert await _matched_handler(data) == "cb_fallback"
```

В `test_router_setup_registers_handlers` ничего менять не нужно.

(В `test_open_lists_visible_tasks` ожидается `open_tickets_text` без заявок — «Открытых заявок нет 🎉».)

- [ ] **Step 2: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_task_handlers.py tests/test_telegram.py`
Expected: FAIL (`AttributeError: ... 'cb_task_help'` и др.).

- [ ] **Step 3: Кнопки карточек (task_handlers.py)**

Добавить **перед** `stale_dialog` (порядок не критичен — префиксы разные, но держим кнопки карточек вместе):

```python
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
```

(В тесте `Cq.answer(text, show_alert=...)` записывает только текст.)

- [ ] **Step 4: handlers.py**

Импорты: `from helpdesk.db import queries`, `from helpdesk.tasks import render as task_render`, `from helpdesk.tasks import rules as task_rules`, `from helpdesk.tasks import service as tasks`.

`HELP` — добавить перед «То же самое — кнопками меню…»:

```
"➕ Новая задача — создать внутреннюю задачу (тип, описание, для кого, срок)\n"
"Комментарий к задаче — «Ответить» на её карточку или сообщение\n"
"/find З-12 — карточка задачи\n"
```

`cmd_my`:

```python
async def cmd_my(message: Message, s, staff: Staff):
    rows = await tickets.my_tickets(s, staff)
    mine = await tasks.my_tasks(s, staff)
    waiting = await tasks.queued_for(s, staff)
    if not rows and not mine and not waiting:
        await message.answer("У вас нет заявок и задач в работе.")
        return
    today = tasks.today()
    parts, buttons = [], None
    if rows:
        lines = [
            f"{'▶️ ' if t.id == staff.active_ticket_id else ''}#{t.id} · {_cname(c)}"
            for t, c in rows
        ]
        parts.append("Ваши заявки в работе:\n" + "\n".join(lines))
        buttons = [[(f"Перейти к #{t.id}", f"goto:{t.id}")] for t, _ in rows]
    if mine:
        parts.append("Мои задачи:\n" + "\n".join(task_render.list_line(t, today) for t in mine))
    if waiting:
        parts.append(
            "Ждут исполнителя:\n" + "\n".join(task_render.list_line(t, today) for t in waiting)
        )
    await message.answer("\n\n".join(parts)[:4000], reply_markup=markup(buttons))
```

`cmd_open`:

```python
async def cmd_open(message: Message, s, staff: Staff):
    text = await stats.open_tickets_text(s)
    open_tasks = await tasks.open_tasks(s, staff)
    if open_tasks:
        names = await queries.staff_names(s)
        today = tasks.today()
        lines = [task_render.open_line(t, names, today) for t in open_tasks]
        text += "\n\nОткрытые задачи:\n" + "\n".join(lines)
    await message.answer(text[:4000])
```

Новая функция и `_find`:

```python
async def _send_task_details(message: Message, s, staff: Staff, task_id: int) -> None:
    t = await tasks.get_visible(s, staff, task_id)
    if t is None:
        await message.answer("Задача не найдена.")
        return
    names = await queries.staff_names(s)
    text = task_render.details(t, names, await tasks.recent_comments(s, t.id), tasks.today())
    help_ok = t.assignee_id == staff.id and await tasks.can_help(s, t, staff)
    buttons = task_render.buttons_for(t, staff, can_help=help_ok) or []
    has_close = any(data.startswith("tk_close:") for row in buttons for _, data in row)
    if staff.is_admin and t.status != "closed" and not has_close:
        buttons = [*buttons, [("✅ Закрыть", f"tk_close:{t.id}")]]
    sent = await message.answer(text, reply_markup=markup(buttons))
    await tasks.link(s, staff.tg_chat_id, sent.message_id, t.id)


async def _find(message: Message, s, staff: Staff, q: str) -> None:
    tid = stats.parse_ticket_id(q)
    if tid is not None:
        await _send_details(message, s, staff, tid)
        return
    task_id = task_render.parse_task_id(q)
    if task_id is not None:
        await _send_task_details(message, s, staff, task_id)
        return
    rows = await stats.search(s, q)
    found = await tasks.search(s, staff, q)
    if not rows and not found:
        await message.answer("Ничего не найдено.")
        return
    parts = []
    if rows:
        lines = [stats.search_line(t, c, name) for t, c, name in rows]
        parts.append("\n".join(lines) + "\n\nПодробнее: /find <номер>")
    if found:
        names = await queries.staff_names(s)
        lines = [task_render.search_line(t, names) for t in found]
        parts.append("Задачи:\n" + "\n".join(lines) + "\n\nПодробнее: /find З-<номер>")
    await message.answer("\n\n".join(parts)[:4000])
```

`cmd_find` — текст подсказки: `"Использование: /find 125 | З-12 | ФИО | кабинет | телефон"`; `menu_search` — `"Введите номер заявки, З-номер задачи, ФИО, кабинет или телефон:"`.

`cb_stats`:

```python
async def cb_stats(cq: CallbackQuery, s):
    kind = cq.data.split(":", 1)[1]
    start, end = stats.period_bounds(kind, clock.utcnow())
    text = stats.render_stats(await stats.compute(s, start, end), stats.PERIOD_TITLES[kind])
    ts = await stats.compute_tasks(s, start, end, tasks.today())
    if ts.created or ts.closed or ts.overdue_now:
        text += "\n\n" + stats.render_task_stats(ts)
    await cq.message.answer(text[:4096])
    await cq.answer()
```

Роль в `/staff`:

```python
_STAFF_ACTIONS = {
    "stf_dis": staff_svc.disable,
    "stf_en": staff_svc.enable,
    "stf_adm": staff_svc.toggle_admin,
    "stf_role": staff_svc.toggle_role,
}


@router.callback_query(F.data.regexp(r"^stf_(dis|en|adm|role):\d{1,9}$"), IsAdmin())
```

Комментарии — новая функция и начало `_reply_to_client`:

```python
async def _comment_task(message: Message, s, staff: Staff, bot: Bot, settings: Settings, t) -> None:
    n = task_render.number(t)
    if not task_rules.can_see(staff, t):
        await message.answer("Задача недоступна.")
        return
    if t.status == "closed":
        await message.answer(f"{n} уже закрыта — комментарий не отправлен.")
        return
    try:
        content = await extract_content(bot, message, settings.files_dir)
    except FileTooBig:
        await message.answer("Файл больше 20 МБ — бот не может его скачать. Отправьте ссылкой.")
        return
    await tasks.comment(
        s, t, staff, kind=content.kind, text=content.text,
        file_path=content.file_path, file_name=content.file_name,
    )
    await tasks.link(s, staff.tg_chat_id, message.message_id, t.id)
    await message.answer(f"💬 → {n} ✓")


async def _reply_to_client(message: Message, s, staff: Staff, bot: Bot, settings: Settings):
    reply_to = message.reply_to_message.message_id if message.reply_to_message else None
    if reply_to is not None:
        # сначала задачи: ответ на карточку задачи никогда не уходит в WhatsApp
        task = await tasks.task_for_reply(s, staff.tg_chat_id, reply_to)
        if task is not None:
            await _comment_task(message, s, staff, bot, settings, task)
            return
    t, err = await tickets.resolve_target(s, staff, reply_to)
    ...  # дальше без изменений
```

- [ ] **Step 5: Проверить и закоммитить**

Run: `make check` — Expected: PASS.

```bash
git add src/helpdesk/telegram tests/test_task_handlers.py tests/test_telegram.py
git commit -m "feat(telegram): task card buttons, comments via reply, tasks in /my, /open, search, /stats and role in /staff

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Документация и чек-лист приёмки

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Дополнить чек-лист**

В `README.md`, в конец раздела «Чек-лист ручной приёмки»:

```markdown
### Внутренние задачи

Нужны: два helpdesk-специалиста (Ерлан, Тимур) и координатор (Айжан).

17. [ ] Админ в `/staff` нажал «🧭 Сделать координатором» у Айжан → в списке «Айжан · 🧭 координатор».
18. [ ] Ерлан: «➕ Новая задача» → Helpdesk → «Заменить картридж» → «каб. 214» → «Завтра» → «Создать» → «Задача З-1 создана». У Ерлана карточка с «Закрыть / 🆘 Нужна помощь», у Тимура и Айжан — без кнопок.
19. [ ] Ерлан: новая задача типа «Координатор» → карточка с «✅ Принять» только у Айжан (у Тимура нет). Айжан приняла → Ерлану «✅ Айжан принял(а) З-2».
20. [ ] Ерлан на З-1 жмёт «🆘 Нужна помощь» → Тимуру и Айжан (на смене) «🆘 Ерлан просит помощи» с «Взять». Оба жмут почти одновременно → взял один, второй видит «Уже взял(а) …».
21. [ ] «Ответить» на карточку З-1 → у остальных «💬 З-1 · …». При этом у Ерлана есть активная заявка WhatsApp — пользователю в WhatsApp ничего не приходит.
22. [ ] Посреди создания задачи нажать «📋 Мои заявки» → диалог прерван, показан список; «✖️ Отмена» на любом шаге → «Отменено».
23. [ ] Задача со сроком «Сегодня», созданная вчера (поправить `due_date`/`created_at` в БД) → в `TASK_REMIND_HOUR` приходит «⏰ Сегодня срок»; на следующий день — «⚠️ Просрочена» исполнителю и Айжан.
24. [ ] `/my`, `/open`, `/find З-1`, `/find картридж` показывают задачи; Тимур не находит задачу «Координатор», созданную Айжан для себя.
25. [ ] `/stats` — блок «📌 Задачи»; `/export` — лист «Задачи».
26. [ ] `/staff` → отключить Тимура с задачей в работе → задача у остальных на смене с «Взять».
```

В разделе «Установка» (там, где перечислены переменные `.env`), если перечисление есть, добавить строку про `TASK_REMIND_HOUR` — «час напоминаний о сроках задач, по умолчанию 9».

- [ ] **Step 2: Финальная проверка**

Run: `make test-db && make check`
Expected: все тесты PASS (155 старых + новые), ruff чисто.

Run (миграция на рабочей базе разработчика, если поднят Docker Compose): `docker compose run --rm bot alembic upgrade head` — без ошибок.

- [ ] **Step 3: Коммит**

```bash
git add README.md
git commit -m "docs: acceptance checklist for internal tasks

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
