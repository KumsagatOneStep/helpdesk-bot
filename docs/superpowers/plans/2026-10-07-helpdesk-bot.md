# Helpdesk-бот WhatsApp → Telegram — план реализации v1

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Бот принимает обращения на общий номер WhatsApp (Green API) и раздаёт их как заявки специалистам в Telegram. Первый нажавший «Взять» получает заявку, у остальных карточка исчезает. Переписка идёт через бота, всё хранится в PostgreSQL, статистика и выгрузка в Excel доступны командами.

**Architecture:** Один asyncio-процесс, в нём четыре задачи: long polling Green API, long polling Telegram (aiogram 3), воркер outbox и таймер напоминаний. Бизнес-логика (`tickets`, `intake`, `staff`, `stats`) работает с базой через SQLAlchemy async. Всё исходящее она пишет в таблицу `outbox` в той же транзакции. Отправку делает только воркер через интерфейсы `WaSender`/`TgSender`.

**Tech Stack:** Python 3.12, uv, aiogram 3, SQLAlchemy 2 (asyncio) + asyncpg, Alembic, httpx, openpyxl, structlog, pydantic-settings, pytest + pytest-asyncio, PostgreSQL 16, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-07-helpdesk-bot-design.md`

## Global Constraints

- Python `>=3.12`, зависимости только через `pyproject.toml` + `uv.lock`; код в `src/helpdesk/`, тесты в `tests/`.
- Текущее время только через `clock.utcnow()` с импортом модуля (`from helpdesk import clock`). Никогда `from helpdesk.clock import utcnow`: тесты подменяют `helpdesk.clock.utcnow`.
- В базе все `datetime` с часовым поясом (UTC). Для показа людям используется `clock.local(dt)` (пояс из `TZ`, по умолчанию `Asia/Almaty`).
- Сервисы **не делают commit**: транзакцией владеет вызывающий код (middleware Telegram, поллер, воркер, таймер).
- Любая отправка пользователю или специалисту идёт через `helpdesk.delivery.outbox`. Исключения — ответы на нажатия кнопок (`callback.answer`) и прямые ответы на команды специалиста (`message.answer`).
- Тексты для пользователей WhatsApp лежат только в `helpdesk/texts.py` (ключи `ru` и `kk`). Интерфейс специалистов на русском.
- Telegram: без `parse_mode` (простой текст); текст ≤ 4096 символов, подпись ≤ 1024; файлы ботам доступны до 20 МБ.
- Форматы `callback_data`: `take:<id>`, `close:<id>`, `ret:<id>`, `goto:<id>`, `chat:<id>`, `approve:<staff_id>`, `reject:<staff_id>`, `stf_dis:<id>`, `stf_en:<id>`, `stf_adm:<id>`, `stats:<today|week|month>`, `export:<month|prev_month|all>`.
- Статусы заявки: `new`, `in_progress`, `closed`. Статусы специалиста: `pending`, `active`, `disabled`. Состояния карточки (`tg_cards.state`): `open`, `deleting`, `deleted`, `marked`, `kept`. Статусы outbox: `pending`, `sent`, `skipped`, `failed`.
- Тесты требуют PostgreSQL на `localhost:5433` (`make test-db`); переменная `TEST_DATABASE_URL` переопределяет адрес.
- Сообщения коммитов на английском, в конце строка `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Ответ специалиста через «Ответить» на сообщение закрытой или чужой заявки.** Сообщение не должно уйти в другую (активную) заявку: специалист получает отказ. Тест в задаче 6 (`test_reply_to_closed_ticket_is_refused_not_redirected`).
2. **Пользователь пишет несколько сообщений подряд, пока заявка в очереди.** У специалиста не должны копиться дубли карточек с «Взять»: старые удаляются, а ещё не отправленные карточки прошлой рассылки пропускаются. Тесты в задаче 5 (`test_next_message_while_queued_rebroadcasts`) и задаче 3 (`test_stale_card_is_skipped`).
3. **Очень длинный текст или много сообщений в заявке.** Карточка обрезается до лимита Telegram, а не падает с ошибкой. Тест в задаче 5 (`test_queue_card_clips_long_text`).
4. **Одно и то же уведомление Green API приходит дважды** (падение между коммитом и `deleteNotification`). Второе сообщение и вторая заявка не создаются. Тесты в задаче 7 (`test_duplicate_notification_ignored`) и задаче 12 (`test_poller_deletes_after_commit`).
5. **У специалиста две заявки, активную он закрыл и пишет обычный текст.** Текст не должен молча уйти во вторую заявку: бот отвечает «Нет активной заявки». Тест в задаче 6 (`test_no_active_after_close`).

## Отклонения от спецификации, принятые при планировании

Спецификация уже обновлена (коммит после c6ed372):
- порты реализованы как outbox;
- новое сообщение в заявку из очереди вызывает повторную рассылку, а не редактирование карточек;
- «Ответить» на закрытую или чужую заявку отклоняется, а не перенаправляется;
- звонки в v1 не обрабатываются;
- бэкап делается в полночь.

## Структура файлов

```
helpdesk-bot/
  pyproject.toml, uv.lock, Makefile, alembic.ini, Dockerfile, docker-compose.yml,
  .env.example, .dockerignore, .gitignore, README.md
  migrations/env.py, migrations/versions/<rev>_initial_schema.py
  src/helpdesk/
    __init__.py
    config.py            — Settings из .env
    clock.py             — utcnow(), local(), set_local_tz()
    texts.py             — тексты для WhatsApp RU/KK
    files.py             — new_file_path() для вложений
    inbound.py           — handle_incoming(): точка входа сообщения WhatsApp
    reminders.py         — run_once()/run(): напоминания по очереди
    main.py              — сборка и запуск
    db/__init__.py, db/models.py, db/session.py, db/queries.py
    delivery/__init__.py, delivery/outbox.py (постановка), delivery/worker.py (отправка)
    whatsapp/__init__.py, whatsapp/parse.py, whatsapp/client.py, whatsapp/poller.py
    intake/__init__.py, intake/service.py
    tickets/__init__.py, tickets/render.py, tickets/service.py
    staff/__init__.py, staff/service.py
    stats/__init__.py, stats/service.py, stats/export.py
    telegram/__init__.py, telegram/gateway.py, telegram/media.py, telegram/parsing.py,
    telegram/middleware.py, telegram/handlers.py
  tests/
    __init__.py, conftest.py, factories.py, fakes.py, test_*.py
```

---

### Task 1: Каркас проекта, настройки, часы

**Files:**
- Create: `pyproject.toml`, `Makefile`, `.gitignore`, `.env.example`, `src/helpdesk/__init__.py`, `src/helpdesk/config.py`, `src/helpdesk/clock.py`, `tests/__init__.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings` (поля `telegram_bot_token`, `admin_tg_ids: str`, `green_api_url`, `green_api_media_url`, `green_api_id_instance`, `green_api_token`, `database_url`, `tz`, `remind_after_min: int`, `files_dir`; свойства `admin_ids: set[int]`, `media_url: str`). `clock.utcnow() -> datetime`, `clock.local(dt) -> datetime`, `clock.set_local_tz(name: str) -> None`, `clock.LOCAL_TZ: ZoneInfo`.

- [ ] **Step 1: Создать `pyproject.toml`**

```toml
[project]
name = "helpdesk-bot"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "aiogram>=3.13,<4",
  "sqlalchemy[asyncio]>=2.0.36",
  "asyncpg>=0.30",
  "alembic>=1.14",
  "pydantic-settings>=2.6",
  "httpx>=0.27",
  "openpyxl>=3.1",
  "structlog>=24.4",
  "tzdata>=2024.2",
]

[dependency-groups]
dev = ["pytest>=8.3", "pytest-asyncio>=1.0", "ruff>=0.8"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/helpdesk"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
testpaths = ["tests"]
pythonpath = ["."]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
ignore = ["E501"]  # длинные строки-тексты для пользователей не переносим
```

- [ ] **Step 2: Создать `Makefile`, `.gitignore`, `.env.example`**

`Makefile` (отступы — табуляция):
```make
TEST_DB_URL ?= postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test

.PHONY: test-db test lint check

test-db:
	docker start helpdesk-test-db 2>/dev/null || docker run -d --name helpdesk-test-db \
	  -p 5433:5432 -e POSTGRES_USER=helpdesk -e POSTGRES_PASSWORD=helpdesk \
	  -e POSTGRES_DB=helpdesk_test postgres:16

test:
	TEST_DATABASE_URL=$(TEST_DB_URL) uv run pytest -q

lint:
	uv run ruff check src tests && uv run ruff format --check src tests

check: lint test
```

`.gitignore`:
```
.venv/
__pycache__/
*.pyc
.env
data/
backups/
.pytest_cache/
.ruff_cache/
```

`.env.example`:
```
TELEGRAM_BOT_TOKEN=
# Telegram ID администраторов через запятую (узнать: @userinfobot)
ADMIN_TG_IDS=
GREEN_API_URL=https://api.green-api.com
# Хост для отправки файлов из личного кабинета Green API (mediaUrl); пусто = GREEN_API_URL
GREEN_API_MEDIA_URL=
GREEN_API_ID_INSTANCE=
GREEN_API_TOKEN=
POSTGRES_PASSWORD=change-me
# Для локального запуска; в Docker Compose переопределяется
DATABASE_URL=postgresql+asyncpg://helpdesk:change-me@localhost:5432/helpdesk
TZ=Asia/Almaty
REMIND_AFTER_MIN=10
FILES_DIR=/data/files
```

- [ ] **Step 3: Написать падающий тест `tests/test_config.py`** (также создать пустой `tests/__init__.py`)

```python
from datetime import UTC, datetime

from helpdesk import clock
from helpdesk.config import Settings

BASE = dict(
    telegram_bot_token="t",
    green_api_id_instance="1101",
    green_api_token="g",
    database_url="postgresql+asyncpg://x/y",
)


def test_admin_ids_parsed_and_media_url_defaults():
    s = Settings(_env_file=None, admin_tg_ids="111, 222,", **BASE)
    assert s.admin_ids == {111, 222}
    assert s.media_url == "https://api.green-api.com"
    assert s.remind_after_min == 10


def test_media_url_override():
    s = Settings(_env_file=None, green_api_media_url="https://7103.media.green-api.com/", **BASE)
    assert s.media_url == "https://7103.media.green-api.com"


def test_local_time_uses_configured_zone():
    clock.set_local_tz("Asia/Almaty")
    dt = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
    assert clock.local(dt).utcoffset().total_seconds() == 5 * 3600
    assert clock.utcnow().tzinfo is not None
```

- [ ] **Step 4: Запустить, убедиться, что падает**

Run: `uv sync && uv run pytest tests/test_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'helpdesk'` (или `helpdesk.config`).

- [ ] **Step 5: Реализация**

`src/helpdesk/__init__.py` — пустой файл.

`src/helpdesk/clock.py`:
```python
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Almaty")


def utcnow() -> datetime:
    return datetime.now(UTC)


def set_local_tz(name: str) -> None:
    global LOCAL_TZ
    LOCAL_TZ = ZoneInfo(name)


def local(dt: datetime) -> datetime:
    return dt.astimezone(LOCAL_TZ)
```

`src/helpdesk/config.py`:
```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str
    admin_tg_ids: str = ""
    green_api_url: str = "https://api.green-api.com"
    green_api_media_url: str = ""
    green_api_id_instance: str
    green_api_token: str
    database_url: str
    tz: str = "Asia/Almaty"
    remind_after_min: int = 10
    files_dir: str = "/data/files"

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.admin_tg_ids.replace(" ", "").split(",") if x}

    @property
    def media_url(self) -> str:
        return (self.green_api_media_url or self.green_api_url).rstrip("/")
```

- [ ] **Step 6: Тесты и линтер зелёные**

Run: `uv run pytest tests/test_config.py -q && uv run ruff check --fix src tests && uv run ruff format src tests`
Expected: `3 passed`, ruff без ошибок.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "chore: project scaffold, settings and clock

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Модели БД, миграция Alembic, тестовая инфраструктура

**Files:**
- Create: `src/helpdesk/db/__init__.py`, `src/helpdesk/db/models.py`, `src/helpdesk/db/session.py`, `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`, `migrations/versions/<rev>_initial_schema.py` (генерируется), `tests/conftest.py`, `tests/factories.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `clock.utcnow()`.
- Produces: модели `Base, Client, Staff, Ticket, Message, TicketEvent, TgCard, TgLink, Outbox` (поля как в коде ниже); `make_engine(url) -> AsyncEngine`, `make_session_factory(engine) -> async_sessionmaker[AsyncSession]`. Фикстуры: `engine`, `sf` (session factory, база очищена), `s` (AsyncSession), `clock_` (`FrozenClock` с `.now` и `.advance(**timedelta_kwargs)`, старт 2026-10-07 09:00 UTC). Фабрики: `make_client(s, *, phone="77011234567", full_name="Айгерим Сериккызы", location="Бухгалтерия, каб. 214", lang="ru", step="done")`, `make_staff(s, *, tg_id, name, is_admin=False, status="active", on_shift=True)`, `make_ticket(s, client, *, status="new", assignee=None, text="Не печатает принтер")`, `payloads(s, channel=None) -> list[dict]`, `clear_outbox(s)`.

- [ ] **Step 1: Модели `src/helpdesk/db/models.py`** (и пустой `src/helpdesk/db/__init__.py`)

```python
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from helpdesk import clock


def _now() -> datetime:
    return clock.utcnow()


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[int] = mapped_column(primary_key=True)
    wa_chat_id: Mapped[str] = mapped_column(String(64), unique=True)
    phone: Mapped[str] = mapped_column(String(32))
    wa_name: Mapped[str | None] = mapped_column(String(200))
    full_name: Mapped[str | None] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(200))
    lang: Mapped[str] = mapped_column(String(2), default="ru")
    intake_step: Mapped[str] = mapped_column(String(16), default="lang")
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Staff(Base):
    __tablename__ = "staff"
    id: Mapped[int] = mapped_column(primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    tg_chat_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(200))
    username: Mapped[str | None] = mapped_column(String(64))
    is_admin: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    on_shift: Mapped[bool] = mapped_column(default=True)
    active_ticket_id: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        Index(
            "uq_tickets_open_per_client",
            "client_id",
            unique=True,
            postgresql_where=text("status <> 'closed'"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    status: Mapped[str] = mapped_column(String(16), default="new")
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(default=_now)
    first_taken_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    closed_by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    last_broadcast_at: Mapped[datetime | None]
    broadcast_seq: Mapped[int] = mapped_column(default=0)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), index=True)
    direction: Mapped[str] = mapped_column(String(3))  # in | out
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    kind: Mapped[str] = mapped_column(String(16))  # text|image|document|audio|video|other
    text: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(String(500))
    file_name: Mapped[str | None] = mapped_column(String(255))
    wa_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    intake: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class TicketEvent(Base):
    __tablename__ = "ticket_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    type: Mapped[str] = mapped_column(String(32))  # created|taken|returned|closed|delivery_failed
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(default=_now)


class TgCard(Base):
    __tablename__ = "tg_cards"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    tg_chat_id: Mapped[int] = mapped_column(BigInteger)
    tg_message_id: Mapped[int] = mapped_column(BigInteger)
    has_button: Mapped[bool] = mapped_column(default=False)
    state: Mapped[str] = mapped_column(String(16), default="open")
    sent_at: Mapped[datetime] = mapped_column(default=_now)


class TgLink(Base):
    __tablename__ = "tg_links"
    tg_chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tg_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_pending", "status", "next_attempt_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(2))  # wa | tg
    chat_key: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(default=_now)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=_now)
```

- [ ] **Step 2: `src/helpdesk/db/session.py`**

```python
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
```

- [ ] **Step 3: Alembic**

Run: `uv run alembic init -t async migrations`

В `alembic.ini` строку `sqlalchemy.url = ...` заменить на `sqlalchemy.url =` (адрес берётся из `DATABASE_URL`). Файл `migrations/env.py` заменить целиком:

```python
import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)
target_metadata = Base.metadata


def _url() -> str:
    return os.environ["DATABASE_URL"]


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
```

Сгенерировать первую миграцию на чистой тестовой базе:
```bash
make test-db
sleep 3
DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test \
  uv run alembic revision --autogenerate -m "initial schema"
```
Открыть сгенерированный файл в `migrations/versions/` и проверить, что в нём 8 таблиц и индекс `uq_tickets_open_per_client` с `postgresql_where=sa.text("status <> 'closed'")`. Если `postgresql_where` отсутствует, дописать его вручную в `op.create_index(...)`.

- [ ] **Step 4: `tests/conftest.py`**

```python
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from helpdesk.db.models import Base
from helpdesk.db.session import make_engine, make_session_factory

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test"
)


@pytest.fixture(scope="session")
def _migrated():
    env = {**os.environ, "DATABASE_URL": TEST_DB_URL}
    for args in (["downgrade", "base"], ["upgrade", "head"]):
        subprocess.run([sys.executable, "-m", "alembic", *args], env=env, check=True)


@pytest.fixture(scope="session")
async def engine(_migrated):
    eng = make_engine(TEST_DB_URL)
    yield eng
    await eng.dispose()


@pytest.fixture
async def sf(engine):
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return make_session_factory(engine)


@pytest.fixture
async def s(sf):
    async with sf() as session:
        yield session


class FrozenClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock_(monkeypatch):
    fc = FrozenClock(datetime(2026, 10, 7, 9, 0, tzinfo=UTC))
    monkeypatch.setattr("helpdesk.clock.utcnow", fc)
    return fc
```

- [ ] **Step 5: `tests/factories.py`**

```python
from sqlalchemy import delete, select

from helpdesk.db.models import Client, Message, Outbox, Staff, Ticket


async def make_client(
    s,
    *,
    phone="77011234567",
    full_name="Айгерим Сериккызы",
    location="Бухгалтерия, каб. 214",
    lang="ru",
    step="done",
) -> Client:
    c = Client(
        wa_chat_id=f"{phone}@c.us",
        phone=phone,
        full_name=full_name,
        location=location,
        lang=lang,
        intake_step=step,
    )
    s.add(c)
    await s.flush()
    return c


async def make_staff(s, *, tg_id, name, is_admin=False, status="active", on_shift=True) -> Staff:
    st = Staff(
        tg_user_id=tg_id,
        tg_chat_id=tg_id,
        name=name,
        is_admin=is_admin,
        status=status,
        on_shift=on_shift,
    )
    s.add(st)
    await s.flush()
    return st


async def make_ticket(s, client, *, status="new", assignee=None, text="Не печатает принтер"):
    t = Ticket(client_id=client.id, status=status, assignee_id=assignee.id if assignee else None)
    s.add(t)
    await s.flush()
    s.add(Message(client_id=client.id, ticket_id=t.id, direction="in", kind="text", text=text))
    await s.flush()
    return t


async def payloads(s, channel=None) -> list[dict]:
    q = select(Outbox).order_by(Outbox.id)
    if channel:
        q = q.where(Outbox.channel == channel)
    return [o.payload for o in (await s.scalars(q)).all()]


async def clear_outbox(s) -> None:
    await s.execute(delete(Outbox))
```

- [ ] **Step 6: Тест `tests/test_models.py`**

```python
import pytest
from sqlalchemy.exc import IntegrityError

from helpdesk.db.models import Ticket
from tests.factories import make_client


async def test_only_one_open_ticket_per_client(s, clock_):
    c = await make_client(s)
    s.add(Ticket(client_id=c.id))
    await s.flush()
    s.add(Ticket(client_id=c.id))
    with pytest.raises(IntegrityError):
        await s.flush()


async def test_closed_ticket_does_not_block_new_one(s, clock_):
    c = await make_client(s)
    s.add(Ticket(client_id=c.id, status="closed"))
    s.add(Ticket(client_id=c.id))
    await s.flush()


async def test_defaults_use_clock(s, clock_):
    c = await make_client(s)
    t = Ticket(client_id=c.id)
    s.add(t)
    await s.flush()
    assert t.created_at == clock_.now
    assert t.status == "new" and t.broadcast_seq == 0
```

- [ ] **Step 7: Запуск**

Run: `make test`
Expected: все тесты (`test_config` + `test_models`) PASS. Если `test_only_one_open_ticket_per_client` не падает с IntegrityError, значит в миграции нет `postgresql_where`/`unique` (см. шаг 3).

- [ ] **Step 8: Commit**

```bash
uv run ruff check --fix src tests && uv run ruff format src tests migrations
git add -A
git commit -m "feat(db): models, initial migration, test fixtures

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Outbox и воркер отправки

**Files:**
- Create: `src/helpdesk/db/queries.py`, `src/helpdesk/delivery/__init__.py`, `src/helpdesk/delivery/outbox.py`, `src/helpdesk/delivery/worker.py`, `tests/fakes.py`
- Test: `tests/test_outbox_worker.py`

**Interfaces:**
- Consumes: модели, `clock`.
- Produces:
  - `queries.on_shift_staff(s) -> list[Staff]` (active + on_shift, по id), `queries.active_admins(s) -> list[Staff]`, `queries.staff_names(s) -> dict[int, str]`.
  - `Buttons = list[list[tuple[str, str]]]`; в payload хранятся как `list[list[list[str]]]`.
  - `outbox.wa_text(s, chat_id: str, text: str, *, ticket_id: int | None = None)` → payload `{"op": "text", "chat_id", "text", "ticket_id"}`.
  - `outbox.wa_file(s, chat_id, path, file_name, caption=None, *, ticket_id=None)` → `{"op": "file", "chat_id", "path", "file_name", "caption", "ticket_id"}`.
  - `outbox.tg_send(s, chat_id: int, *, text=None, file_path=None, file_kind=None, buttons=None, ticket_id=None, card_staff_id=None, card_seq=None)` → `{"op": "send", ...все эти ключи}`; `file_kind` ∈ `photo|document|voice|video`.
  - `outbox.tg_edit(s, chat_id, message_id, text, buttons=None)` → `{"op": "edit", ...}`.
  - `outbox.tg_delete_card(s, card: TgCard, fallback_text: str)` → `{"op": "delete_card", "card_id", "fallback_text"}`; ставит `card.state = "deleting"`.
  - `worker.WaSender`/`worker.TgSender` (Protocol), `worker.RETRY_DELAYS = [5, 30, 120, 600, 1800]`, `await worker.process_batch(sf, wa, tg, limit=20) -> int`, `await worker.run(sf, wa, tg, stop: asyncio.Event, idle_sleep=1.0)`.
  - `tests/fakes.py`: `FakeWa(fail_chats=set())` с `.sent: list[tuple]` (`("text", chat_id, text)` / `("file", chat_id, path, file_name, caption)`), `FakeTg()` с `.sent: list[dict]` (ключи `chat_id, text, file_path, file_kind, buttons, mid`), `.edited: list[tuple]` (`(chat_id, mid, text, buttons)`), `.deleted: list[tuple]`, флаги `fail_send`, `fail_delete`.

- [ ] **Step 1: `src/helpdesk/db/queries.py`**

```python
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
```

- [ ] **Step 2: `src/helpdesk/delivery/outbox.py`** (и пустой `delivery/__init__.py`)

```python
from helpdesk.db.models import Outbox, TgCard

Buttons = list[list[tuple[str, str]]]


def _add(s, channel: str, chat_key: str, payload: dict) -> None:
    s.add(Outbox(channel=channel, chat_key=chat_key, payload=payload))


def _buttons(buttons: Buttons | None) -> list | None:
    return [[list(b) for b in row] for row in buttons] if buttons else None


def wa_text(s, chat_id: str, text: str, *, ticket_id: int | None = None) -> None:
    payload = {"op": "text", "chat_id": chat_id, "text": text, "ticket_id": ticket_id}
    _add(s, "wa", f"wa:{chat_id}", payload)


def wa_file(s, chat_id, path, file_name, caption=None, *, ticket_id=None) -> None:
    payload = {
        "op": "file",
        "chat_id": chat_id,
        "path": path,
        "file_name": file_name,
        "caption": caption,
        "ticket_id": ticket_id,
    }
    _add(s, "wa", f"wa:{chat_id}", payload)


def tg_send(
    s,
    chat_id: int,
    *,
    text: str | None = None,
    file_path: str | None = None,
    file_kind: str | None = None,
    buttons: Buttons | None = None,
    ticket_id: int | None = None,
    card_staff_id: int | None = None,
    card_seq: int | None = None,
) -> None:
    payload = {
        "op": "send",
        "chat_id": chat_id,
        "text": text,
        "file_path": file_path,
        "file_kind": file_kind,
        "buttons": _buttons(buttons),
        "ticket_id": ticket_id,
        "card_staff_id": card_staff_id,
        "card_seq": card_seq,
    }
    _add(s, "tg", f"tg:{chat_id}", payload)


def tg_edit(s, chat_id: int, message_id: int, text: str, buttons: Buttons | None = None) -> None:
    payload = {
        "op": "edit",
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "buttons": _buttons(buttons),
    }
    _add(s, "tg", f"tg:{chat_id}", payload)


def tg_delete_card(s, card: TgCard, fallback_text: str) -> None:
    card.state = "deleting"
    payload = {"op": "delete_card", "card_id": card.id, "fallback_text": fallback_text}
    _add(s, "tg", f"tg:{card.tg_chat_id}", payload)
```

- [ ] **Step 3: `tests/fakes.py`**

```python
class FakeWa:
    def __init__(self, fail_chats=None):
        self.sent: list[tuple] = []
        self.fail_chats: set[str] = set(fail_chats or ())

    async def send_text(self, chat_id, text):
        if chat_id in self.fail_chats:
            raise RuntimeError("wa down")
        self.sent.append(("text", chat_id, text))
        return f"wa{len(self.sent)}"

    async def send_file(self, chat_id, path, file_name, caption):
        if chat_id in self.fail_chats:
            raise RuntimeError("wa down")
        self.sent.append(("file", chat_id, path, file_name, caption))
        return f"wa{len(self.sent)}"


class FakeTg:
    def __init__(self):
        self.sent: list[dict] = []
        self.edited: list[tuple] = []
        self.deleted: list[tuple] = []
        self.fail_send = False
        self.fail_delete = False
        self._mid = 1000

    async def send(self, chat_id, *, text, file_path, file_kind, buttons):
        if self.fail_send:
            raise RuntimeError("tg down")
        self._mid += 1
        self.sent.append(
            dict(
                chat_id=chat_id,
                text=text,
                file_path=file_path,
                file_kind=file_kind,
                buttons=buttons,
                mid=self._mid,
            )
        )
        return self._mid

    async def edit(self, chat_id, message_id, text, buttons):
        self.edited.append((chat_id, message_id, text, buttons))

    async def delete(self, chat_id, message_id):
        if self.fail_delete:
            raise RuntimeError("message can't be deleted")
        self.deleted.append((chat_id, message_id))
```

- [ ] **Step 4: Падающие тесты `tests/test_outbox_worker.py`**

```python
from sqlalchemy import select

from helpdesk.db.models import Outbox, TgCard, TgLink, TicketEvent
from helpdesk.delivery import outbox, worker
from tests.factories import make_client, make_staff, make_ticket
from tests.fakes import FakeTg, FakeWa


async def drain(sf, wa, tg) -> int:
    total = 0
    while n := await worker.process_batch(sf, wa, tg):
        total += n
    return total


async def test_sends_in_order_per_chat(sf, clock_):
    async with sf() as s, s.begin():
        outbox.wa_text(s, "1@c.us", "a")
        outbox.wa_text(s, "1@c.us", "b")
        outbox.wa_text(s, "2@c.us", "x")
        outbox.wa_text(s, "1@c.us", "c")
    wa, tg = FakeWa(), FakeTg()
    await drain(sf, wa, tg)
    assert [m[2] for m in wa.sent if m[1] == "1@c.us"] == ["a", "b", "c"]
    assert len(wa.sent) == 4


async def test_failed_head_blocks_rest_of_chat(sf, clock_):
    async with sf() as s, s.begin():
        outbox.wa_text(s, "1@c.us", "a")
        outbox.wa_text(s, "1@c.us", "b")
        outbox.wa_text(s, "2@c.us", "x")
    wa, tg = FakeWa(fail_chats={"1@c.us"}), FakeTg()
    await drain(sf, wa, tg)
    assert wa.sent == [("text", "2@c.us", "x")]
    wa.fail_chats.clear()
    await drain(sf, wa, tg)  # пауза ещё не прошла
    assert len(wa.sent) == 1
    clock_.advance(seconds=5)
    await drain(sf, wa, tg)
    assert [m[2] for m in wa.sent] == ["x", "a", "b"]


async def test_gives_up_after_retries_and_warns_assignee(sf, clock_):
    async with sf() as s, s.begin():
        c = await make_client(s)
        erlan = await make_staff(s, tg_id=1, name="Ерлан")
        t = await make_ticket(s, c, status="in_progress", assignee=erlan)
        outbox.wa_text(s, c.wa_chat_id, "Сейчас подойду", ticket_id=t.id)
        tid = t.id
    wa, tg = FakeWa(fail_chats={"77011234567@c.us"}), FakeTg()
    for delay in [0, *worker.RETRY_DELAYS]:
        clock_.advance(seconds=delay)
        await drain(sf, wa, tg)
    async with sf() as s:
        item = await s.scalar(select(Outbox).where(Outbox.channel == "wa"))
        assert item.status == "failed"
        assert item.attempts == len(worker.RETRY_DELAYS) + 1
        types = (await s.scalars(select(TicketEvent.type).where(TicketEvent.ticket_id == tid))).all()
        assert "delivery_failed" in types
    assert any(
        m["chat_id"] == 1 and m["text"].startswith(f"⚠️ Не доставлено в #{tid}") for m in tg.sent
    )


async def test_card_send_records_card_and_link(sf, clock_):
    async with sf() as s, s.begin():
        c = await make_client(s)
        st = await make_staff(s, tg_id=1, name="Ерлан")
        t = await make_ticket(s, c)
        outbox.tg_send(
            s,
            1,
            text="card",
            buttons=[[("✋ Взять", f"take:{t.id}")]],
            ticket_id=t.id,
            card_staff_id=st.id,
            card_seq=t.broadcast_seq,
        )
    tg = FakeTg()
    await drain(sf, FakeWa(), tg)
    assert tg.sent[0]["buttons"] == [[["✋ Взять", "take:1"]]]
    async with sf() as s:
        card = await s.scalar(select(TgCard))
        assert card.has_button and card.state == "open"
        assert card.tg_message_id == tg.sent[0]["mid"]
        link = await s.get(TgLink, (1, card.tg_message_id))
        assert link.ticket_id == 1


async def test_stale_card_is_skipped(sf, clock_):
    async with sf() as s, s.begin():
        c = await make_client(s)
        st = await make_staff(s, tg_id=1, name="Ерлан")
        t = await make_ticket(s, c)
        outbox.tg_send(s, 1, text="old", ticket_id=t.id, card_staff_id=st.id, card_seq=0)
        t.broadcast_seq = 1
    tg = FakeTg()
    await drain(sf, FakeWa(), tg)
    assert tg.sent == []
    async with sf() as s:
        assert (await s.scalar(select(Outbox))).status == "skipped"


async def test_card_for_taken_ticket_is_skipped(sf, clock_):
    async with sf() as s, s.begin():
        c = await make_client(s)
        st = await make_staff(s, tg_id=1, name="Ерлан")
        t = await make_ticket(s, c)
        outbox.tg_send(s, 1, text="card", ticket_id=t.id, card_staff_id=st.id, card_seq=0)
        t.status = "in_progress"
    tg = FakeTg()
    await drain(sf, FakeWa(), tg)
    assert tg.sent == []


async def test_delete_card_falls_back_to_edit(sf, clock_):
    async with sf() as s, s.begin():
        c = await make_client(s)
        st = await make_staff(s, tg_id=1, name="Ерлан")
        t = await make_ticket(s, c)
        ok = TgCard(ticket_id=t.id, staff_id=st.id, tg_chat_id=1, tg_message_id=54, has_button=True)
        old = TgCard(ticket_id=t.id, staff_id=st.id, tg_chat_id=1, tg_message_id=55, has_button=True)
        s.add_all([ok, old])
        await s.flush()
        outbox.tg_delete_card(s, ok, "🔒 #1 взял Тимур")
    tg = FakeTg()
    await drain(sf, FakeWa(), tg)
    assert tg.deleted == [(1, 54)]
    async with sf() as s, s.begin():
        old = await s.scalar(select(TgCard).where(TgCard.tg_message_id == 55))
        outbox.tg_delete_card(s, old, "🔒 #1 взял Тимур")
    tg.fail_delete = True
    await drain(sf, FakeWa(), tg)
    assert tg.edited == [(1, 55, "🔒 #1 взял Тимур", None)]
    async with sf() as s:
        states = dict((await s.execute(select(TgCard.tg_message_id, TgCard.state))).all())
        assert states == {54: "deleted", 55: "marked"}


async def test_tg_edit_is_sent(sf, clock_):
    async with sf() as s, s.begin():
        outbox.tg_edit(s, 1, 77, "новый текст", [[("✅ Закрыть", "close:1")]])
    tg = FakeTg()
    await drain(sf, FakeWa(), tg)
    assert tg.edited == [(1, 77, "новый текст", [["✅ Закрыть", "close:1"]])]

```

- [ ] **Step 5: Запустить, убедиться, что падает**

Run: `make test`
Expected: FAIL — `ImportError: cannot import name 'worker'`.

- [ ] **Step 6: `src/helpdesk/delivery/worker.py`**

```python
import asyncio
from datetime import timedelta
from typing import Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.orm import aliased

from helpdesk import clock
from helpdesk.db import queries
from helpdesk.db.models import Outbox, Staff, TgCard, TgLink, Ticket, TicketEvent
from helpdesk.delivery import outbox

log = structlog.get_logger()

RETRY_DELAYS = [5, 30, 120, 600, 1800]


class WaSender(Protocol):
    async def send_text(self, chat_id: str, text: str) -> str: ...

    async def send_file(
        self, chat_id: str, path: str, file_name: str, caption: str | None
    ) -> str: ...


class TgSender(Protocol):
    async def send(
        self,
        chat_id: int,
        *,
        text: str | None,
        file_path: str | None,
        file_kind: str | None,
        buttons: list | None,
    ) -> int: ...

    async def edit(self, chat_id: int, message_id: int, text: str, buttons: list | None) -> None: ...

    async def delete(self, chat_id: int, message_id: int) -> None: ...


async def process_batch(sf, wa: WaSender, tg: TgSender, limit: int = 20) -> int:
    prev = aliased(Outbox)
    blocked = (
        select(prev.id)
        .where(prev.chat_key == Outbox.chat_key, prev.status == "pending", prev.id < Outbox.id)
        .exists()
    )
    async with sf() as s:
        q = (
            select(Outbox.id)
            .where(Outbox.status == "pending", Outbox.next_attempt_at <= clock.utcnow(), ~blocked)
            .order_by(Outbox.id)
            .limit(limit)
        )
        ids = list((await s.scalars(q)).all())
    for item_id in ids:
        await _process_one(sf, wa, tg, item_id)
    return len(ids)


async def run(sf, wa: WaSender, tg: TgSender, stop: asyncio.Event, idle_sleep: float = 1.0):
    while not stop.is_set():
        try:
            n = await process_batch(sf, wa, tg)
        except Exception:
            log.exception("outbox batch failed")
            n = 0
        if n == 0:
            try:
                await asyncio.wait_for(stop.wait(), idle_sleep)
            except TimeoutError:
                pass


async def _process_one(sf, wa, tg, item_id: int) -> None:
    async with sf() as s, s.begin():
        item = await s.get(Outbox, item_id, with_for_update=True)
        if item is None or item.status != "pending":
            return
        try:
            async with s.begin_nested():
                await _deliver(s, item, wa, tg)
        except Exception as e:
            log.warning("outbox delivery failed", id=item_id, error=str(e))
            await s.refresh(item)  # откат savepoint мог сделать объект устаревшим
            item.attempts += 1
            item.last_error = str(e)[:1000]
            if item.attempts > len(RETRY_DELAYS):
                item.status = "failed"
                await _on_failed(s, item)
            else:
                delay = RETRY_DELAYS[item.attempts - 1]
                item.next_attempt_at = clock.utcnow() + timedelta(seconds=delay)
            return
        if item.status == "pending":
            item.status = "sent"


async def _deliver(s, item: Outbox, wa, tg) -> None:
    p = item.payload
    op = p["op"]
    if item.channel == "wa":
        if op == "text":
            await wa.send_text(p["chat_id"], p["text"])
        else:
            await wa.send_file(p["chat_id"], p["path"], p["file_name"], p.get("caption"))
        return
    if op == "send":
        if p.get("card_seq") is not None:
            t = await s.get(Ticket, p["ticket_id"], populate_existing=True)
            if t is None or t.status != "new" or t.broadcast_seq != p["card_seq"]:
                item.status = "skipped"
                return
        mid = await tg.send(
            p["chat_id"],
            text=p.get("text"),
            file_path=p.get("file_path"),
            file_kind=p.get("file_kind"),
            buttons=p.get("buttons"),
        )
        if p.get("ticket_id") is not None:
            await s.merge(TgLink(tg_chat_id=p["chat_id"], tg_message_id=mid, ticket_id=p["ticket_id"]))
        if p.get("card_staff_id") is not None:
            s.add(
                TgCard(
                    ticket_id=p["ticket_id"],
                    staff_id=p["card_staff_id"],
                    tg_chat_id=p["chat_id"],
                    tg_message_id=mid,
                    has_button=bool(p.get("buttons")),
                )
            )
    elif op == "edit":
        await tg.edit(p["chat_id"], p["message_id"], p["text"], p.get("buttons"))
    elif op == "delete_card":
        card = await s.get(TgCard, p["card_id"])
        if card is None or card.state != "deleting":
            item.status = "skipped"
            return
        try:
            await tg.delete(card.tg_chat_id, card.tg_message_id)
            card.state = "deleted"
        except Exception:
            try:
                await tg.edit(card.tg_chat_id, card.tg_message_id, p["fallback_text"], None)
            except Exception as e:  # карточка-файл без текста — оставляем как есть
                log.info("card edit fallback failed", card_id=card.id, error=str(e))
            card.state = "marked"


async def _on_failed(s, item: Outbox) -> None:
    p = item.payload
    if item.channel != "wa" or p.get("ticket_id") is None:
        return
    t = await s.get(Ticket, p["ticket_id"])
    if t is None:
        return
    s.add(TicketEvent(ticket_id=t.id, type="delivery_failed"))
    preview = (p.get("text") or p.get("file_name") or "")[:200]
    text = f"⚠️ Не доставлено в #{t.id}: {preview}"
    assignee = await s.get(Staff, t.assignee_id) if t.assignee_id else None
    recipients = [assignee] if assignee and assignee.status == "active" else []
    recipients = recipients or await queries.active_admins(s)
    for st in recipients:
        outbox.tg_send(s, st.tg_chat_id, text=text, ticket_id=t.id)
```

- [ ] **Step 7: Тесты зелёные**

Run: `make test`
Expected: все PASS.

- [ ] **Step 8: Commit**

```bash
uv run ruff check --fix src tests && uv run ruff format src tests
git add -A
git commit -m "feat(delivery): transactional outbox with ordered retrying worker

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 4: WhatsApp — разбор уведомлений, клиент Green API, хранение файлов

**Files:**
- Create: `src/helpdesk/whatsapp/__init__.py`, `src/helpdesk/whatsapp/parse.py`, `src/helpdesk/whatsapp/client.py`, `src/helpdesk/files.py`
- Test: `tests/test_whatsapp_parse.py`, `tests/test_whatsapp_client.py`, `tests/test_files.py`

**Interfaces:**
- Produces:
  - `IncomingMessage` (frozen dataclass): `wa_message_id: str, chat_id: str, phone: str, sender_name: str | None, kind: str, text: str | None, download_url: str | None = None, file_name: str | None = None`; `kind` ∈ `text|image|document|audio|video|other`.
  - `parse_notification(body: dict) -> IncomingMessage | None`: `None` для всего, кроме `incomingMessageReceived` из личного чата (`@c.us`), а также для реакций, правок и удалений.
  - `GreenApiClient(api_url, media_url, id_instance, token, http: httpx.AsyncClient | None = None)` с методами `receive(timeout=20) -> tuple[int, dict] | None`, `delete(receipt_id) -> None`, `send_text(chat_id, text) -> str`, `send_file(chat_id, path, file_name, caption) -> str`, `download(url, dest: Path) -> None`, `aclose()`. Реализует `worker.WaSender`.
  - `files.new_file_path(files_dir: str, file_name: str | None, now: datetime) -> Path` — `<files_dir>/<YYYY>/<MM>/<uuid><ext>`, каталог создаётся.

- [ ] **Step 1: Падающие тесты `tests/test_whatsapp_parse.py`**

```python
from helpdesk.whatsapp.parse import IncomingMessage, parse_notification


def _body(message_data, chat_id="77011234567@c.us", type_webhook="incomingMessageReceived"):
    return {
        "typeWebhook": type_webhook,
        "instanceData": {"idInstance": 1101, "wid": "77000000000@c.us"},
        "timestamp": 1791364800,
        "idMessage": "BAE5F4886F6F2D05",
        "senderData": {"chatId": chat_id, "sender": chat_id, "senderName": "Aigerim"},
        "messageData": message_data,
    }


def test_text_message():
    m = parse_notification(
        _body({"typeMessage": "textMessage", "textMessageData": {"textMessage": "Не печатает"}})
    )
    assert m == IncomingMessage(
        wa_message_id="BAE5F4886F6F2D05",
        chat_id="77011234567@c.us",
        phone="77011234567",
        sender_name="Aigerim",
        kind="text",
        text="Не печатает",
    )


def test_extended_and_quoted_text():
    for tm in ("extendedTextMessage", "quotedMessage"):
        m = parse_notification(
            _body({"typeMessage": tm, "extendedTextMessageData": {"text": "см. выше"}})
        )
        assert m.kind == "text" and m.text == "см. выше"


def test_file_messages():
    cases = {
        "imageMessage": "image",
        "documentMessage": "document",
        "audioMessage": "audio",
        "videoMessage": "video",
    }
    for tm, kind in cases.items():
        m = parse_notification(
            _body(
                {
                    "typeMessage": tm,
                    "fileMessageData": {
                        "downloadUrl": "https://sw-media-out.example/f.bin",
                        "caption": "ошибка",
                        "fileName": "f.bin",
                        "mimeType": "application/octet-stream",
                    },
                }
            )
        )
        assert (m.kind, m.text, m.download_url, m.file_name) == (
            kind,
            "ошибка",
            "https://sw-media-out.example/f.bin",
            "f.bin",
        )


def test_empty_caption_is_none():
    m = parse_notification(
        _body({"typeMessage": "imageMessage", "fileMessageData": {"downloadUrl": "u", "caption": ""}})
    )
    assert m.text is None


def test_unsupported_types_become_other():
    m = parse_notification(_body({"typeMessage": "stickerMessage"}))
    assert (m.kind, m.text) == ("other", "[стикер]")
    m = parse_notification(_body({"typeMessage": "somethingNew"}))
    assert (m.kind, m.text) == ("other", "[somethingNew]")


def test_ignored_notifications():
    assert parse_notification(_body({"typeMessage": "reactionMessage"})) is None
    assert parse_notification(_body({"typeMessage": "editedMessage"})) is None
    assert parse_notification(_body({"typeMessage": "textMessage"}, chat_id="1203@g.us")) is None
    assert parse_notification(_body({}, type_webhook="outgoingMessageReceived")) is None
    assert parse_notification({"typeWebhook": "stateInstanceChanged"}) is None
```

- [ ] **Step 2: Падающие тесты `tests/test_whatsapp_client.py`**

```python
import json

import httpx
import pytest

from helpdesk.whatsapp.client import GreenApiClient


def _client(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GreenApiClient("https://api.test", "https://media.test", "1101", "tok", http=http)


async def test_receive_returns_none_when_queue_empty():
    c = _client(lambda req: httpx.Response(200, text="null"))
    assert await c.receive() is None


async def test_receive_and_delete_urls():
    seen = []

    def h(req):
        seen.append((req.method, req.url.path, dict(req.url.params)))
        if req.method == "GET":
            return httpx.Response(200, json={"receiptId": 7, "body": {"typeWebhook": "x"}})
        return httpx.Response(200, json={"result": True})

    c = _client(h)
    assert await c.receive(timeout=5) == (7, {"typeWebhook": "x"})
    await c.delete(7)
    assert seen == [
        ("GET", "/waInstance1101/receiveNotification/tok", {"receiveTimeout": "5"}),
        ("DELETE", "/waInstance1101/deleteNotification/tok/7", {}),
    ]


async def test_send_text_posts_json():
    def h(req):
        assert req.url.path == "/waInstance1101/sendMessage/tok"
        assert json.loads(req.content) == {"chatId": "1@c.us", "message": "привет"}
        return httpx.Response(200, json={"idMessage": "ABC"})

    assert await _client(h).send_text("1@c.us", "привет") == "ABC"


async def test_send_file_uses_media_host(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF")

    def h(req):
        assert req.url.host == "media.test"
        assert req.url.path == "/waInstance1101/sendFileByUpload/tok"
        assert b'name="chatId"' in req.content and b"%PDF" in req.content
        assert b'name="caption"' in req.content
        return httpx.Response(200, json={"idMessage": "F1"})

    assert await _client(h).send_file("1@c.us", str(f), "a.pdf", "акт") == "F1"


async def test_download_writes_file(tmp_path):
    c = _client(lambda req: httpx.Response(200, content=b"imagebytes"))
    dest = tmp_path / "x.jpg"
    await c.download("https://cdn.test/x", dest)
    assert dest.read_bytes() == b"imagebytes"


async def test_http_error_raises():
    c = _client(lambda req: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await c.send_text("1@c.us", "x")
```

- [ ] **Step 3: Падающие тесты `tests/test_files.py`**

```python
from datetime import UTC, datetime

from helpdesk.files import new_file_path


def test_new_file_path_layout(tmp_path):
    p = new_file_path(str(tmp_path), "Отчёт.PDF", datetime(2026, 10, 7, tzinfo=UTC))
    assert p.parent == tmp_path / "2026" / "10"
    assert p.parent.is_dir()
    assert p.suffix == ".pdf"
    assert not p.exists()


def test_new_file_path_without_or_weird_name(tmp_path):
    now = datetime(2026, 1, 2, tzinfo=UTC)
    assert new_file_path(str(tmp_path), None, now).suffix == ""
    assert new_file_path(str(tmp_path), "x.verylongextension", now).suffix == ""
    a = new_file_path(str(tmp_path), "a.jpg", now)
    b = new_file_path(str(tmp_path), "a.jpg", now)
    assert a != b
```

- [ ] **Step 4: Запустить, убедиться, что падает**

Run: `uv run pytest tests/test_whatsapp_parse.py tests/test_whatsapp_client.py tests/test_files.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 5: Реализация**

`src/helpdesk/whatsapp/__init__.py` — пустой.

`src/helpdesk/whatsapp/parse.py`:
```python
from dataclasses import dataclass


@dataclass(frozen=True)
class IncomingMessage:
    wa_message_id: str
    chat_id: str
    phone: str
    sender_name: str | None
    kind: str
    text: str | None
    download_url: str | None = None
    file_name: str | None = None


_FILE_KINDS = {
    "imageMessage": "image",
    "documentMessage": "document",
    "audioMessage": "audio",
    "videoMessage": "video",
}
_OTHER_LABELS = {
    "stickerMessage": "[стикер]",
    "locationMessage": "[геолокация]",
    "contactMessage": "[контакт]",
    "contactsArrayMessage": "[контакты]",
    "pollMessage": "[опрос]",
}
_IGNORED = {"reactionMessage", "editedMessage", "deletedMessage"}


def parse_notification(body: dict) -> IncomingMessage | None:
    if body.get("typeWebhook") != "incomingMessageReceived":
        return None
    sender = body.get("senderData") or {}
    chat_id = sender.get("chatId", "")
    if not chat_id.endswith("@c.us"):
        return None
    md = body.get("messageData") or {}
    tm = md.get("typeMessage")
    if tm in _IGNORED:
        return None
    base = dict(
        wa_message_id=body["idMessage"],
        chat_id=chat_id,
        phone=chat_id.split("@")[0],
        sender_name=sender.get("senderName") or None,
    )
    if tm == "textMessage":
        text = (md.get("textMessageData") or {}).get("textMessage", "")
        return IncomingMessage(**base, kind="text", text=text)
    if tm in ("extendedTextMessage", "quotedMessage"):
        text = (md.get("extendedTextMessageData") or {}).get("text", "")
        return IncomingMessage(**base, kind="text", text=text)
    if tm in _FILE_KINDS:
        f = md.get("fileMessageData") or {}
        return IncomingMessage(
            **base,
            kind=_FILE_KINDS[tm],
            text=f.get("caption") or None,
            download_url=f.get("downloadUrl"),
            file_name=f.get("fileName"),
        )
    return IncomingMessage(**base, kind="other", text=_OTHER_LABELS.get(tm, f"[{tm}]"))
```

`src/helpdesk/whatsapp/client.py`:
```python
from pathlib import Path

import httpx


class GreenApiClient:
    def __init__(
        self,
        api_url: str,
        media_url: str,
        id_instance: str,
        token: str,
        http: httpx.AsyncClient | None = None,
    ):
        self._base = f"{api_url.rstrip('/')}/waInstance{id_instance}"
        self._media = f"{media_url.rstrip('/')}/waInstance{id_instance}"
        self._token = token
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(60))

    async def receive(self, timeout: int = 20) -> tuple[int, dict] | None:
        r = await self._http.get(
            f"{self._base}/receiveNotification/{self._token}",
            params={"receiveTimeout": timeout},
            timeout=timeout + 10,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        return data["receiptId"], data["body"]

    async def delete(self, receipt_id: int) -> None:
        r = await self._http.delete(f"{self._base}/deleteNotification/{self._token}/{receipt_id}")
        r.raise_for_status()

    async def send_text(self, chat_id: str, text: str) -> str:
        r = await self._http.post(
            f"{self._base}/sendMessage/{self._token}", json={"chatId": chat_id, "message": text}
        )
        r.raise_for_status()
        return r.json()["idMessage"]

    async def send_file(self, chat_id: str, path: str, file_name: str, caption: str | None) -> str:
        content = Path(path).read_bytes()
        data = {"chatId": chat_id, "fileName": file_name}
        if caption:
            data["caption"] = caption
        r = await self._http.post(
            f"{self._media}/sendFileByUpload/{self._token}",
            data=data,
            files={"file": (file_name, content)},
        )
        r.raise_for_status()
        return r.json()["idMessage"]

    async def download(self, url: str, dest: Path) -> None:
        async with self._http.stream("GET", url) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)

    async def aclose(self) -> None:
        await self._http.aclose()
```

`src/helpdesk/files.py`:
```python
import uuid
from datetime import datetime
from pathlib import Path


def new_file_path(files_dir: str, file_name: str | None, now: datetime) -> Path:
    suffix = Path(file_name).suffix.lower() if file_name else ""
    if len(suffix) > 10:
        suffix = ""
    d = Path(files_dir) / f"{now:%Y}" / f"{now:%m}"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{uuid.uuid4().hex}{suffix}"
```

- [ ] **Step 6: Тесты зелёные**

Run: `make test`
Expected: все PASS.

- [ ] **Step 7: Commit**

```bash
uv run ruff check --fix src tests && uv run ruff format src tests
git add -A
git commit -m "feat(whatsapp): Green API client, notification parser, file paths

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Заявки — создание, рассылка карточек, тексты

**Files:**
- Create: `src/helpdesk/texts.py`, `src/helpdesk/tickets/__init__.py`, `src/helpdesk/tickets/render.py`, `src/helpdesk/tickets/service.py`
- Test: `tests/test_tickets_queue.py`, `tests/test_render.py`

**Interfaces:**
- Consumes: `outbox.*`, `queries.on_shift_staff/active_admins/staff_names`, модели, `clock`.
- Produces:
  - `texts.CHOOSE_LANG: str`, `texts.t(lang: str, key: str, **kw) -> str`; ключи: `ask_name, bad_name, ask_location, bad_location, accepted(id), taken(id, name), closed(id), unsupported`.
  - `render.clip(text, limit=3500) -> str` (оставляет хвост, спереди «…»), `render.client_line(c) -> str`, `render.message_line(m) -> str`, `render.queue_card(t, c, msgs, waited_min=None) -> str`, `render.history(msgs, names) -> str`, `render.working_card(t, c, msgs, names) -> str`.
  - `tickets.QUEUE_BUTTONS(tid)`, `tickets.WORK_BUTTONS(tid)`, `tickets.TG_FILE_KIND: dict[str, str]`.
  - `await tickets.open_ticket(s, client_id) -> Ticket | None`
  - `await tickets.ticket_messages(s, ticket_id) -> list[Message]`
  - `await tickets.on_client_message(s, client, message: Message | None) -> Ticket`
  - `await tickets.broadcast(s, t, client=None, *, waited_min=None, warn_if_empty=True) -> int` (сколько специалистов получили карточки)
  - внутренние `_send_cards(s, t, client, msgs, staff, waited_min=None)`, `_delete_open_cards(s, t, fallback, keep_staff_id=None)`.

- [ ] **Step 1: Падающие тесты `tests/test_render.py`**

```python
from datetime import UTC, datetime

from helpdesk.db.models import Client, Message, Ticket
from helpdesk.tickets import render

T0 = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)  # 14:00 по Алматы


def _client():
    return Client(
        phone="77011234567", full_name="Айгерим Сериккызы", location="Бухгалтерия, каб. 214"
    )


def test_queue_card_layout():
    t = Ticket(id=125, created_at=T0)
    msgs = [
        Message(direction="in", kind="text", text="Не печатает", created_at=T0),
        Message(direction="in", kind="image", text=None, file_path="/x.jpg", created_at=T0),
    ]
    text = render.queue_card(t, _client(), msgs)
    assert text.splitlines()[0] == "🆕 Заявка #125 · 14:00"
    assert "👤 Айгерим Сериккызы · Бухгалтерия, каб. 214" in text
    assert "📞 +77011234567" in text
    assert "«Не печатает»" in text and "«[🖼 фото]»" in text


def test_queue_card_reminder_header():
    t = Ticket(id=7, created_at=T0)
    assert render.queue_card(t, _client(), [], waited_min=20).startswith("⏰ Заявка #7 ждёт 20 мин")


def test_queue_card_clips_long_text():
    t = Ticket(id=1, created_at=T0)
    msgs = [Message(direction="in", kind="text", text="x" * 5000 + "КОНЕЦ", created_at=T0)]
    text = render.queue_card(t, _client(), msgs)
    assert len(text) <= 4000
    assert text.startswith("🆕 Заявка #1") and "КОНЕЦ" in text


def test_history_marks_directions_and_names():
    msgs = [
        Message(direction="in", kind="text", text="Помогите", created_at=T0),
        Message(direction="out", staff_id=3, kind="text", text="Иду", created_at=T0),
    ]
    h = render.history(msgs, {3: "Ерлан"})
    assert h.splitlines() == ["07.10 14:00 👤: Помогите", "07.10 14:00 🛠 Ерлан: Иду"]


def test_client_line_falls_back_to_whatsapp_name():
    c = Client(phone="7701", wa_name="Aigerim", full_name=None, location=None)
    assert render.client_line(c) == "👤 Aigerim · —\n📞 +7701"
```

- [ ] **Step 2: Падающие тесты `tests/test_tickets_queue.py`**

```python
from sqlalchemy import select

from helpdesk.db.models import Message, TgCard, TicketEvent
from helpdesk.tickets import service as tickets
from tests.factories import clear_outbox, make_client, make_staff, make_ticket, payloads


async def _in(s, client, text="Не печатает принтер", **kw) -> Message:
    m = Message(
        client_id=client.id,
        direction="in",
        kind=kw.get("kind", "text"),
        text=text,
        file_path=kw.get("file_path"),
        file_name=kw.get("file_name"),
    )
    s.add(m)
    await s.flush()
    return m


async def test_first_message_creates_ticket_and_broadcasts(s, clock_):
    c = await make_client(s)
    await make_staff(s, tg_id=1, name="Ерлан")
    await make_staff(s, tg_id=2, name="Тимур")
    await make_staff(s, tg_id=3, name="Алибек", on_shift=False)
    await make_staff(s, tg_id=4, name="Новичок", status="pending")
    m = await _in(s, c)
    t = await tickets.on_client_message(s, c, m)
    await s.refresh(m)
    assert t.status == "new" and m.ticket_id == t.id
    assert t.broadcast_seq == 1 and t.last_broadcast_at == clock_.now
    assert await payloads(s, "wa") == [
        {
            "op": "text",
            "chat_id": c.wa_chat_id,
            "text": f"Заявка #{t.id} принята. Специалист скоро ответит.",
            "ticket_id": t.id,
        }
    ]
    cards = await payloads(s, "tg")
    assert sorted(p["chat_id"] for p in cards) == [1, 2]
    assert all(p["buttons"] == [[["✋ Взять", f"take:{t.id}"]]] for p in cards)
    assert all(p["card_seq"] == 1 and p["ticket_id"] == t.id for p in cards)
    assert "Айгерим Сериккызы" in cards[0]["text"] and "Не печатает принтер" in cards[0]["text"]
    types = (await s.scalars(select(TicketEvent.type))).all()
    assert types == ["created"]


async def test_pending_messages_without_ticket_are_attached(s, clock_):
    c = await make_client(s)
    await make_staff(s, tg_id=1, name="Ерлан")
    first = await _in(s, c, "Привет")
    s.add(Message(client_id=c.id, direction="in", kind="text", text="1", intake=True))
    await s.flush()
    t = await tickets.on_client_message(s, c, None)
    texts_ = [m.text for m in await tickets.ticket_messages(s, t.id)]
    assert texts_ == ["Привет"]
    await s.refresh(first)
    assert first.ticket_id == t.id


async def test_attachment_cards_come_before_button_card(s, clock_):
    c = await make_client(s)
    st = await make_staff(s, tg_id=1, name="Ерлан")
    m = await _in(s, c, "вот ошибка", kind="image", file_path="/data/files/x.jpg", file_name="x.jpg")
    await tickets.on_client_message(s, c, m)
    cards = await payloads(s, "tg")
    assert [p["file_kind"] for p in cards] == ["photo", None]
    assert cards[0]["file_path"] == "/data/files/x.jpg" and cards[0]["text"] == "вот ошибка"
    assert cards[0]["card_staff_id"] == st.id and cards[0]["buttons"] is None


async def test_next_message_while_queued_rebroadcasts(s, clock_):
    c = await make_client(s)
    st = await make_staff(s, tg_id=1, name="Ерлан")
    t = await tickets.on_client_message(s, c, await _in(s, c))
    s.add(TgCard(ticket_id=t.id, staff_id=st.id, tg_chat_id=1, tg_message_id=500, has_button=True))
    await s.flush()
    await clear_outbox(s)
    t2 = await tickets.on_client_message(s, c, await _in(s, c, "И сканер тоже"))
    assert t2.id == t.id and t.broadcast_seq == 2
    p = await payloads(s, "tg")
    assert p[0]["op"] == "delete_card"
    assert [x["op"] for x in p[1:]] == ["send"]
    assert p[-1]["card_seq"] == 2
    assert "Не печатает принтер" in p[-1]["text"] and "И сканер тоже" in p[-1]["text"]
    assert await payloads(s, "wa") == []


async def test_message_in_progress_goes_to_assignee_only(s, clock_):
    c = await make_client(s)
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    await make_staff(s, tg_id=2, name="Тимур")
    t = await make_ticket(s, c, status="in_progress", assignee=erlan)
    await tickets.on_client_message(s, c, await _in(s, c, "Жду"))
    p = await payloads(s, "tg")
    assert len(p) == 1
    assert p[0]["chat_id"] == 1 and p[0]["ticket_id"] == t.id
    assert p[0]["text"] == f"#{t.id} · Айгерим Сериккызы\nЖду"
    assert p[0]["card_staff_id"] is None


async def test_file_in_progress_forwarded_as_file(s, clock_):
    c = await make_client(s)
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    t = await make_ticket(s, c, status="in_progress", assignee=erlan)
    m = await _in(s, c, None, kind="audio", file_path="/f/v.ogg", file_name="v.ogg")
    await tickets.on_client_message(s, c, m)
    p = (await payloads(s, "tg"))[0]
    assert (p["file_kind"], p["file_path"], p["text"]) == ("voice", "/f/v.ogg", f"#{t.id} · Айгерим Сериккызы")


async def test_nobody_on_shift_warns_admins(s, clock_):
    c = await make_client(s)
    await make_staff(s, tg_id=9, name="Админ", is_admin=True, on_shift=False)
    t = await tickets.on_client_message(s, c, await _in(s, c))
    p = await payloads(s, "tg")
    assert len(p) == 1 and p[0]["chat_id"] == 9
    assert p[0]["text"] == f"⚠️ Заявка #{t.id}: на смене никого нет"
```

- [ ] **Step 3: Запустить, убедиться, что падает**

Run: `uv run pytest tests/test_render.py tests/test_tickets_queue.py -q` (с `TEST_DATABASE_URL`, проще `make test`)
Expected: FAIL — `ModuleNotFoundError: helpdesk.tickets`.

- [ ] **Step 4: `src/helpdesk/texts.py`**

```python
CHOOSE_LANG = "Тілді таңдаңыз / Выберите язык:\n1 — Русский\n2 — Қазақша"

TEXTS = {
    "ru": {
        "ask_name": "Здравствуйте! Это IT-поддержка. Укажите, пожалуйста, ваше ФИО.",
        "bad_name": "Пожалуйста, напишите ФИО текстом (от 2 до 100 символов).",
        "ask_location": "Укажите отдел и номер кабинета.",
        "bad_location": "Пожалуйста, напишите отдел и кабинет текстом (до 200 символов).",
        "accepted": "Заявка #{id} принята. Специалист скоро ответит.",
        "taken": "Вашей заявкой #{id} занимается {name}.",
        "closed": "Заявка #{id} выполнена. Если проблема осталась — напишите снова.",
        "unsupported": "Принимаются текст, фото, файлы и голосовые сообщения.",
    },
    "kk": {
        "ask_name": "Сәлеметсіз бе! Бұл IT-қолдау қызметі. Аты-жөніңізді жазыңыз.",
        "bad_name": "Аты-жөніңізді мәтінмен жазыңыз (2-ден 100 таңбаға дейін).",
        "ask_location": "Бөліміңізді және кабинет нөмірін жазыңыз.",
        "bad_location": "Бөлім мен кабинетті мәтінмен жазыңыз (200 таңбаға дейін).",
        "accepted": "#{id} өтінім қабылданды. Маман жақын арада жауап береді.",
        "taken": "#{id} өтініміңізбен {name} айналысады.",
        "closed": "#{id} өтінім орындалды. Мәселе шешілмесе, қайта жазыңыз.",
        "unsupported": "Мәтін, фото, файл және дауыстық хабарламалар қабылданады.",
    },
}


def t(lang: str, key: str, **kw) -> str:
    return TEXTS.get(lang, TEXTS["ru"])[key].format(**kw)
```

- [ ] **Step 5: `src/helpdesk/tickets/render.py`** (и пустой `tickets/__init__.py`)

```python
from helpdesk import clock
from helpdesk.db.models import Client, Message, Ticket

KIND_LABEL = {
    "image": "🖼 фото",
    "document": "📎 файл",
    "audio": "🎤 голосовое",
    "video": "🎬 видео",
}


def clip(text: str, limit: int = 3500) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


def client_line(c: Client) -> str:
    name = c.full_name or c.wa_name or "без имени"
    return f"👤 {name} · {c.location or '—'}\n📞 +{c.phone}"


def message_line(m: Message) -> str:
    parts = []
    if m.kind in KIND_LABEL:
        parts.append(f"[{KIND_LABEL[m.kind]}]")
    if m.text:
        parts.append(m.text)
    return " ".join(parts) or "[пусто]"


def queue_card(t: Ticket, c: Client, msgs: list[Message], waited_min: int | None = None) -> str:
    if waited_min:
        head = f"⏰ Заявка #{t.id} ждёт {waited_min} мин"
    else:
        head = f"🆕 Заявка #{t.id} · {clock.local(t.created_at):%H:%M}"
    body = "\n".join(f"«{message_line(m)}»" for m in msgs if m.direction == "in")
    return f"{head}\n{client_line(c)}\n\n{clip(body)}"


def history(msgs: list[Message], names: dict[int, str]) -> str:
    lines = []
    for m in msgs:
        who = "👤" if m.direction == "in" else f"🛠 {names.get(m.staff_id, '?')}"
        lines.append(f"{clock.local(m.created_at):%d.%m %H:%M} {who}: {message_line(m)}")
    return "\n".join(lines)


def working_card(t: Ticket, c: Client, msgs: list[Message], names: dict[int, str]) -> str:
    return f"✅ Заявка #{t.id} у вас в работе\n{client_line(c)}\n\n{clip(history(msgs, names))}"
```

- [ ] **Step 6: `src/helpdesk/tickets/service.py`** (часть 1 — очередь)

```python
from sqlalchemy import select, update

from helpdesk import clock, texts
from helpdesk.db import queries
from helpdesk.db.models import Client, Message, Staff, TgCard, Ticket, TicketEvent
from helpdesk.delivery import outbox
from helpdesk.tickets import render

TG_FILE_KIND = {"image": "photo", "document": "document", "audio": "voice", "video": "video"}


def QUEUE_BUTTONS(tid: int):
    return [[("✋ Взять", f"take:{tid}")]]


def WORK_BUTTONS(tid: int):
    return [[("✅ Закрыть", f"close:{tid}"), ("↩️ Вернуть", f"ret:{tid}")]]


async def open_ticket(s, client_id: int) -> Ticket | None:
    q = select(Ticket).where(Ticket.client_id == client_id, Ticket.status != "closed")
    return await s.scalar(q)


async def ticket_messages(s, ticket_id: int) -> list[Message]:
    q = select(Message).where(Message.ticket_id == ticket_id).order_by(Message.id)
    return list((await s.scalars(q)).all())


async def on_client_message(s, client: Client, message: Message | None) -> Ticket:
    t = await open_ticket(s, client.id)
    if t is None:
        t = Ticket(client_id=client.id)
        s.add(t)
        await s.flush()
        await s.execute(
            update(Message)
            .where(
                Message.client_id == client.id,
                Message.ticket_id.is_(None),
                Message.intake.is_(False),
            )
            .values(ticket_id=t.id)
            .execution_options(synchronize_session=False)
        )
        if message is not None:
            message.ticket_id = t.id
        s.add(TicketEvent(ticket_id=t.id, type="created"))
        outbox.wa_text(s, client.wa_chat_id, texts.t(client.lang, "accepted", id=t.id), ticket_id=t.id)
        await broadcast(s, t, client)
        return t
    if message is not None:
        message.ticket_id = t.id
        await s.flush()
    if t.status == "in_progress" and message is not None:
        staff = await s.get(Staff, t.assignee_id)
        _forward(s, staff, t, client, message)
    elif t.status == "new":
        await broadcast(s, t, client)
    return t


def _header(t: Ticket, c: Client) -> str:
    return f"#{t.id} · {c.full_name or c.wa_name or '+' + c.phone}"


def _forward(s, staff: Staff, t: Ticket, c: Client, m: Message) -> None:
    if m.file_path:
        text = f"{_header(t, c)}\n{m.text}" if m.text else _header(t, c)
        outbox.tg_send(
            s,
            staff.tg_chat_id,
            text=text,
            file_path=m.file_path,
            file_kind=TG_FILE_KIND[m.kind],
            ticket_id=t.id,
        )
    else:
        text = f"{_header(t, c)}\n{render.message_line(m)}"
        outbox.tg_send(s, staff.tg_chat_id, text=text, ticket_id=t.id)


async def broadcast(
    s, t: Ticket, client: Client | None = None, *, waited_min=None, warn_if_empty=True
) -> int:
    client = client or await s.get(Client, t.client_id)
    t.broadcast_seq += 1
    t.last_broadcast_at = clock.utcnow()
    await _delete_open_cards(s, t, f"#{t.id} — карточка обновлена")
    recipients = await queries.on_shift_staff(s)
    if not recipients:
        if warn_if_empty:
            for a in await queries.active_admins(s):
                text = f"⚠️ Заявка #{t.id}: на смене никого нет"
                outbox.tg_send(s, a.tg_chat_id, text=text, ticket_id=t.id)
        return 0
    msgs = await ticket_messages(s, t.id)
    for st in recipients:
        _send_cards(s, t, client, msgs, st, waited_min=waited_min)
    return len(recipients)


def _send_cards(s, t, client, msgs, st: Staff, waited_min=None) -> None:
    for m in msgs:
        if m.direction == "in" and m.file_path:
            outbox.tg_send(
                s,
                st.tg_chat_id,
                text=m.text,
                file_path=m.file_path,
                file_kind=TG_FILE_KIND[m.kind],
                ticket_id=t.id,
                card_staff_id=st.id,
                card_seq=t.broadcast_seq,
            )
    outbox.tg_send(
        s,
        st.tg_chat_id,
        text=render.queue_card(t, client, msgs, waited_min),
        buttons=QUEUE_BUTTONS(t.id),
        ticket_id=t.id,
        card_staff_id=st.id,
        card_seq=t.broadcast_seq,
    )


async def _delete_open_cards(s, t: Ticket, fallback: str, keep_staff_id: int | None = None):
    q = select(TgCard).where(TgCard.ticket_id == t.id, TgCard.state == "open")
    for card in (await s.scalars(q)).all():
        if card.staff_id != keep_staff_id:
            outbox.tg_delete_card(s, card, fallback)
```

- [ ] **Step 7: Тесты зелёные**

Run: `make test`
Expected: все PASS.

- [ ] **Step 8: Commit**

```bash
uv run ruff check --fix src tests && uv run ruff format src tests
git add -A
git commit -m "feat(tickets): ticket creation, queue cards broadcast, client texts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Заявки — взять, вернуть, закрыть, маршрутизация ответов

**Files:**
- Modify: `src/helpdesk/tickets/service.py` (дописать в конец)
- Test: `tests/test_tickets_work.py`

**Interfaces:**
- Consumes: всё из задачи 5.
- Produces:
  - `TakeResult(ok: bool, holder_name: str | None)` (dataclass)
  - `await tickets.take(s, ticket_id, staff) -> TakeResult`
  - `await tickets.return_ticket(s, ticket_id, staff) -> bool`
  - `await tickets.close_ticket(s, ticket_id, staff) -> bool`
  - `await tickets.resolve_target(s, staff, reply_to_message_id: int | None) -> tuple[Ticket | None, str]` (при `None` вторым элементом идёт текст отказа)
  - `await tickets.send_staff_reply(s, staff, t, kind, text, file_path, file_name) -> Message`
  - `await tickets.set_active(s, staff, ticket_id) -> Ticket | None`
  - `await tickets.my_tickets(s, staff) -> list[tuple[Ticket, Client]]`
  - `await tickets.recent_view(s, t, limit=5) -> str`
  - `await tickets.link(s, chat_id, message_id, ticket_id) -> None`

- [ ] **Step 1: Падающие тесты `tests/test_tickets_work.py`**

```python
import asyncio

from sqlalchemy import func, select

from helpdesk.db.models import Message, Staff, TgCard, TgLink, TicketEvent
from helpdesk.tickets import service as tickets
from tests.factories import clear_outbox, make_client, make_staff, make_ticket, payloads


async def _queued(s):
    c = await make_client(s)
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    tima = await make_staff(s, tg_id=2, name="Тимур")
    t = await make_ticket(s, c)
    for st, mid in ((erlan, 101), (tima, 201)):
        s.add(
            TgCard(
                ticket_id=t.id,
                staff_id=st.id,
                tg_chat_id=st.tg_chat_id,
                tg_message_id=mid,
                has_button=True,
            )
        )
    await s.flush()
    await clear_outbox(s)
    return c, erlan, tima, t


async def test_take_assigns_and_cleans_other_cards(s, clock_):
    c, erlan, tima, t = await _queued(s)
    r = await tickets.take(s, t.id, erlan)
    assert r == tickets.TakeResult(True, "Ерлан")
    await s.refresh(t)
    assert (t.status, t.assignee_id, t.first_taken_at) == ("in_progress", erlan.id, clock_.now)
    assert erlan.active_ticket_id == t.id
    states = dict((await s.execute(select(TgCard.tg_message_id, TgCard.state))).all())
    assert states == {101: "kept", 201: "deleting"}
    tg = await payloads(s, "tg")
    deletes = [p for p in tg if p["op"] == "delete_card"]
    edits = [p for p in tg if p["op"] == "edit"]
    assert [d["fallback_text"] for d in deletes] == [f"🔒 #{t.id} взял Ерлан"]
    assert (edits[0]["chat_id"], edits[0]["message_id"]) == (1, 101)
    assert edits[0]["buttons"] == [[["✅ Закрыть", f"close:{t.id}"], ["↩️ Вернуть", f"ret:{t.id}"]]]
    assert edits[0]["text"].startswith(f"✅ Заявка #{t.id} у вас в работе")
    wa = await payloads(s, "wa")
    assert wa[0]["text"] == f"Вашей заявкой #{t.id} занимается Ерлан."
    types = (await s.scalars(select(TicketEvent.type))).all()
    assert types == ["taken"]


async def test_concurrent_take_has_one_winner(sf, clock_):
    async with sf() as s, s.begin():
        c, erlan, tima, t = await _queued(s)
        ids = (erlan.id, tima.id, t.id)

    async def attempt(staff_id):
        async with sf() as s, s.begin():
            st = await s.get(Staff, staff_id)
            return await tickets.take(s, ids[2], st)

    results = await asyncio.gather(attempt(ids[0]), attempt(ids[1]))
    assert sorted(r.ok for r in results) == [False, True]
    winner = next(r for r in results if r.ok)
    loser = next(r for r in results if not r.ok)
    assert loser.holder_name == winner.holder_name
    async with sf() as s:
        q = select(func.count()).select_from(TicketEvent).where(TicketEvent.type == "taken")
        assert await s.scalar(q) == 1


async def test_late_card_of_loser_is_removed(s, clock_):
    c, erlan, tima, t = await _queued(s)
    await tickets.take(s, t.id, erlan)
    s.add(TgCard(ticket_id=t.id, staff_id=tima.id, tg_chat_id=2, tg_message_id=202, has_button=True))
    await s.flush()
    await clear_outbox(s)
    r = await tickets.take(s, t.id, tima)
    assert r == tickets.TakeResult(False, "Ерлан")
    p = await payloads(s, "tg")
    assert [x["op"] for x in p] == ["delete_card"]


async def test_take_without_card_sends_working_card(s, clock_):
    c = await make_client(s)
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    t = await make_ticket(s, c)
    await tickets.take(s, t.id, erlan)
    sends = [p for p in await payloads(s, "tg") if p["op"] == "send"]
    assert len(sends) == 1 and sends[0]["chat_id"] == 1
    assert sends[0]["buttons"][0][0] == ["✅ Закрыть", f"close:{t.id}"]
    assert sends[0]["ticket_id"] == t.id


async def test_return_requeues_and_clears_active(s, clock_):
    c, erlan, tima, t = await _queued(s)
    await tickets.take(s, t.id, erlan)
    await clear_outbox(s)
    assert not await tickets.return_ticket(s, t.id, tima)
    assert await tickets.return_ticket(s, t.id, erlan)
    await s.refresh(t)
    assert (t.status, t.assignee_id, erlan.active_ticket_id) == ("new", None, None)
    assert t.broadcast_seq == 1
    p = await payloads(s, "tg")
    assert sorted(x["chat_id"] for x in p if x["op"] == "send") == [1, 2]
    assert not any(x["op"] == "delete_card" for x in p)
    types = (await s.scalars(select(TicketEvent.type).order_by(TicketEvent.id))).all()
    assert types == ["taken", "returned"]


async def test_admin_can_return_foreign_ticket(s, clock_):
    c, erlan, tima, t = await _queued(s)
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    await tickets.take(s, t.id, erlan)
    assert await tickets.return_ticket(s, t.id, admin)


async def test_close_notifies_client_and_next_message_opens_new(s, clock_):
    c, erlan, tima, t = await _queued(s)
    await tickets.take(s, t.id, erlan)
    await clear_outbox(s)
    assert not await tickets.close_ticket(s, t.id, tima)
    assert await tickets.close_ticket(s, t.id, erlan)
    await s.refresh(t)
    assert (t.status, t.closed_at, t.closed_by_id) == ("closed", clock_.now, erlan.id)
    assert erlan.active_ticket_id is None
    wa = await payloads(s, "wa")
    assert wa[-1]["text"] == f"Заявка #{t.id} выполнена. Если проблема осталась — напишите снова."
    m = Message(client_id=c.id, direction="in", kind="text", text="Опять не печатает")
    s.add(m)
    await s.flush()
    t2 = await tickets.on_client_message(s, c, m)
    assert t2.id != t.id and t2.status == "new"


async def _two_in_work(s):
    c1 = await make_client(s)
    c2 = await make_client(s, phone="77017654321", full_name="Бауыржан")
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    t1 = await make_ticket(s, c1, status="in_progress", assignee=erlan)
    t2 = await make_ticket(s, c2, status="in_progress", assignee=erlan)
    erlan.active_ticket_id = t2.id
    s.add(TgLink(tg_chat_id=1, tg_message_id=700, ticket_id=t1.id))
    await s.flush()
    return erlan, t1, t2


async def test_resolve_prefers_reply_link(s, clock_):
    erlan, t1, t2 = await _two_in_work(s)
    assert await tickets.resolve_target(s, erlan, 700) == (t1, "")


async def test_plain_message_goes_to_active(s, clock_):
    erlan, t1, t2 = await _two_in_work(s)
    assert await tickets.resolve_target(s, erlan, None) == (t2, "")
    assert await tickets.resolve_target(s, erlan, 99999) == (t2, "")  # reply на не-заявку


async def test_reply_to_closed_ticket_is_refused_not_redirected(s, clock_):
    erlan, t1, t2 = await _two_in_work(s)
    t1.status = "closed"
    await s.flush()
    t, err = await tickets.resolve_target(s, erlan, 700)
    assert t is None
    assert err == f"Заявка #{t1.id} закрыта или не у вас — сообщение не отправлено."


async def test_no_active_after_close(s, clock_):
    erlan, t1, t2 = await _two_in_work(s)
    assert await tickets.close_ticket(s, t2.id, erlan)
    t, err = await tickets.resolve_target(s, erlan, None)
    assert t is None
    assert err == "Нет активной заявки — сообщение не отправлено. /my — ваши заявки."


async def test_send_staff_reply_text_and_file(s, clock_):
    erlan, t1, t2 = await _two_in_work(s)
    m = await tickets.send_staff_reply(s, erlan, t2, "text", "Иду", None, None)
    assert (m.direction, m.staff_id, m.ticket_id) == ("out", erlan.id, t2.id)
    await tickets.send_staff_reply(s, erlan, t2, "image", "скрин", "/f/a.jpg", "a.jpg")
    wa = await payloads(s, "wa")
    assert wa[0] == {"op": "text", "chat_id": "77017654321@c.us", "text": "Иду", "ticket_id": t2.id}
    assert (wa[1]["op"], wa[1]["path"], wa[1]["file_name"], wa[1]["caption"]) == (
        "file",
        "/f/a.jpg",
        "a.jpg",
        "скрин",
    )


async def test_set_active_and_my_tickets(s, clock_):
    erlan, t1, t2 = await _two_in_work(s)
    assert (await tickets.set_active(s, erlan, t1.id)).id == t1.id
    assert erlan.active_ticket_id == t1.id
    other = await make_staff(s, tg_id=3, name="Алибек")
    assert await tickets.set_active(s, other, t1.id) is None
    rows = await tickets.my_tickets(s, erlan)
    assert [t.id for t, _ in rows] == [t1.id, t2.id]
    view = await tickets.recent_view(s, t1)
    assert view.startswith(f"▶️ Активная заявка #{t1.id} · Айгерим Сериккызы")
```

- [ ] **Step 2: Запустить, убедиться, что падает**

Run: `make test`
Expected: FAIL — `AttributeError: module 'helpdesk.tickets.service' has no attribute 'take'`.

- [ ] **Step 3: Дописать в `src/helpdesk/tickets/service.py`**

Добавить в импорты: `from dataclasses import dataclass`, `from pathlib import Path`, `from sqlalchemy import func`, `from helpdesk.db.models import TgLink`. Код в конец файла:

```python
@dataclass
class TakeResult:
    ok: bool
    holder_name: str | None


async def _working_text(s, t: Ticket, client: Client) -> str:
    msgs = await ticket_messages(s, t.id)
    return render.working_card(t, client, msgs, await queries.staff_names(s))


async def take(s, ticket_id: int, staff: Staff) -> TakeResult:
    now = clock.utcnow()
    res = await s.execute(
        update(Ticket)
        .where(Ticket.id == ticket_id, Ticket.status == "new")
        .values(
            status="in_progress",
            assignee_id=staff.id,
            first_taken_at=func.coalesce(Ticket.first_taken_at, now),
        )
        .returning(Ticket.id)
        .execution_options(synchronize_session=False)
    )
    claimed = res.scalar_one_or_none()
    t = await s.get(Ticket, ticket_id, populate_existing=True)
    if claimed is None:
        holder = await s.get(Staff, t.assignee_id) if t and t.assignee_id else None
        mine = select(TgCard).where(
            TgCard.ticket_id == ticket_id, TgCard.staff_id == staff.id, TgCard.state == "open"
        )
        for card in (await s.scalars(mine)).all():
            outbox.tg_delete_card(s, card, f"🔒 #{ticket_id} уже взята")
        return TakeResult(False, holder.name if holder else None)

    client = await s.get(Client, t.client_id)
    s.add(TicketEvent(ticket_id=t.id, type="taken", staff_id=staff.id))
    staff.active_ticket_id = t.id
    working = await _working_text(s, t, client)
    cards = (
        await s.scalars(select(TgCard).where(TgCard.ticket_id == t.id, TgCard.state == "open"))
    ).all()
    edited = False
    for card in cards:
        if card.staff_id != staff.id:
            outbox.tg_delete_card(s, card, f"🔒 #{t.id} взял {staff.name}")
            continue
        card.state = "kept"
        if card.has_button:
            outbox.tg_edit(s, card.tg_chat_id, card.tg_message_id, working, WORK_BUTTONS(t.id))
            edited = True
    if not edited:
        outbox.tg_send(
            s, staff.tg_chat_id, text=working, buttons=WORK_BUTTONS(t.id), ticket_id=t.id
        )
    outbox.wa_text(
        s,
        client.wa_chat_id,
        texts.t(client.lang, "taken", id=t.id, name=staff.name),
        ticket_id=t.id,
    )
    return TakeResult(True, staff.name)


async def _owned_in_progress(s, ticket_id: int, staff: Staff) -> Ticket | None:
    t = await s.get(Ticket, ticket_id, with_for_update=True, populate_existing=True)
    if t is None or t.status != "in_progress":
        return None
    if t.assignee_id != staff.id and not staff.is_admin:
        return None
    return t


async def _release(s, t: Ticket) -> None:
    prev = await s.get(Staff, t.assignee_id) if t.assignee_id else None
    if prev is not None and prev.active_ticket_id == t.id:
        prev.active_ticket_id = None


async def return_ticket(s, ticket_id: int, staff: Staff) -> bool:
    t = await _owned_in_progress(s, ticket_id, staff)
    if t is None:
        return False
    await _release(s, t)
    t.status = "new"
    t.assignee_id = None
    s.add(TicketEvent(ticket_id=t.id, type="returned", staff_id=staff.id))
    await s.flush()
    await broadcast(s, t)
    return True


async def close_ticket(s, ticket_id: int, staff: Staff) -> bool:
    t = await _owned_in_progress(s, ticket_id, staff)
    if t is None:
        return False
    await _release(s, t)
    t.status = "closed"
    t.closed_at = clock.utcnow()
    t.closed_by_id = staff.id
    s.add(TicketEvent(ticket_id=t.id, type="closed", staff_id=staff.id))
    client = await s.get(Client, t.client_id)
    outbox.wa_text(s, client.wa_chat_id, texts.t(client.lang, "closed", id=t.id), ticket_id=t.id)
    await s.flush()
    return True


def _is_mine(t: Ticket | None, staff: Staff) -> bool:
    return t is not None and t.status == "in_progress" and t.assignee_id == staff.id


async def resolve_target(s, staff: Staff, reply_to: int | None) -> tuple[Ticket | None, str]:
    if reply_to is not None:
        link = await s.get(TgLink, (staff.tg_chat_id, reply_to))
        if link is not None:
            t = await s.get(Ticket, link.ticket_id)
            if _is_mine(t, staff):
                return t, ""
            return None, f"Заявка #{link.ticket_id} закрыта или не у вас — сообщение не отправлено."
    if staff.active_ticket_id:
        t = await s.get(Ticket, staff.active_ticket_id)
        if _is_mine(t, staff):
            return t, ""
    return None, "Нет активной заявки — сообщение не отправлено. /my — ваши заявки."


async def send_staff_reply(
    s, staff: Staff, t: Ticket, kind: str, text: str | None, file_path: str | None, file_name: str | None
) -> Message:
    client = await s.get(Client, t.client_id)
    m = Message(
        client_id=client.id,
        ticket_id=t.id,
        direction="out",
        staff_id=staff.id,
        kind=kind,
        text=text,
        file_path=file_path,
        file_name=file_name,
    )
    s.add(m)
    if file_path:
        name = file_name or Path(file_path).name
        outbox.wa_file(s, client.wa_chat_id, file_path, name, text, ticket_id=t.id)
    else:
        outbox.wa_text(s, client.wa_chat_id, text or "", ticket_id=t.id)
    await s.flush()
    return m


async def set_active(s, staff: Staff, ticket_id: int) -> Ticket | None:
    t = await s.get(Ticket, ticket_id)
    if not _is_mine(t, staff):
        return None
    staff.active_ticket_id = t.id
    return t


async def my_tickets(s, staff: Staff) -> list[tuple[Ticket, Client]]:
    q = (
        select(Ticket, Client)
        .join(Client, Client.id == Ticket.client_id)
        .where(Ticket.status == "in_progress", Ticket.assignee_id == staff.id)
        .order_by(Ticket.id)
    )
    return [(t, c) for t, c in (await s.execute(q)).all()]


async def recent_view(s, t: Ticket, limit: int = 5) -> str:
    client = await s.get(Client, t.client_id)
    msgs = (await ticket_messages(s, t.id))[-limit:]
    name = client.full_name or client.wa_name or "+" + client.phone
    body = render.history(msgs, await queries.staff_names(s))
    return f"▶️ Активная заявка #{t.id} · {name}\n\n{render.clip(body)}"


async def link(s, chat_id: int, message_id: int, ticket_id: int) -> None:
    await s.merge(TgLink(tg_chat_id=chat_id, tg_message_id=message_id, ticket_id=ticket_id))
```

- [ ] **Step 4: Тесты зелёные**

Run: `make test`
Expected: все PASS. Если `test_concurrent_take_has_one_winner` зависает, проверьте, что каждая попытка использует свою сессию из `sf()` (два соединения), а не общую.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix src tests && uv run ruff format src tests
git add -A
git commit -m "feat(tickets): take/return/close with atomic claim, reply routing

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 7: Анкета и обработка входящего сообщения WhatsApp

**Files:**
- Create: `src/helpdesk/intake/__init__.py`, `src/helpdesk/intake/service.py`, `src/helpdesk/inbound.py`
- Test: `tests/test_inbound.py`

**Interfaces:**
- Consumes: `IncomingMessage` (задача 4), `tickets.on_client_message` (задача 5), `outbox.wa_text`, `texts`.
- Produces:
  - `intake.parse_lang(text: str | None) -> str | None`
  - `intake.start(s, client) -> None` — отправляет `texts.CHOOSE_LANG`
  - `intake.advance(s, client, msg: IncomingMessage) -> bool` — `True`, когда анкета только что завершена
  - `await inbound.handle_incoming(s, msg: IncomingMessage, file_path: str | None) -> None`

- [ ] **Step 1: Падающие тесты `tests/test_inbound.py`**

```python
from sqlalchemy import func, select

from helpdesk import texts
from helpdesk.db.models import Client, Message, Ticket
from helpdesk.inbound import handle_incoming
from helpdesk.intake.service import parse_lang
from helpdesk.whatsapp.parse import IncomingMessage
from tests.factories import make_client, make_staff, payloads

CHAT = "77011234567@c.us"


def incoming(text="Привет", *, mid="M1", kind="text", chat=CHAT, **kw) -> IncomingMessage:
    return IncomingMessage(
        wa_message_id=mid,
        chat_id=chat,
        phone=chat.split("@")[0],
        sender_name="Aigerim",
        kind=kind,
        text=text,
        **kw,
    )


async def _feed(s, *texts_):
    for i, t in enumerate(texts_):
        await handle_incoming(s, incoming(t, mid=f"M{i}"), None)


async def _wa_texts(s):
    return [p["text"] for p in await payloads(s, "wa")]


def test_parse_lang():
    assert parse_lang(" 1 ") == "ru" and parse_lang("Рус") == "ru"
    assert parse_lang("2") == "kk" and parse_lang("қазақша") == "kk" and parse_lang("KZ") == "kk"
    assert parse_lang("3") is None and parse_lang(None) is None


async def test_full_intake_ru_creates_ticket_with_first_message(s, clock_):
    await make_staff(s, tg_id=1, name="Ерлан")
    await _feed(s, "Не печатает принтер", "1", "Айгерим Сериккызы", "Бухгалтерия, 214")
    c = await s.scalar(select(Client))
    assert (c.lang, c.full_name, c.location, c.intake_step, c.wa_name) == (
        "ru",
        "Айгерим Сериккызы",
        "Бухгалтерия, 214",
        "done",
        "Aigerim",
    )
    t = await s.scalar(select(Ticket))
    msgs = (await s.scalars(select(Message.text).where(Message.ticket_id == t.id))).all()
    assert msgs == ["Не печатает принтер"]
    assert await _wa_texts(s) == [
        texts.CHOOSE_LANG,
        texts.t("ru", "ask_name"),
        texts.t("ru", "ask_location"),
        "Заявка #1 принята. Специалист скоро ответит.",
    ]
    cards = [p for p in await payloads(s, "tg") if p.get("buttons")]
    assert len(cards) == 1 and "Бухгалтерия, 214" in cards[0]["text"]


async def test_full_intake_kk(s, clock_):
    await _feed(s, "Сәлем", "2", "Асқар Нұрланұлы", "Қойма, 5")
    assert await _wa_texts(s) == [
        texts.CHOOSE_LANG,
        texts.t("kk", "ask_name"),
        texts.t("kk", "ask_location"),
        "#1 өтінім қабылданды. Маман жақын арада жауап береді.",
    ]


async def test_unknown_language_answer_repeats_question(s, clock_):
    await _feed(s, "Привет", "3")
    assert await _wa_texts(s) == [texts.CHOOSE_LANG, texts.CHOOSE_LANG]
    assert (await s.scalar(select(Client))).intake_step == "lang"


async def test_name_must_be_text(s, clock_):
    await _feed(s, "Привет", "1")
    await handle_incoming(s, incoming(None, mid="P1", kind="image", download_url="u"), "/f/x.jpg")
    assert (await _wa_texts(s))[-1] == texts.t("ru", "bad_name")
    assert (await s.scalar(select(Client))).intake_step == "name"
    await handle_incoming(s, incoming("А", mid="P2"), None)  # 1 символ
    assert (await _wa_texts(s))[-1] == texts.t("ru", "bad_name")


async def test_duplicate_notification_ignored(s, clock_):
    await handle_incoming(s, incoming("Привет", mid="SAME"), None)
    await handle_incoming(s, incoming("Привет", mid="SAME"), None)
    assert await s.scalar(select(func.count()).select_from(Message)) == 1
    assert await _wa_texts(s) == [texts.CHOOSE_LANG]


async def test_known_client_opens_ticket_directly(s, clock_):
    await make_client(s)
    await handle_incoming(s, incoming("Не работает интернет", mid="K1"), None)
    t = await s.scalar(select(Ticket))
    assert t is not None
    assert await _wa_texts(s) == ["Заявка #1 принята. Специалист скоро ответит."]


async def test_file_path_is_stored(s, clock_):
    await make_client(s)
    msg = incoming("скрин", mid="F1", kind="image", download_url="u", file_name="a.jpg")
    await handle_incoming(s, msg, "/f/a.jpg")
    m = await s.scalar(select(Message))
    assert (m.kind, m.text, m.file_path, m.file_name) == ("image", "скрин", "/f/a.jpg", "a.jpg")


async def test_sticker_gets_unsupported_reply_and_goes_into_ticket(s, clock_):
    await make_client(s)
    await handle_incoming(s, incoming("[стикер]", mid="S1", kind="other"), None)
    assert await _wa_texts(s) == [
        texts.t("ru", "unsupported"),
        "Заявка #1 принята. Специалист скоро ответит.",
    ]
    m = await s.scalar(select(Message))
    assert m.ticket_id == 1 and m.text == "[стикер]"
```

- [ ] **Step 2: Запустить, убедиться, что падает**

Run: `make test`
Expected: FAIL — `ModuleNotFoundError: helpdesk.inbound`.

- [ ] **Step 3: `src/helpdesk/intake/service.py`** (и пустой `intake/__init__.py`)

```python
from helpdesk import texts
from helpdesk.db.models import Client
from helpdesk.delivery import outbox
from helpdesk.whatsapp.parse import IncomingMessage

_LANGS = {
    "1": "ru",
    "ru": "ru",
    "рус": "ru",
    "русский": "ru",
    "2": "kk",
    "kk": "kk",
    "kz": "kk",
    "kaz": "kk",
    "каз": "kk",
    "қаз": "kk",
    "қазақша": "kk",
    "казахский": "kk",
}


def parse_lang(text: str | None) -> str | None:
    if not text:
        return None
    return _LANGS.get(text.strip().lower().rstrip("."))


def start(s, client: Client) -> None:
    outbox.wa_text(s, client.wa_chat_id, texts.CHOOSE_LANG)


def _say(s, client: Client, key: str) -> None:
    outbox.wa_text(s, client.wa_chat_id, texts.t(client.lang, key))


def advance(s, client: Client, msg: IncomingMessage) -> bool:
    text = (msg.text or "").strip() if msg.kind == "text" else ""
    if client.intake_step == "lang":
        lang = parse_lang(text)
        if lang is None:
            start(s, client)
            return False
        client.lang = lang
        client.intake_step = "name"
        _say(s, client, "ask_name")
        return False
    if client.intake_step == "name":
        if not 2 <= len(text) <= 100:
            _say(s, client, "bad_name")
            return False
        client.full_name = text
        client.intake_step = "location"
        _say(s, client, "ask_location")
        return False
    if client.intake_step == "location":
        if not 1 <= len(text) <= 200:
            _say(s, client, "bad_location")
            return False
        client.location = text
        client.intake_step = "done"
        return True
    return True
```

- [ ] **Step 4: `src/helpdesk/inbound.py`**

```python
from sqlalchemy import select

from helpdesk import texts
from helpdesk.db.models import Client, Message
from helpdesk.delivery import outbox
from helpdesk.intake import service as intake
from helpdesk.tickets import service as tickets
from helpdesk.whatsapp.parse import IncomingMessage


def _save(s, client: Client, msg: IncomingMessage, file_path: str | None, *, intake_answer: bool):
    m = Message(
        client_id=client.id,
        direction="in",
        kind=msg.kind,
        text=msg.text,
        file_path=file_path,
        file_name=msg.file_name,
        wa_message_id=msg.wa_message_id,
        intake=intake_answer,
    )
    s.add(m)
    return m


async def handle_incoming(s, msg: IncomingMessage, file_path: str | None) -> None:
    dup = await s.scalar(select(Message.id).where(Message.wa_message_id == msg.wa_message_id))
    if dup is not None:
        return
    client = await s.scalar(select(Client).where(Client.wa_chat_id == msg.chat_id))
    if client is None:
        client = Client(wa_chat_id=msg.chat_id, phone=msg.phone, wa_name=msg.sender_name)
        s.add(client)
        await s.flush()
        _save(s, client, msg, file_path, intake_answer=False)
        intake.start(s, client)
        await s.flush()
        return
    if client.intake_step != "done":
        _save(s, client, msg, file_path, intake_answer=True)
        if intake.advance(s, client, msg):
            await s.flush()
            await tickets.on_client_message(s, client, None)
        await s.flush()
        return
    m = _save(s, client, msg, file_path, intake_answer=False)
    await s.flush()
    if msg.kind == "other":
        outbox.wa_text(s, client.wa_chat_id, texts.t(client.lang, "unsupported"))
    await tickets.on_client_message(s, client, m)
```

- [ ] **Step 5: Тесты зелёные**

Run: `make test`
Expected: все PASS.

- [ ] **Step 6: Commit**

```bash
uv run ruff check --fix src tests && uv run ruff format src tests
git add -A
git commit -m "feat(intake): RU/KK onboarding and inbound WhatsApp handling with dedup

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Специалисты — регистрация, одобрение, отключение, смена

**Files:**
- Create: `src/helpdesk/staff/__init__.py`, `src/helpdesk/staff/service.py`
- Modify: `src/helpdesk/tickets/service.py` (добавить `deliver_queue_to`)
- Test: `tests/test_staff.py`

**Interfaces:**
- Consumes: `tickets.return_ticket`, `tickets._send_cards`, `tickets.ticket_messages`, `queries.active_admins`, `outbox.tg_send`.
- Produces:
  - `await tickets.deliver_queue_to(s, staff) -> int` — отправляет специалисту карточки всех заявок `new`, возвращает их число
  - `await staff_svc.register(s, *, tg_user_id, chat_id, name, username, admin_ids: set[int]) -> Staff`
  - `await staff_svc.approve(s, admin, staff_id) -> Staff | None`, `reject(...) -> Staff | None`
  - `await staff_svc.disable(s, admin, staff_id) -> bool`, `enable(...) -> bool`, `toggle_admin(...) -> bool`
  - `await staff_svc.set_shift(s, staff, on: bool) -> int | None` (`None`, если статус не изменился; иначе число доставленных заявок из очереди, при `off` — 0)
  - `await staff_svc.list_staff(s) -> list[Staff]`, `staff_svc.staff_line(st) -> str`, `staff_svc.staff_buttons(st, me) -> Buttons | None`

- [ ] **Step 1: Падающие тесты `tests/test_staff.py`**

```python
from helpdesk.staff import service as staff_svc
from tests.factories import clear_outbox, make_client, make_staff, make_ticket, payloads


async def test_admin_from_env_is_active(s, clock_):
    st = await staff_svc.register(
        s, tg_user_id=9, chat_id=9, name="Админ", username=None, admin_ids={9}
    )
    assert (st.status, st.is_admin) == ("active", True)
    assert await payloads(s) == []


async def test_newcomer_is_pending_and_admins_are_asked(s, clock_):
    await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    st = await staff_svc.register(
        s, tg_user_id=5, chat_id=5, name="Ерлан", username="erlan", admin_ids={9}
    )
    assert (st.status, st.is_admin) == ("pending", False)
    p = await payloads(s, "tg")
    assert p[0]["chat_id"] == 9
    assert p[0]["text"] == "🙋 Ерлан (@erlan) просит доступ к helpdesk-боту"
    assert p[0]["buttons"] == [[["✅ Одобрить", f"approve:{st.id}"], ["❌ Отклонить", f"reject:{st.id}"]]]


async def test_register_is_idempotent_and_promotes_env_admin(s, clock_):
    a = await staff_svc.register(s, tg_user_id=5, chat_id=5, name="E", username=None, admin_ids=set())
    b = await staff_svc.register(s, tg_user_id=5, chat_id=55, name="E", username=None, admin_ids=set())
    assert a.id == b.id and b.tg_chat_id == 55 and b.status == "pending"
    c = await staff_svc.register(s, tg_user_id=5, chat_id=55, name="E", username=None, admin_ids={5})
    assert (c.status, c.is_admin) == ("active", True)


async def test_approve_and_reject_require_admin(s, clock_):
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    user = await make_staff(s, tg_id=1, name="Тимур")
    p1 = await make_staff(s, tg_id=5, name="Ерлан", status="pending")
    p2 = await make_staff(s, tg_id=6, name="Гость", status="pending")
    assert await staff_svc.approve(s, user, p1.id) is None
    assert (await staff_svc.approve(s, admin, p1.id)).status == "active"
    assert await staff_svc.approve(s, admin, p1.id) is None  # уже одобрен
    assert (await staff_svc.reject(s, admin, p2.id)).status == "disabled"
    texts_ = {p["chat_id"]: p["text"] for p in await payloads(s, "tg")}
    assert texts_ == {5: "✅ Доступ открыт. /help — список команд.", 6: "Доступ отклонён."}


async def test_disable_returns_tickets_to_queue(s, clock_):
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True, on_shift=False)
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    await make_staff(s, tg_id=2, name="Тимур")
    c = await make_client(s)
    t = await make_ticket(s, c, status="in_progress", assignee=erlan)
    erlan.active_ticket_id = t.id
    assert await staff_svc.disable(s, admin, erlan.id)
    await s.refresh(t)
    assert (erlan.status, t.status, t.assignee_id, erlan.active_ticket_id) == (
        "disabled",
        "new",
        None,
        None,
    )
    sends = [p for p in await payloads(s, "tg") if p["op"] == "send" and p.get("buttons")]
    assert [p["chat_id"] for p in sends] == [2]


async def test_admin_cannot_disable_self(s, clock_):
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    assert not await staff_svc.disable(s, admin, admin.id)
    assert not await staff_svc.toggle_admin(s, admin, admin.id)


async def test_enable_and_toggle_admin(s, clock_):
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    u = await make_staff(s, tg_id=1, name="Тимур", status="disabled")
    assert await staff_svc.enable(s, admin, u.id) and u.status == "active"
    assert await staff_svc.toggle_admin(s, admin, u.id) and u.is_admin
    assert await staff_svc.toggle_admin(s, admin, u.id) and not u.is_admin


async def test_shift_on_delivers_queue(s, clock_):
    tima = await make_staff(s, tg_id=2, name="Тимур", on_shift=False)
    c = await make_client(s)
    t = await make_ticket(s, c)
    t.broadcast_seq = 3
    assert await staff_svc.set_shift(s, tima, True) == 1
    p = await payloads(s, "tg")
    assert len(p) == 1 and p[0]["chat_id"] == 2 and p[0]["card_seq"] == 3
    assert await staff_svc.set_shift(s, tima, True) is None  # уже на смене — без дублей
    await clear_outbox(s)
    assert await staff_svc.set_shift(s, tima, False) == 0
    assert await payloads(s) == []


async def test_staff_line_and_buttons(s, clock_):
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    u = await make_staff(s, tg_id=1, name="Тимур")
    assert staff_svc.staff_line(u) == "Тимур — активен, на смене"
    assert staff_svc.staff_line(admin) == "👑 Админ — активен, на смене"
    assert staff_svc.staff_buttons(u, admin) == [
        [("⛔ Отключить", f"stf_dis:{u.id}"), ("👑 Сделать админом", f"stf_adm:{u.id}")]
    ]
    assert staff_svc.staff_buttons(admin, admin) is None
```

- [ ] **Step 2: Запустить, убедиться, что падает**

Run: `make test`
Expected: FAIL — `ModuleNotFoundError: helpdesk.staff`.

- [ ] **Step 3: Дописать в конец `src/helpdesk/tickets/service.py`**

```python
async def deliver_queue_to(s, staff: Staff) -> int:
    q = (
        select(Ticket, Client)
        .join(Client, Client.id == Ticket.client_id)
        .where(Ticket.status == "new")
        .order_by(Ticket.id)
    )
    rows = (await s.execute(q)).all()
    for t, c in rows:
        _send_cards(s, t, c, await ticket_messages(s, t.id), staff)
    return len(rows)
```

- [ ] **Step 4: `src/helpdesk/staff/service.py`** (и пустой `staff/__init__.py`)

```python
from sqlalchemy import select

from helpdesk.db import queries
from helpdesk.db.models import Staff, Ticket
from helpdesk.delivery import outbox
from helpdesk.delivery.outbox import Buttons
from helpdesk.tickets import service as tickets

STATUS_RU = {"pending": "ждёт одобрения", "active": "активен", "disabled": "отключён"}


async def register(s, *, tg_user_id, chat_id, name, username, admin_ids: set[int]) -> Staff:
    is_env_admin = tg_user_id in admin_ids
    st = await s.scalar(select(Staff).where(Staff.tg_user_id == tg_user_id))
    if st is not None:
        st.tg_chat_id = chat_id
        if is_env_admin:
            st.is_admin, st.status = True, "active"
        return st
    st = Staff(
        tg_user_id=tg_user_id,
        tg_chat_id=chat_id,
        name=name,
        username=username,
        is_admin=is_env_admin,
        status="active" if is_env_admin else "pending",
    )
    s.add(st)
    await s.flush()
    if not is_env_admin:
        who = f"{name} (@{username})" if username else name
        for a in await queries.active_admins(s):
            outbox.tg_send(
                s,
                a.tg_chat_id,
                text=f"🙋 {who} просит доступ к helpdesk-боту",
                buttons=[[("✅ Одобрить", f"approve:{st.id}"), ("❌ Отклонить", f"reject:{st.id}")]],
            )
    return st


async def _target(s, admin: Staff, staff_id: int) -> Staff | None:
    if not (admin.is_admin and admin.status == "active"):
        return None
    return await s.get(Staff, staff_id)


async def approve(s, admin: Staff, staff_id: int) -> Staff | None:
    st = await _target(s, admin, staff_id)
    if st is None or st.status != "pending":
        return None
    st.status = "active"
    outbox.tg_send(s, st.tg_chat_id, text="✅ Доступ открыт. /help — список команд.")
    return st


async def reject(s, admin: Staff, staff_id: int) -> Staff | None:
    st = await _target(s, admin, staff_id)
    if st is None or st.status != "pending":
        return None
    st.status = "disabled"
    outbox.tg_send(s, st.tg_chat_id, text="Доступ отклонён.")
    return st


async def disable(s, admin: Staff, staff_id: int) -> bool:
    st = await _target(s, admin, staff_id)
    if st is None or st.id == admin.id or st.status == "disabled":
        return False
    st.status = "disabled"
    await s.flush()
    q = select(Ticket.id).where(Ticket.status == "in_progress", Ticket.assignee_id == st.id)
    for tid in (await s.scalars(q)).all():
        await tickets.return_ticket(s, tid, admin)
    return True


async def enable(s, admin: Staff, staff_id: int) -> bool:
    st = await _target(s, admin, staff_id)
    if st is None or st.status == "active":
        return False
    st.status = "active"
    return True


async def toggle_admin(s, admin: Staff, staff_id: int) -> bool:
    st = await _target(s, admin, staff_id)
    if st is None or st.id == admin.id:
        return False
    st.is_admin = not st.is_admin
    return True


async def set_shift(s, staff: Staff, on: bool) -> int | None:
    if staff.on_shift == on:
        return None
    staff.on_shift = on
    if not on:
        return 0
    return await tickets.deliver_queue_to(s, staff)


async def list_staff(s) -> list[Staff]:
    return list((await s.scalars(select(Staff).order_by(Staff.id))).all())


def staff_line(st: Staff) -> str:
    crown = "👑 " if st.is_admin else ""
    nick = f" (@{st.username})" if st.username else ""
    shift = ", на смене" if st.status == "active" and st.on_shift else ""
    return f"{crown}{st.name}{nick} — {STATUS_RU[st.status]}{shift}"


def staff_buttons(st: Staff, me: Staff) -> Buttons | None:
    if st.id == me.id:
        return None
    if st.status == "active":
        adm = "Снять админа" if st.is_admin else "👑 Сделать админом"
        return [[("⛔ Отключить", f"stf_dis:{st.id}"), (adm, f"stf_adm:{st.id}")]]
    return [[("✅ Включить", f"stf_en:{st.id}")]]
```

- [ ] **Step 5: Тесты зелёные**

Run: `make test`
Expected: все PASS.

- [ ] **Step 6: Commit**

```bash
uv run ruff check --fix src tests && uv run ruff format src tests
git add -A
git commit -m "feat(staff): registration with admin approval, disable, shifts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Напоминания о невзятых заявках

**Files:**
- Create: `src/helpdesk/reminders.py`
- Test: `tests/test_reminders.py`

**Interfaces:**
- Consumes: `tickets.broadcast(s, t, waited_min=..., warn_if_empty=False)`.
- Produces: `await reminders.run_once(sf, after_min: int) -> int`, `await reminders.run(sf, after_min, stop: asyncio.Event, interval=60.0)`.

- [ ] **Step 1: Падающие тесты `tests/test_reminders.py`**

```python
from datetime import timedelta

from sqlalchemy import select

from helpdesk import reminders
from helpdesk.db.models import Ticket
from tests.factories import make_client, make_staff, make_ticket, payloads


async def test_reminds_only_stale_queued_tickets(sf, clock_):
    now = clock_.now
    async with sf() as s, s.begin():
        erlan = await make_staff(s, tg_id=1, name="Ерлан")
        old = await make_ticket(s, await make_client(s, phone="1"))
        old.created_at = old.last_broadcast_at = now - timedelta(minutes=11)
        fresh = await make_ticket(s, await make_client(s, phone="2"))
        fresh.last_broadcast_at = now - timedelta(minutes=5)
        work = await make_ticket(
            s, await make_client(s, phone="3"), status="in_progress", assignee=erlan
        )
        work.last_broadcast_at = now - timedelta(minutes=30)
        old_id = old.id
    assert await reminders.run_once(sf, 10) == 1
    async with sf() as s:
        t = await s.get(Ticket, old_id)
        assert t.last_broadcast_at == now and t.broadcast_seq == 1
        cards = [p for p in await payloads(s, "tg") if p.get("buttons")]
        assert len(cards) == 1
        assert cards[0]["text"].startswith(f"⏰ Заявка #{old_id} ждёт 11 мин")
    assert await reminders.run_once(sf, 10) == 0


async def test_reminder_without_staff_does_not_warn_admins(sf, clock_):
    async with sf() as s, s.begin():
        await make_staff(s, tg_id=9, name="Админ", is_admin=True, on_shift=False)
        t = await make_ticket(s, await make_client(s))
        t.last_broadcast_at = clock_.now - timedelta(minutes=15)
    assert await reminders.run_once(sf, 10) == 1
    async with sf() as s:
        assert await payloads(s, "tg") == []
        assert (await s.scalar(select(Ticket))).last_broadcast_at == clock_.now
```

- [ ] **Step 2: Запустить, убедиться, что падает**

Run: `make test`
Expected: FAIL — `ImportError: cannot import name 'reminders'`.

- [ ] **Step 3: `src/helpdesk/reminders.py`**

```python
import asyncio
from datetime import timedelta

import structlog
from sqlalchemy import select

from helpdesk import clock
from helpdesk.db.models import Ticket
from helpdesk.tickets import service as tickets

log = structlog.get_logger()


async def run_once(sf, after_min: int) -> int:
    async with sf() as s, s.begin():
        now = clock.utcnow()
        q = (
            select(Ticket)
            .where(
                Ticket.status == "new",
                Ticket.last_broadcast_at <= now - timedelta(minutes=after_min),
            )
            .order_by(Ticket.id)
            .with_for_update(skip_locked=True)
        )
        due = list((await s.scalars(q)).all())
        for t in due:
            waited = max(1, int((now - t.created_at).total_seconds() // 60))
            await tickets.broadcast(s, t, waited_min=waited, warn_if_empty=False)
    return len(due)


async def run(sf, after_min: int, stop: asyncio.Event, interval: float = 60.0) -> None:
    while not stop.is_set():
        try:
            await run_once(sf, after_min)
        except Exception:
            log.exception("reminders failed")
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except TimeoutError:
            pass
```

- [ ] **Step 4: Тесты зелёные**

Run: `make test`
Expected: все PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix src tests && uv run ruff format src tests
git add -A
git commit -m "feat(reminders): re-broadcast tickets nobody took

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 10: Статистика, поиск, карточка заявки, выгрузка в Excel

**Files:**
- Create: `src/helpdesk/stats/__init__.py`, `src/helpdesk/stats/service.py`, `src/helpdesk/stats/export.py`
- Test: `tests/test_stats.py`

**Interfaces:**
- Consumes: `tickets.ticket_messages`, `tickets.TG_FILE_KIND`, `render.*`, `queries.staff_names`, `clock`.
- Produces:
  - `stats.PERIOD_TITLES: dict[str, str]`, `stats.STATUS_RU: dict[str, str]`
  - `stats.period_bounds(kind, now) -> tuple[datetime | None, datetime | None]`; `kind` ∈ `today|week|month|prev_month|all`; начало включительно, конец не включительно
  - `stats.between(col, start, end) -> list` (условия SQLAlchemy)
  - `Stats`, `StaffStat` (dataclass); `await stats.compute(s, start, end) -> Stats`; `stats.render_stats(st, title) -> str`
  - `stats.parse_ticket_id(q) -> int | None` (только цифры, необязательный `#`, не больше 6 цифр)
  - `await stats.search(s, q, limit=10) -> list[tuple[Ticket, Client, str | None]]`; `stats.search_line(t, c, assignee_name) -> str`
  - `await stats.ticket_details(s, ticket_id) -> str | None`
  - `Conversation(chunks: list[str], files: list[tuple[str, str, str | None]])`; `await stats.conversation(s, ticket_id) -> Conversation | None`
  - `await stats.open_tickets_text(s) -> str`
  - `export.HEADERS: list[str]`; `await export.export_xlsx(s, start, end) -> bytes`

- [ ] **Step 1: Падающие тесты `tests/test_stats.py`**

```python
from datetime import datetime, timedelta
from io import BytesIO

from openpyxl import load_workbook

from helpdesk import clock
from helpdesk.db.models import Message, TicketEvent
from helpdesk.stats import export
from helpdesk.stats import service as stats
from tests.factories import make_client, make_staff, make_ticket


def L(*args):  # локальное время Алматы
    return datetime(*args, tzinfo=clock.LOCAL_TZ)


async def _dataset(s, now):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    tima = await make_staff(s, tg_id=2, name="Тимур")
    c1 = await make_client(s)
    c2 = await make_client(s, phone="77017654321", full_name="Бауыржан Ахметов", location="Склад")
    c3 = await make_client(
        s, phone="77010000003", full_name="Сауле", location="Бухгалтерия, каб. 214"
    )

    def at(**kw):
        return now - timedelta(**kw)

    t1 = await make_ticket(s, c1, status="closed", assignee=erlan)
    t1.created_at, t1.first_taken_at = at(hours=2), at(hours=1, minutes=50)
    t1.closed_at, t1.closed_by_id = at(hours=1), erlan.id
    t2 = await make_ticket(s, c2, status="in_progress", assignee=tima, text="Нет интернета")
    t2.created_at, t2.first_taken_at = at(minutes=30), at(minutes=20)
    t3 = await make_ticket(s, c3, text="Мышь")
    t3.created_at = at(minutes=5)
    old = await make_ticket(s, c1, status="closed", assignee=tima, text="Старое")
    old.created_at = old.first_taken_at = old.closed_at = at(days=40)
    old.closed_by_id = tima.id
    s.add_all(
        [
            TicketEvent(ticket_id=t1.id, type="created", created_at=t1.created_at),
            TicketEvent(
                ticket_id=t1.id, type="taken", staff_id=erlan.id, created_at=t1.first_taken_at
            ),
            TicketEvent(ticket_id=t1.id, type="closed", staff_id=erlan.id, created_at=t1.closed_at),
            TicketEvent(
                ticket_id=t2.id, type="taken", staff_id=tima.id, created_at=t2.first_taken_at
            ),
            TicketEvent(
                ticket_id=old.id, type="taken", staff_id=tima.id, created_at=old.first_taken_at
            ),
        ]
    )
    await s.flush()
    return t1, t2, t3, old


def test_period_bounds_follow_local_calendar(clock_):
    now = clock_.now  # среда 07.10.2026 14:00 по Алматы
    assert stats.period_bounds("today", now) == (L(2026, 10, 7), None)
    assert stats.period_bounds("week", now) == (L(2026, 10, 5), None)
    assert stats.period_bounds("month", now) == (L(2026, 10, 1), None)
    assert stats.period_bounds("prev_month", now) == (L(2026, 9, 1), L(2026, 10, 1))
    assert stats.period_bounds("all", now) == (None, None)


async def test_compute_today(s, clock_):
    await _dataset(s, clock_.now)
    st = await stats.compute(s, *stats.period_bounds("today", clock_.now))
    assert (st.created, st.closed, st.in_progress, st.queued) == (3, 1, 1, 1)
    assert st.avg_reaction_min == 10.0 and st.avg_resolve_min == 60.0
    assert st.per_staff == [
        stats.StaffStat("Ерлан", 1, 1, 60.0),
        stats.StaffStat("Тимур", 1, 0, None),
    ]
    assert st.top_locations == [("Бухгалтерия, каб. 214", 2), ("Склад", 1)]
    text = stats.render_stats(st, "сегодня")
    assert text.splitlines()[:4] == [
        "📊 Статистика: сегодня",
        "Создано: 3 · Закрыто: 1",
        "Сейчас в работе: 1 · в очереди: 1",
        "Ср. время реакции: 10 мин · решения: 60 мин",
    ]
    assert "• Тимур — взял 1, закрыл 0, ср. —" in text


async def test_compute_all_time(s, clock_):
    await _dataset(s, clock_.now)
    st = await stats.compute(s, None, None)
    assert (st.created, st.closed) == (4, 2)


def test_parse_ticket_id():
    assert stats.parse_ticket_id("#12") == 12 and stats.parse_ticket_id(" 125 ") == 125
    assert stats.parse_ticket_id("87011234567") is None
    assert stats.parse_ticket_id("Айгерим") is None


async def test_search(s, clock_):
    t1, t2, t3, old = await _dataset(s, clock_.now)
    assert [t.id for t, _, _ in await stats.search(s, "айгер")] == [old.id, t1.id]
    assert [t.id for t, _, _ in await stats.search(s, "склад")] == [t2.id]
    rows = await stats.search(s, "+7 701 765")
    assert [(t.id, name) for t, _, name in rows] == [(t2.id, "Тимур")]
    assert stats.search_line(*rows[0]) == f"#{t2.id} · 07.10 · Бауыржан Ахметов · в работе · Тимур"


async def test_ticket_details_shows_progress(s, clock_):
    t1, *_ = await _dataset(s, clock_.now)
    text = await stats.ticket_details(s, t1.id)
    assert text.splitlines()[0] == f"📄 Заявка #{t1.id} — закрыта · Ерлан"
    assert "07.10 12:00 создана" in text
    assert "07.10 12:10 взял Ерлан" in text and "07.10 13:00 закрыл Ерлан" in text
    assert await stats.ticket_details(s, 999) is None


async def test_conversation_lists_files(s, clock_):
    _, t2, *_ = await _dataset(s, clock_.now)
    s.add(
        Message(
            client_id=t2.client_id,
            ticket_id=t2.id,
            direction="in",
            kind="image",
            text="скрин",
            file_path="/f/a.jpg",
        )
    )
    await s.flush()
    conv = await stats.conversation(s, t2.id)
    assert conv.chunks[0].startswith(f"💬 Переписка по заявке #{t2.id}")
    assert "Нет интернета" in conv.chunks[0]
    assert conv.files == [("/f/a.jpg", "photo", "скрин")]


async def test_open_tickets_text(s, clock_):
    _, t2, t3, _ = await _dataset(s, clock_.now)
    text = await stats.open_tickets_text(s)
    assert f"#{t2.id} · Бауыржан Ахметов · Тимур · 30 мин" in text
    assert f"#{t3.id} · Сауле · в очереди · 5 мин" in text


async def test_export_xlsx(s, clock_):
    t1, *_ = await _dataset(s, clock_.now)
    data = await export.export_xlsx(s, *stats.period_bounds("today", clock_.now))
    ws = load_workbook(BytesIO(data)).active
    rows = list(ws.iter_rows(values_only=True))
    assert list(rows[0]) == export.HEADERS
    assert len(rows) == 4
    assert list(rows[1]) == [
        t1.id,
        datetime(2026, 10, 7, 12, 0),
        "Айгерим Сериккызы",
        "Бухгалтерия, каб. 214",
        "+77011234567",
        "Не печатает принтер",
        "Ерлан",
        "закрыта",
        datetime(2026, 10, 7, 12, 10),
        datetime(2026, 10, 7, 13, 0),
        10,
        60,
    ]
```

- [ ] **Step 2: Запустить, убедиться, что падает**

Run: `make test`
Expected: FAIL — `ModuleNotFoundError: helpdesk.stats`.

- [ ] **Step 3: `src/helpdesk/stats/service.py`** (и пустой `stats/__init__.py`)

```python
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select

from helpdesk import clock
from helpdesk.db import queries
from helpdesk.db.models import Client, Ticket, TicketEvent
from helpdesk.tickets import render
from helpdesk.tickets.service import TG_FILE_KIND, ticket_messages

PERIOD_TITLES = {
    "today": "сегодня",
    "week": "эта неделя",
    "month": "этот месяц",
    "prev_month": "прошлый месяц",
    "all": "всё время",
}
STATUS_RU = {"new": "в очереди", "in_progress": "в работе", "closed": "закрыта"}
EVENT_RU = {
    "created": "создана",
    "taken": "взял",
    "returned": "вернул в очередь",
    "closed": "закрыл",
    "delivery_failed": "⚠️ сбой доставки",
}


def period_bounds(kind: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    if kind == "all":
        return None, None
    day = clock.local(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "today":
        return day, None
    if kind == "week":
        return day - timedelta(days=day.weekday()), None
    month = day.replace(day=1)
    if kind == "month":
        return month, None
    if kind == "prev_month":
        return (month - timedelta(days=1)).replace(day=1), month
    raise ValueError(kind)


def between(col, start, end) -> list:
    conds = []
    if start is not None:
        conds.append(col >= start)
    if end is not None:
        conds.append(col < end)
    return conds


def _minutes(a, b):
    return func.extract("epoch", a - b) / 60


def _f(x) -> float | None:
    return float(x) if x is not None else None


def _name(c: Client) -> str:
    return c.full_name or c.wa_name or "+" + c.phone


@dataclass
class StaffStat:
    name: str
    taken: int
    closed: int
    avg_resolve_min: float | None


@dataclass
class Stats:
    created: int
    closed: int
    in_progress: int
    queued: int
    avg_reaction_min: float | None
    avg_resolve_min: float | None
    per_staff: list[StaffStat]
    top_locations: list[tuple[str, int]]


async def _count(s, *conds) -> int:
    return await s.scalar(select(func.count()).select_from(Ticket).where(*conds))


async def compute(s, start, end) -> Stats:
    created_in = between(Ticket.created_at, start, end)
    closed_in = [Ticket.status == "closed", *between(Ticket.closed_at, start, end)]
    avg_reaction = await s.scalar(
        select(func.avg(_minutes(Ticket.first_taken_at, Ticket.created_at))).where(
            Ticket.first_taken_at.is_not(None), *created_in
        )
    )
    avg_resolve = await s.scalar(
        select(func.avg(_minutes(Ticket.closed_at, Ticket.created_at))).where(*closed_in)
    )
    names = await queries.staff_names(s)
    taken_q = (
        select(TicketEvent.staff_id, func.count())
        .where(TicketEvent.type == "taken", *between(TicketEvent.created_at, start, end))
        .group_by(TicketEvent.staff_id)
    )
    taken = dict((await s.execute(taken_q)).all())
    closed_q = (
        select(
            Ticket.closed_by_id,
            func.count(),
            func.avg(_minutes(Ticket.closed_at, Ticket.created_at)),
        )
        .where(*closed_in)
        .group_by(Ticket.closed_by_id)
    )
    closed_by = {sid: (n, _f(avg)) for sid, n, avg in (await s.execute(closed_q)).all()}
    ids = [i for i in set(taken) | set(closed_by) if i is not None]
    ids.sort(key=lambda i: (-closed_by.get(i, (0, None))[0], -taken.get(i, 0), i))
    per_staff = [
        StaffStat(
            names.get(i, "?"),
            taken.get(i, 0),
            closed_by.get(i, (0, None))[0],
            closed_by.get(i, (0, None))[1],
        )
        for i in ids
    ]
    loc_q = (
        select(Client.location, func.count(Ticket.id))
        .select_from(Ticket)
        .join(Client, Client.id == Ticket.client_id)
        .where(*created_in)
        .group_by(Client.location)
        .order_by(func.count(Ticket.id).desc(), Client.location)
        .limit(5)
    )
    top = [(loc or "—", n) for loc, n in (await s.execute(loc_q)).all()]
    return Stats(
        created=await _count(s, *created_in),
        closed=await _count(s, *closed_in),
        in_progress=await _count(s, Ticket.status == "in_progress"),
        queued=await _count(s, Ticket.status == "new"),
        avg_reaction_min=_f(avg_reaction),
        avg_resolve_min=_f(avg_resolve),
        per_staff=per_staff,
        top_locations=top,
    )


def fmt_min(x: float | None) -> str:
    return "—" if x is None else f"{round(x)} мин"


def render_stats(st: Stats, title: str) -> str:
    lines = [
        f"📊 Статистика: {title}",
        f"Создано: {st.created} · Закрыто: {st.closed}",
        f"Сейчас в работе: {st.in_progress} · в очереди: {st.queued}",
        f"Ср. время реакции: {fmt_min(st.avg_reaction_min)} · решения: {fmt_min(st.avg_resolve_min)}",
    ]
    if st.per_staff:
        lines += ["", "По специалистам:"]
        lines += [
            f"• {p.name} — взял {p.taken}, закрыл {p.closed}, ср. {fmt_min(p.avg_resolve_min)}"
            for p in st.per_staff
        ]
    if st.top_locations:
        lines += ["", "Топ отделов:"] + [f"• {loc} — {n}" for loc, n in st.top_locations]
    return "\n".join(lines)


def parse_ticket_id(q: str) -> int | None:
    q = q.strip().lstrip("#")
    return int(q) if q.isdigit() and len(q) <= 6 else None


async def search(s, q: str, limit: int = 10) -> list[tuple[Ticket, Client, str | None]]:
    like = f"%{q.strip()}%"
    conds = [Client.full_name.ilike(like), Client.location.ilike(like), Client.wa_name.ilike(like)]
    digits = "".join(ch for ch in q if ch.isdigit())
    if len(digits) >= 4:
        conds.append(Client.phone.like(f"%{digits}%"))
    query = (
        select(Ticket, Client)
        .join(Client, Client.id == Ticket.client_id)
        .where(or_(*conds))
        .order_by(Ticket.id.desc())
        .limit(limit)
    )
    names = await queries.staff_names(s)
    return [(t, c, names.get(t.assignee_id)) for t, c in (await s.execute(query)).all()]


def search_line(t: Ticket, c: Client, assignee: str | None) -> str:
    who = f" · {assignee}" if assignee else ""
    return f"#{t.id} · {clock.local(t.created_at):%d.%m} · {_name(c)} · {STATUS_RU[t.status]}{who}"


async def ticket_details(s, ticket_id: int) -> str | None:
    t = await s.get(Ticket, ticket_id)
    if t is None:
        return None
    c = await s.get(Client, t.client_id)
    names = await queries.staff_names(s)
    head = f"📄 Заявка #{t.id} — {STATUS_RU[t.status]}"
    if t.assignee_id:
        head += f" · {names.get(t.assignee_id, '?')}"
    lines = [head, render.client_line(c), "", "Ход исполнения:"]
    q = select(TicketEvent).where(TicketEvent.ticket_id == t.id).order_by(TicketEvent.id)
    for e in (await s.scalars(q)).all():
        who = f" {names.get(e.staff_id, '?')}" if e.staff_id else ""
        lines.append(f"{clock.local(e.created_at):%d.%m %H:%M} {EVENT_RU.get(e.type, e.type)}{who}")
    return "\n".join(lines)


@dataclass
class Conversation:
    chunks: list[str]
    files: list[tuple[str, str, str | None]]


async def conversation(s, ticket_id: int) -> Conversation | None:
    t = await s.get(Ticket, ticket_id)
    if t is None:
        return None
    msgs = await ticket_messages(s, t.id)
    body = render.history(msgs, await queries.staff_names(s)) or "(пусто)"
    text = f"💬 Переписка по заявке #{t.id}\n\n{body}"
    chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
    files = [(m.file_path, TG_FILE_KIND[m.kind], m.text) for m in msgs if m.file_path]
    return Conversation(chunks, files)


async def open_tickets_text(s) -> str:
    q = (
        select(Ticket, Client)
        .join(Client, Client.id == Ticket.client_id)
        .where(Ticket.status != "closed")
        .order_by(Ticket.id)
    )
    rows = (await s.execute(q)).all()
    if not rows:
        return "Открытых заявок нет 🎉"
    names = await queries.staff_names(s)
    now = clock.utcnow()
    lines = ["Открытые заявки:"]
    for t, c in rows:
        mins = int((now - t.created_at).total_seconds() // 60)
        who = names.get(t.assignee_id, "?") if t.assignee_id else "в очереди"
        lines.append(f"#{t.id} · {_name(c)} · {who} · {mins} мин")
    return "\n".join(lines)[:4000]
```

- [ ] **Step 4: `src/helpdesk/stats/export.py`**

```python
from datetime import datetime
from io import BytesIO

from openpyxl import Workbook
from sqlalchemy import select

from helpdesk import clock
from helpdesk.db import queries
from helpdesk.db.models import Client, Message, Ticket
from helpdesk.stats.service import STATUS_RU, between

HEADERS = [
    "№",
    "Создана",
    "ФИО",
    "Отдел/кабинет",
    "Телефон",
    "Первое сообщение",
    "Исполнитель",
    "Статус",
    "Взята",
    "Закрыта",
    "Реакция, мин",
    "Решение, мин",
]
WIDTHS = [6, 17, 28, 24, 15, 50, 16, 11, 17, 17, 13, 13]


def _naive(dt: datetime | None) -> datetime | None:
    return clock.local(dt).replace(tzinfo=None) if dt else None


def _minutes(a: datetime | None, b: datetime) -> int | None:
    return round((a - b).total_seconds() / 60) if a else None


async def export_xlsx(s, start, end) -> bytes:
    q = (
        select(Ticket, Client)
        .join(Client, Client.id == Ticket.client_id)
        .where(*between(Ticket.created_at, start, end))
        .order_by(Ticket.id)
    )
    rows = (await s.execute(q)).all()
    ids = [t.id for t, _ in rows]
    first: dict[int, str] = {}
    if ids:
        fq = (
            select(Message.ticket_id, Message.text, Message.kind)
            .where(Message.ticket_id.in_(ids), Message.direction == "in")
            .order_by(Message.ticket_id, Message.id)
            .distinct(Message.ticket_id)
        )
        first = {tid: text or f"[{kind}]" for tid, text, kind in (await s.execute(fq)).all()}
    names = await queries.staff_names(s)

    wb = Workbook()
    ws = wb.active
    ws.title = "Заявки"
    ws.append(HEADERS)
    for t, c in rows:
        ws.append(
            [
                t.id,
                _naive(t.created_at),
                c.full_name or c.wa_name,
                c.location,
                "+" + c.phone,
                first.get(t.id),
                names.get(t.assignee_id or t.closed_by_id),
                STATUS_RU[t.status],
                _naive(t.first_taken_at),
                _naive(t.closed_at),
                _minutes(t.first_taken_at, t.created_at),
                _minutes(t.closed_at, t.created_at),
            ]
        )
    for i, width in enumerate(WIDTHS):
        ws.column_dimensions[chr(ord("A") + i)].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, datetime):
                cell.number_format = "DD.MM.YYYY HH:MM"
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
```

- [ ] **Step 5: Тесты зелёные**

Run: `make test`
Expected: все PASS. Если `test_search` по «айгер» ничего не находит, проверьте, что тестовая база создана с UTF-8 локалью (образ `postgres:16` по умолчанию `en_US.utf8`): иначе `ILIKE` не сравнивает кириллицу без учёта регистра.

- [ ] **Step 6: Commit**

```bash
uv run ruff check --fix src tests && uv run ruff format src tests
git add -A
git commit -m "feat(stats): period stats, search, ticket timeline, xlsx export

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Telegram-интерфейс

**Files:**
- Create: `src/helpdesk/telegram/__init__.py`, `src/helpdesk/telegram/gateway.py`, `src/helpdesk/telegram/media.py`, `src/helpdesk/telegram/parsing.py`, `src/helpdesk/telegram/middleware.py`, `src/helpdesk/telegram/handlers.py`
- Test: `tests/test_telegram.py`

**Interfaces:**
- Consumes: `tickets.*`, `staff_svc.*`, `stats.*`, `export.export_xlsx`, `render.client_line`, `files.new_file_path`, `Settings`.
- Produces:
  - `gateway.markup(buttons) -> InlineKeyboardMarkup | None`; `gateway.AiogramSender(bot)` реализует `worker.TgSender`
  - `media.Content(kind, text, file_path, file_name)`, `media.FileTooBig`, `media.MAX_BYTES = 20 * 1024 * 1024`, `await media.extract_content(bot, message, files_dir) -> Content`
  - `parsing.parse_client_args(args: str | None) -> tuple[int, str, str] | None` (второй элемент — имя поля модели: `full_name` или `location`)
  - `middleware.DbMiddleware(sf)` кладёт в данные хендлера `s` (сессия с открытой транзакцией) и `staff` (`Staff | None`)
  - `handlers.router`, `handlers.BOT_COMMANDS: list[tuple[str, str]]`, `handlers.setup(dp, sf) -> None`

- [ ] **Step 1: Падающие тесты `tests/test_telegram.py`**

```python
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram import Dispatcher
from aiogram.types import InlineKeyboardMarkup

from helpdesk.telegram import handlers
from helpdesk.telegram.gateway import markup
from helpdesk.telegram.media import MAX_BYTES, FileTooBig, extract_content
from helpdesk.telegram.parsing import parse_client_args


def test_parse_client_args():
    assert parse_client_args("125 name Иванов Иван") == (125, "full_name", "Иванов Иван")
    assert parse_client_args("#7 location Склад, каб. 3") == (7, "location", "Склад, каб. 3")
    assert parse_client_args(None) is None
    assert parse_client_args("125 phone 123") is None
    assert parse_client_args("abc name X") is None
    assert parse_client_args("125 name") is None
    assert parse_client_args("125 name " + "x" * 201) is None


def test_markup():
    assert markup(None) is None
    kb = markup([[("✋ Взять", "take:1")], [("A", "a"), ("B", "b")]])
    assert isinstance(kb, InlineKeyboardMarkup)
    assert [[b.callback_data for b in row] for row in kb.inline_keyboard] == [["take:1"], ["a", "b"]]
    # формат из payload outbox (списки вместо кортежей) тоже принимается
    assert markup([[["x", "y"]]]).inline_keyboard[0][0].text == "x"


def _msg(**kw):
    base = dict(text=None, caption=None, photo=None, document=None, voice=None, audio=None,
                video=None, video_note=None)
    base.update(kw)
    return SimpleNamespace(**base)


class FakeBot:
    def __init__(self):
        self.downloaded = []

    async def download(self, obj, destination):
        Path(destination).write_bytes(b"data")
        self.downloaded.append(obj)


async def test_extract_text(tmp_path, clock_):
    c = await extract_content(FakeBot(), _msg(text="Иду"), str(tmp_path))
    assert (c.kind, c.text, c.file_path, c.file_name) == ("text", "Иду", None, None)


async def test_extract_photo_takes_largest_size(tmp_path, clock_):
    small = SimpleNamespace(file_unique_id="s", file_size=10)
    big = SimpleNamespace(file_unique_id="b", file_size=1000)
    bot = FakeBot()
    c = await extract_content(bot, _msg(photo=[small, big], caption="скрин"), str(tmp_path))
    assert (c.kind, c.text, c.file_name) == ("image", "скрин", "photo_b.jpg")
    assert bot.downloaded == [big] and Path(c.file_path).read_bytes() == b"data"


async def test_extract_voice_and_document(tmp_path, clock_):
    v = await extract_content(FakeBot(), _msg(voice=SimpleNamespace(file_size=5)), str(tmp_path))
    assert (v.kind, v.file_name) == ("audio", "voice.ogg")
    doc = SimpleNamespace(file_size=5, file_name="акт.pdf")
    d = await extract_content(FakeBot(), _msg(document=doc), str(tmp_path))
    assert (d.kind, d.file_name) == ("document", "акт.pdf")


async def test_extract_too_big(tmp_path, clock_):
    doc = SimpleNamespace(file_size=MAX_BYTES + 1, file_name="big.zip")
    with pytest.raises(FileTooBig):
        await extract_content(FakeBot(), _msg(document=doc), str(tmp_path))


def test_router_setup_registers_handlers():
    dp = Dispatcher()
    handlers.setup(dp, sf=None)
    assert len(handlers.router.message.handlers) >= 14
    assert len(handlers.router.callback_query.handlers) >= 10
    assert ("my", "Мои заявки в работе") in handlers.BOT_COMMANDS
```

- [ ] **Step 2: Запустить, убедиться, что падает**

Run: `make test`
Expected: FAIL — `ModuleNotFoundError: helpdesk.telegram`.

- [ ] **Step 3: `src/helpdesk/telegram/gateway.py`** (и пустой `telegram/__init__.py`)

```python
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup


def markup(buttons) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in buttons
        ]
    )


class AiogramSender:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def send(self, chat_id, *, text, file_path, file_kind, buttons) -> int:
        kb = markup(buttons)
        if not file_path:
            m = await self.bot.send_message(chat_id, (text or "")[:4096], reply_markup=kb)
            return m.message_id
        caption = text[:1024] if text else None
        if file_kind == "photo":
            m = await self.bot.send_photo(chat_id, FSInputFile(file_path), caption=caption, reply_markup=kb)
        elif file_kind == "video":
            m = await self.bot.send_video(chat_id, FSInputFile(file_path), caption=caption, reply_markup=kb)
        elif file_kind == "voice":
            try:
                m = await self.bot.send_voice(
                    chat_id, FSInputFile(file_path), caption=caption, reply_markup=kb
                )
            except TelegramBadRequest:  # не OGG/MP3/M4A — отправим файлом
                m = await self.bot.send_document(
                    chat_id, FSInputFile(file_path), caption=caption, reply_markup=kb
                )
        else:
            m = await self.bot.send_document(
                chat_id, FSInputFile(file_path), caption=caption, reply_markup=kb
            )
        return m.message_id

    async def edit(self, chat_id, message_id, text, buttons) -> None:
        try:
            await self.bot.edit_message_text(
                text=text[:4096], chat_id=chat_id, message_id=message_id, reply_markup=markup(buttons)
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise

    async def delete(self, chat_id, message_id) -> None:
        await self.bot.delete_message(chat_id, message_id)
```

- [ ] **Step 4: `src/helpdesk/telegram/media.py`**

```python
from dataclasses import dataclass

from helpdesk import clock, files

MAX_BYTES = 20 * 1024 * 1024


class FileTooBig(Exception):
    pass


@dataclass
class Content:
    kind: str
    text: str | None
    file_path: str | None
    file_name: str | None


def _pick(message):
    if message.photo:
        p = message.photo[-1]
        return "image", p, f"photo_{p.file_unique_id}.jpg"
    if message.document:
        return "document", message.document, message.document.file_name or "file"
    if message.voice:
        return "audio", message.voice, "voice.ogg"
    if message.audio:
        return "audio", message.audio, message.audio.file_name or "audio.mp3"
    if message.video:
        return "video", message.video, message.video.file_name or "video.mp4"
    if message.video_note:
        return "video", message.video_note, "video_note.mp4"
    return None


async def extract_content(bot, message, files_dir: str) -> Content:
    if message.text is not None:
        return Content("text", message.text, None, None)
    picked = _pick(message)
    if picked is None:
        raise ValueError("unsupported content")
    kind, obj, name = picked
    if (obj.file_size or 0) > MAX_BYTES:
        raise FileTooBig(name)
    path = files.new_file_path(files_dir, name, clock.utcnow())
    await bot.download(obj, destination=path)
    return Content(kind, message.caption, str(path), name)
```

- [ ] **Step 5: `src/helpdesk/telegram/parsing.py`**

```python
_FIELDS = {"name": "full_name", "location": "location"}


def parse_client_args(args: str | None) -> tuple[int, str, str] | None:
    if not args:
        return None
    parts = args.split(maxsplit=2)
    if len(parts) != 3:
        return None
    num, field, value = parts[0].lstrip("#"), parts[1].lower(), parts[2].strip()
    if not num.isdigit() or field not in _FIELDS or not 1 <= len(value) <= 200:
        return None
    return int(num), _FIELDS[field], value
```

- [ ] **Step 6: `src/helpdesk/telegram/middleware.py`**

```python
from aiogram import BaseMiddleware
from sqlalchemy import select

from helpdesk.db.models import Staff


class DbMiddleware(BaseMiddleware):
    def __init__(self, sf):
        self.sf = sf

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        async with self.sf() as s, s.begin():
            data["s"] = s
            data["staff"] = (
                await s.scalar(select(Staff).where(Staff.tg_user_id == user.id)) if user else None
            )
            return await handler(event, data)
```

- [ ] **Step 7: `src/helpdesk/telegram/handlers.py`**

```python
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, Filter
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from helpdesk import clock
from helpdesk.config import Settings
from helpdesk.db.models import Client, Staff, Ticket
from helpdesk.staff import service as staff_svc
from helpdesk.stats import export
from helpdesk.stats import service as stats
from helpdesk.telegram.gateway import AiogramSender, markup
from helpdesk.telegram.media import FileTooBig, extract_content
from helpdesk.telegram.middleware import DbMiddleware
from helpdesk.telegram.parsing import parse_client_args
from helpdesk.tickets import render
from helpdesk.tickets import service as tickets

router = Router()

BOT_COMMANDS = [
    ("my", "Мои заявки в работе"),
    ("open", "Все открытые заявки"),
    ("find", "Поиск заявки"),
    ("stats", "Статистика"),
    ("export", "Выгрузка в Excel"),
    ("on", "Я на смене"),
    ("off", "Я не на смене"),
    ("help", "Помощь"),
]

HELP = (
    "Команды:\n"
    "/my — мои заявки в работе\n"
    "/open — все незакрытые заявки\n"
    "/find <№ | ФИО | кабинет | телефон> — поиск\n"
    "/stats — статистика\n"
    "/export — выгрузка в Excel\n"
    "/on, /off — на смене / не на смене\n"
    "/client <№> name|location <значение> — исправить данные пользователя\n"
    "/staff — специалисты (только админ)\n\n"
    "Ответ пользователю: просто напишите сообщение (текст, фото, файл, голосовое) — "
    "оно уйдёт в активную заявку. Чтобы ответить в другую заявку, нажмите «Ответить» "
    "на её сообщение или выберите её в /my."
)


class IsActive(Filter):
    async def __call__(self, event, staff: Staff | None = None) -> bool:
        return staff is not None and staff.status == "active"


class IsAdmin(Filter):
    async def __call__(self, event, staff: Staff | None = None) -> bool:
        return staff is not None and staff.status == "active" and staff.is_admin


def _arg_id(cq: CallbackQuery) -> int:
    return int(cq.data.split(":", 1)[1])


def _cname(c: Client) -> str:
    return c.full_name or c.wa_name or "+" + c.phone


# ---------- доступ ----------


@router.message(CommandStart())
async def cmd_start(message: Message, s, settings: Settings):
    u = message.from_user
    st = await staff_svc.register(
        s,
        tg_user_id=u.id,
        chat_id=message.chat.id,
        name=u.full_name,
        username=u.username,
        admin_ids=settings.admin_ids,
    )
    if st.status == "active":
        await message.answer(f"Вы в системе, {st.name}.\n\n{HELP}")
    elif st.status == "pending":
        await message.answer("Запрос на доступ отправлен администратору. Ожидайте.")
    else:
        await message.answer("Доступ закрыт.")


@router.message(Command("help"), IsActive())
async def cmd_help(message: Message):
    await message.answer(HELP)


@router.message(Command("on"), IsActive())
async def cmd_on(message: Message, s, staff: Staff):
    n = await staff_svc.set_shift(s, staff, True)
    await message.answer("Вы уже на смене." if n is None else f"Вы на смене. Заявок в очереди: {n}.")


@router.message(Command("off"), IsActive())
async def cmd_off(message: Message, s, staff: Staff):
    n = await staff_svc.set_shift(s, staff, False)
    await message.answer(
        "Вы уже не на смене."
        if n is None
        else "Вы не на смене: новые заявки приходить не будут. Заявки в работе остаются за вами."
    )


# ---------- работа с заявками ----------


@router.message(Command("my"), IsActive())
async def cmd_my(message: Message, s, staff: Staff):
    rows = await tickets.my_tickets(s, staff)
    if not rows:
        await message.answer("У вас нет заявок в работе.")
        return
    lines = [
        f"{'▶️ ' if t.id == staff.active_ticket_id else ''}#{t.id} · {_cname(c)}" for t, c in rows
    ]
    buttons = [[(f"Перейти к #{t.id}", f"goto:{t.id}")] for t, _ in rows]
    await message.answer("Ваши заявки в работе:\n" + "\n".join(lines), reply_markup=markup(buttons))


@router.callback_query(F.data.startswith("goto:"), IsActive())
async def cb_goto(cq: CallbackQuery, s, staff: Staff):
    t = await tickets.set_active(s, staff, _arg_id(cq))
    if t is None:
        await cq.answer("Заявка не у вас или закрыта", show_alert=True)
        return
    sent = await cq.message.answer(await tickets.recent_view(s, t))
    await tickets.link(s, staff.tg_chat_id, sent.message_id, t.id)
    await cq.answer(f"Активная: #{t.id}")


@router.callback_query(F.data.startswith("take:"), IsActive())
async def cb_take(cq: CallbackQuery, s, staff: Staff):
    r = await tickets.take(s, _arg_id(cq), staff)
    if r.ok:
        await cq.answer("Заявка ваша ✅")
    else:
        text = f"Уже взял {r.holder_name}" if r.holder_name else "Заявка уже недоступна"
        await cq.answer(text, show_alert=True)


async def _drop_buttons(cq: CallbackQuery) -> None:
    try:
        await cq.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("close:"), IsActive())
async def cb_close(cq: CallbackQuery, s, staff: Staff):
    tid = _arg_id(cq)
    if not await tickets.close_ticket(s, tid, staff):
        await cq.answer("Заявка уже не у вас или закрыта", show_alert=True)
        return
    await _drop_buttons(cq)
    await cq.message.answer(f"✔️ Заявка #{tid} закрыта.")
    await cq.answer()


@router.callback_query(F.data.startswith("ret:"), IsActive())
async def cb_return(cq: CallbackQuery, s, staff: Staff):
    tid = _arg_id(cq)
    if not await tickets.return_ticket(s, tid, staff):
        await cq.answer("Заявка уже не у вас или закрыта", show_alert=True)
        return
    await _drop_buttons(cq)
    await cq.message.answer(f"↩️ Заявка #{tid} возвращена в очередь.")
    await cq.answer()


# ---------- поиск, статистика, выгрузка ----------


@router.message(Command("open"), IsActive())
async def cmd_open(message: Message, s):
    await message.answer(await stats.open_tickets_text(s))


async def _send_details(message: Message, s, staff: Staff, tid: int) -> None:
    text = await stats.ticket_details(s, tid)
    if text is None:
        await message.answer("Заявка не найдена.")
        return
    t = await s.get(Ticket, tid)
    buttons = [[("💬 Показать переписку", f"chat:{tid}")]]
    if t.status == "new":
        buttons += tickets.QUEUE_BUTTONS(tid)
    if t.status == "in_progress" and (staff.is_admin or t.assignee_id == staff.id):
        buttons += tickets.WORK_BUTTONS(tid)
    sent = await message.answer(text, reply_markup=markup(buttons))
    if t.status == "in_progress" and t.assignee_id == staff.id:
        await tickets.link(s, staff.tg_chat_id, sent.message_id, t.id)


@router.message(Command("find"), IsActive())
async def cmd_find(message: Message, command: CommandObject, s, staff: Staff):
    q = (command.args or "").strip()
    if not q:
        await message.answer("Использование: /find 125 | ФИО | кабинет | телефон")
        return
    tid = stats.parse_ticket_id(q)
    if tid is not None:
        await _send_details(message, s, staff, tid)
        return
    rows = await stats.search(s, q)
    if not rows:
        await message.answer("Ничего не найдено.")
        return
    lines = [stats.search_line(t, c, name) for t, c, name in rows]
    await message.answer("\n".join(lines) + "\n\nПодробнее: /find <номер>")


@router.callback_query(F.data.startswith("chat:"), IsActive())
async def cb_chat(cq: CallbackQuery, s, bot: Bot):
    conv = await stats.conversation(s, _arg_id(cq))
    if conv is None:
        await cq.answer("Заявка не найдена", show_alert=True)
        return
    await cq.answer()
    for chunk in conv.chunks:
        await cq.message.answer(chunk)
    sender = AiogramSender(bot)
    for path, kind, caption in conv.files:
        if os.path.exists(path):
            await sender.send(
                cq.message.chat.id, text=caption, file_path=path, file_kind=kind, buttons=None
            )
        else:
            await cq.message.answer(f"[файл не найден: {os.path.basename(path)}]")


@router.message(Command("stats"), IsActive())
async def cmd_stats(message: Message):
    kb = markup([[("Сегодня", "stats:today"), ("Неделя", "stats:week"), ("Месяц", "stats:month")]])
    await message.answer("Статистика за период:", reply_markup=kb)


@router.callback_query(F.data.in_({"stats:today", "stats:week", "stats:month"}), IsActive())
async def cb_stats(cq: CallbackQuery, s):
    kind = cq.data.split(":", 1)[1]
    start, end = stats.period_bounds(kind, clock.utcnow())
    result = await stats.compute(s, start, end)
    await cq.message.answer(stats.render_stats(result, stats.PERIOD_TITLES[kind]))
    await cq.answer()


@router.message(Command("export"), IsActive())
async def cmd_export(message: Message):
    kb = markup(
        [
            [("Этот месяц", "export:month"), ("Прошлый месяц", "export:prev_month")],
            [("Всё время", "export:all")],
        ]
    )
    await message.answer("Выгрузка заявок в Excel:", reply_markup=kb)


@router.callback_query(
    F.data.in_({"export:month", "export:prev_month", "export:all"}), IsActive()
)
async def cb_export(cq: CallbackQuery, s):
    kind = cq.data.split(":", 1)[1]
    start, end = stats.period_bounds(kind, clock.utcnow())
    data = await export.export_xlsx(s, start, end)
    name = f"zayavki_{kind}_{clock.local(clock.utcnow()):%Y-%m-%d}.xlsx"
    await cq.message.answer_document(
        BufferedInputFile(data, filename=name), caption=f"Заявки: {stats.PERIOD_TITLES[kind]}"
    )
    await cq.answer()


@router.message(Command("client"), IsActive())
async def cmd_client(message: Message, command: CommandObject, s):
    parsed = parse_client_args(command.args)
    if parsed is None:
        await message.answer("Использование: /client <№ заявки> name|location <значение>")
        return
    tid, field, value = parsed
    t = await s.get(Ticket, tid)
    if t is None:
        await message.answer("Заявка не найдена.")
        return
    c = await s.get(Client, t.client_id)
    setattr(c, field, value)
    await message.answer(f"Сохранено (заявка #{tid}):\n{render.client_line(c)}")


# ---------- администрирование ----------


@router.message(Command("staff"), IsAdmin())
async def cmd_staff(message: Message, s, staff: Staff):
    for st in await staff_svc.list_staff(s):
        await message.answer(
            staff_svc.staff_line(st), reply_markup=markup(staff_svc.staff_buttons(st, staff))
        )


@router.callback_query(F.data.startswith("approve:"), IsAdmin())
async def cb_approve(cq: CallbackQuery, s, staff: Staff):
    st = await staff_svc.approve(s, staff, _arg_id(cq))
    if st is None:
        await cq.answer("Уже обработано", show_alert=True)
        return
    await cq.message.edit_text(f"✅ {st.name} — доступ открыт")
    await cq.answer()


@router.callback_query(F.data.startswith("reject:"), IsAdmin())
async def cb_reject(cq: CallbackQuery, s, staff: Staff):
    st = await staff_svc.reject(s, staff, _arg_id(cq))
    if st is None:
        await cq.answer("Уже обработано", show_alert=True)
        return
    await cq.message.edit_text(f"❌ {st.name} — доступ отклонён")
    await cq.answer()


_STAFF_ACTIONS = {
    "stf_dis": staff_svc.disable,
    "stf_en": staff_svc.enable,
    "stf_adm": staff_svc.toggle_admin,
}


@router.callback_query(F.data.regexp(r"^stf_(dis|en|adm):\d+$"), IsAdmin())
async def cb_staff_action(cq: CallbackQuery, s, staff: Staff):
    action, sid = cq.data.split(":")
    if not await _STAFF_ACTIONS[action](s, staff, int(sid)):
        await cq.answer("Это действие недоступно", show_alert=True)
        return
    st = await s.get(Staff, int(sid))
    await cq.message.edit_text(
        staff_svc.staff_line(st), reply_markup=markup(staff_svc.staff_buttons(st, staff))
    )
    await cq.answer("Готово")


# ---------- ответы пользователю ----------


async def _reply_to_client(message: Message, s, staff: Staff, bot: Bot, settings: Settings):
    reply_to = message.reply_to_message.message_id if message.reply_to_message else None
    t, err = await tickets.resolve_target(s, staff, reply_to)
    if t is None:
        await message.answer(err)
        return
    try:
        content = await extract_content(bot, message, settings.files_dir)
    except FileTooBig:
        await message.answer("Файл больше 20 МБ — бот не может его скачать. Отправьте ссылкой.")
        return
    await tickets.send_staff_reply(
        s, staff, t, content.kind, content.text, content.file_path, content.file_name
    )
    c = await s.get(Client, t.client_id)
    await message.answer(f"→ #{t.id} {_cname(c)} ✓")


@router.message(IsActive(), F.text, ~F.text.startswith("/"))
async def staff_text(message: Message, s, staff: Staff, bot: Bot, settings: Settings):
    await _reply_to_client(message, s, staff, bot, settings)


@router.message(IsActive(), F.photo | F.document | F.voice | F.audio | F.video | F.video_note)
async def staff_media(message: Message, s, staff: Staff, bot: Bot, settings: Settings):
    await _reply_to_client(message, s, staff, bot, settings)


# ---------- всё остальное ----------


@router.message()
async def fallback(message: Message, staff: Staff | None = None):
    if staff is None:
        await message.answer("Напишите /start, чтобы запросить доступ.")
    elif staff.status == "pending":
        await message.answer("Ожидайте одобрения администратора.")
    elif staff.status == "disabled":
        await message.answer("Доступ закрыт.")
    else:
        await message.answer("Не понял команду. /help — список команд.")


@router.callback_query()
async def cb_fallback(cq: CallbackQuery):
    await cq.answer("Нет доступа или действие устарело", show_alert=True)


def setup(dp: Dispatcher, sf) -> None:
    mw = DbMiddleware(sf)
    router.message.outer_middleware(mw)
    router.callback_query.outer_middleware(mw)
    dp.include_router(router)
```

- [ ] **Step 8: Тесты зелёные**

Run: `make test`
Expected: все PASS.

- [ ] **Step 9: Commit**

```bash
uv run ruff check --fix src tests && uv run ruff format src tests
git add -A
git commit -m "feat(telegram): staff interface — cards, replies, search, stats, admin

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Поллер WhatsApp, запуск, Docker, README

**Files:**
- Create: `src/helpdesk/whatsapp/poller.py`, `src/helpdesk/main.py`, `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `README.md`
- Modify: `tests/fakes.py` (добавить `FakeGreen`)
- Test: `tests/test_poller.py`

**Interfaces:**
- Consumes: `GreenApiClient` (receive/delete/download), `parse_notification`, `inbound.handle_incoming`, `worker.run`, `reminders.run`, `handlers.setup/BOT_COMMANDS`, `AiogramSender`, `Settings`.
- Produces: `await poller.poll_once(sf, green, files_dir, failures: dict[int, int], retry_sleep=1.0) -> bool` (False — очередь пуста), `await poller.run(sf, green, files_dir, stop)`, `poller.MAX_FAILURES = 3`; `helpdesk.main.main()`.

- [ ] **Step 1: Добавить в `tests/fakes.py`**

```python
from pathlib import Path


class FakeGreen:
    def __init__(self, items=()):
        self.items = list(items)  # [(receipt_id, body), ...]
        self.deleted: list[int] = []
        self.fail_download = False

    async def receive(self, timeout=20):
        return self.items[0] if self.items else None

    async def delete(self, receipt_id):
        self.deleted.append(receipt_id)
        self.items = [i for i in self.items if i[0] != receipt_id]

    async def download(self, url, dest):
        if self.fail_download:
            raise RuntimeError("link expired")
        Path(dest).write_bytes(b"img")
```

(`from pathlib import Path` поместить в начало файла.)

- [ ] **Step 2: Падающие тесты `tests/test_poller.py`**

```python
from pathlib import Path

from sqlalchemy import func, select

from helpdesk.db.models import Client, Message
from helpdesk.whatsapp import poller
from tests.factories import make_client
from tests.fakes import FakeGreen


def _note(mid, text="Привет", type_message="textMessage", **file):
    md = {"typeMessage": type_message}
    if type_message == "textMessage":
        md["textMessageData"] = {"textMessage": text}
    else:
        md["fileMessageData"] = {"caption": text, **file}
    return {
        "typeWebhook": "incomingMessageReceived",
        "idMessage": mid,
        "senderData": {"chatId": "77011234567@c.us", "senderName": "Aigerim"},
        "messageData": md,
    }


async def _count(sf, model):
    async with sf() as s:
        return await s.scalar(select(func.count()).select_from(model))


async def test_empty_queue(sf, tmp_path):
    assert await poller.poll_once(sf, FakeGreen(), str(tmp_path), {}) is False


async def test_poller_deletes_after_commit(sf, clock_, tmp_path):
    green = FakeGreen([(1, _note("M1")), (2, _note("M1"))])  # второе — повтор того же сообщения
    assert await poller.poll_once(sf, green, str(tmp_path), {})
    assert green.deleted == [1] and await _count(sf, Message) == 1
    assert await poller.poll_once(sf, green, str(tmp_path), {})
    assert green.deleted == [1, 2] and await _count(sf, Message) == 1


async def test_ignored_notification_is_deleted(sf, clock_, tmp_path):
    green = FakeGreen([(5, {"typeWebhook": "outgoingMessageReceived"})])
    await poller.poll_once(sf, green, str(tmp_path), {})
    assert green.deleted == [5] and await _count(sf, Client) == 0


async def test_failing_handler_is_retried_then_dropped(sf, clock_, tmp_path, monkeypatch):
    async def boom(s, msg, file_path):
        raise RuntimeError("db down")

    monkeypatch.setattr("helpdesk.inbound.handle_incoming", boom)
    green = FakeGreen([(9, _note("M9"))])
    failures: dict[int, int] = {}
    for _ in range(poller.MAX_FAILURES - 1):
        await poller.poll_once(sf, green, str(tmp_path), failures, retry_sleep=0)
        assert green.deleted == []
    await poller.poll_once(sf, green, str(tmp_path), failures, retry_sleep=0)
    assert green.deleted == [9] and failures == {}


async def test_download_success_stores_file(sf, clock_, tmp_path):
    async with sf() as s, s.begin():
        await make_client(s)
    note = _note("F1", "скрин", "imageMessage", downloadUrl="https://cdn/x", fileName="x.jpg")
    await poller.poll_once(sf, FakeGreen([(1, note)]), str(tmp_path), {})
    async with sf() as s:
        m = await s.scalar(select(Message))
        assert m.kind == "image" and Path(m.file_path).read_bytes() == b"img"
        assert str(tmp_path) in m.file_path


async def test_download_failure_marks_text(sf, clock_, tmp_path):
    async with sf() as s, s.begin():
        await make_client(s)
    note = _note("F2", "скрин", "imageMessage", downloadUrl="https://cdn/x", fileName="x.jpg")
    green = FakeGreen([(1, note)])
    green.fail_download = True
    await poller.poll_once(sf, green, str(tmp_path), {})
    async with sf() as s:
        m = await s.scalar(select(Message))
        assert (m.text, m.file_path) == ("скрин [файл не загружен]", None)
```

- [ ] **Step 3: Запустить, убедиться, что падает**

Run: `make test`
Expected: FAIL — `ImportError: cannot import name 'poller'`.

- [ ] **Step 4: `src/helpdesk/whatsapp/poller.py`**

```python
import asyncio
from dataclasses import replace

import structlog

from helpdesk import clock, files, inbound
from helpdesk.whatsapp.parse import parse_notification

log = structlog.get_logger()

MAX_FAILURES = 3


async def poll_once(sf, green, files_dir: str, failures: dict[int, int], retry_sleep=1.0) -> bool:
    got = await green.receive(timeout=20)
    if got is None:
        return False
    receipt_id, body = got
    msg = parse_notification(body)
    if msg is not None:
        try:
            file_path = None
            if msg.download_url:
                try:
                    path = files.new_file_path(files_dir, msg.file_name, clock.utcnow())
                    await green.download(msg.download_url, path)
                    file_path = str(path)
                except Exception as e:
                    log.warning("wa file download failed", error=str(e))
                    msg = replace(msg, text=f"{msg.text or ''} [файл не загружен]".strip())
            async with sf() as s, s.begin():
                await inbound.handle_incoming(s, msg, file_path)
        except Exception:
            n = failures[receipt_id] = failures.get(receipt_id, 0) + 1
            log.exception("inbound message failed", receipt_id=receipt_id, attempt=n)
            if n < MAX_FAILURES:
                await asyncio.sleep(retry_sleep)
                return True  # не удаляем — Green API отдаст уведомление снова
    await green.delete(receipt_id)
    failures.pop(receipt_id, None)
    return True


async def run(sf, green, files_dir: str, stop: asyncio.Event, error_sleep: float = 5.0) -> None:
    failures: dict[int, int] = {}
    while not stop.is_set():
        try:
            await poll_once(sf, green, files_dir, failures)
        except Exception:
            log.exception("green api poll failed")
            try:
                await asyncio.wait_for(stop.wait(), error_sleep)
            except TimeoutError:
                pass
```

- [ ] **Step 5: `src/helpdesk/main.py`**

```python
import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from helpdesk import clock, reminders
from helpdesk.config import Settings
from helpdesk.db.session import make_engine, make_session_factory
from helpdesk.delivery import worker
from helpdesk.telegram.gateway import AiogramSender
from helpdesk.telegram.handlers import BOT_COMMANDS, setup
from helpdesk.whatsapp import poller
from helpdesk.whatsapp.client import GreenApiClient


async def amain() -> None:
    settings = Settings()
    clock.set_local_tz(settings.tz)
    logging.basicConfig(level=logging.INFO)
    Path(settings.files_dir).mkdir(parents=True, exist_ok=True)

    engine = make_engine(settings.database_url)
    sf = make_session_factory(engine)
    bot = Bot(settings.telegram_bot_token)
    green = GreenApiClient(
        settings.green_api_url,
        settings.media_url,
        settings.green_api_id_instance,
        settings.green_api_token,
    )
    dp = Dispatcher()
    dp["settings"] = settings
    setup(dp, sf)

    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(worker.run(sf, green, AiogramSender(bot), stop)),
        asyncio.create_task(poller.run(sf, green, settings.files_dir, stop)),
        asyncio.create_task(reminders.run(sf, settings.remind_after_min, stop)),
    ]
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in BOT_COMMANDS])
        await dp.start_polling(bot)
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await green.aclose()
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Тесты зелёные + проверка импорта main**

Run: `make test && uv run python -c "import helpdesk.main"`
Expected: все PASS, импорт без ошибок.

- [ ] **Step 7: Docker-файлы**

`Dockerfile`:
```dockerfile
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
CMD ["sh", "-c", "alembic upgrade head && python -m helpdesk.main"]
```

`.dockerignore`:
```
.venv
.git
data
backups
tests
.env
**/__pycache__
.pytest_cache
.ruff_cache
```

`docker-compose.yml`:
```yaml
services:
  db:
    image: postgres:16
    restart: unless-stopped
    environment:
      POSTGRES_USER: helpdesk
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: helpdesk
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U helpdesk"]
      interval: 5s
      retries: 10

  bot:
    build: .
    restart: unless-stopped
    env_file: .env
    environment:
      DATABASE_URL: postgresql+asyncpg://helpdesk:${POSTGRES_PASSWORD}@db:5432/helpdesk
      FILES_DIR: /data/files
    volumes:
      - ./data:/data
    depends_on:
      db:
        condition: service_healthy
    stop_grace_period: 30s

  backup:
    image: prodrigestivill/postgres-backup-local:16
    restart: unless-stopped
    environment:
      POSTGRES_HOST: db
      POSTGRES_DB: helpdesk
      POSTGRES_USER: helpdesk
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      SCHEDULE: "@daily"
      BACKUP_KEEP_DAYS: 14
      BACKUP_KEEP_WEEKS: 0
      BACKUP_KEEP_MONTHS: 0
      TZ: ${TZ:-Asia/Almaty}
    volumes:
      - ./backups:/backups
    depends_on:
      db:
        condition: service_healthy

volumes:
  pgdata:
```

Проверка:
```bash
cp .env.example .env && docker compose config -q && echo compose-ok
docker build -t helpdesk-bot:test . && docker run --rm helpdesk-bot:test python -c "import helpdesk.main; print('image-ok')"
rm .env
```
Expected: `compose-ok` и `image-ok`.

- [ ] **Step 8: `README.md`**

````markdown
# Helpdesk-бот: WhatsApp → Telegram

Пользователи пишут на общий номер WhatsApp. Заявки приходят специалистам в Telegram,
первый нажавший «✋ Взять» получает заявку, у остальных она исчезает. Подробности:
`docs/superpowers/specs/2026-10-07-helpdesk-bot-design.md`.

## Что нужно

- VPS с Ubuntu (1 vCPU, 1–2 ГБ RAM) и Docker (`curl -fsSL https://get.docker.com | sh`).
- Отдельная SIM-карта с WhatsApp или WhatsApp Business на телефоне.
- Аккаунт Green API (green-api.com) и созданный инстанс.
- Telegram-бот, созданный через @BotFather (нужен токен).

## Установка

1. `git clone <репозиторий> helpdesk-bot && cd helpdesk-bot`
2. `cp .env.example .env` и заполнить:
   - `TELEGRAM_BOT_TOKEN` — токен от @BotFather;
   - `ADMIN_TG_IDS` — ваш Telegram ID (узнать у @userinfobot), через запятую;
   - `GREEN_API_ID_INSTANCE`, `GREEN_API_TOKEN`, `GREEN_API_URL`, `GREEN_API_MEDIA_URL` — из личного кабинета Green API (apiUrl и mediaUrl инстанса);
   - `POSTGRES_PASSWORD` — любой длинный пароль.
3. `mkdir -p data backups && sudo chown 999:999 backups` (сервис бэкапа пишет от uid 999).
4. Green API: в кабинете отсканировать QR телефоном рабочего номера. В настройках инстанса включить
   «Получать уведомления о входящих сообщениях и файлах», поле Webhook URL оставить **пустым**.
5. `docker compose up -d --build`
6. Проверить: `docker compose logs -f bot` — без ошибок, бот отвечает на `/start`.
7. Админ пишет боту `/start`. Специалисты пишут `/start`, админ нажимает «✅ Одобрить».

## Обслуживание

- Логи: `docker compose logs -f bot`
- Обновление: `git pull && docker compose up -d --build` (миграции применяются при старте)
- Бэкапы базы: `./backups/` (ежедневно, 14 дней). Вложения лежат в `./data/files` —
  включите снапшоты диска у провайдера VPS или копируйте папку `data/` отдельно.
- Восстановление: `gunzip -c backups/daily/<файл>.sql.gz | docker compose exec -T db psql -U helpdesk helpdesk`

## Разработка

```bash
uv sync
make test-db   # PostgreSQL для тестов на localhost:5433
make check     # ruff + pytest
```

## Чек-лист ручной приёмки

Нужны: рабочий номер, второй телефон (пользователь) и три Telegram-аккаунта (админ, Ерлан, Тимур).

1. [ ] Админ `/start` → «Вы в системе». Ерлан и Тимур `/start` → админу приходят запросы, оба одобрены.
2. [ ] С телефона пользователя «Не печатает принтер» → вопрос о языке; «1» → ФИО; ФИО → кабинет; кабинет → «Заявка #1 принята».
3. [ ] У всех троих появилась карточка #1 с ФИО, кабинетом, телефоном, текстом и кнопкой «✋ Взять».
4. [ ] Ерлан нажал «Взять» → у админа и Тимура карточка исчезла, у Ерлана кнопки «Закрыть / Вернуть», пользователю пришло «занимается Ерлан».
5. [ ] Тимур и админ почти одновременно жмут «Взять» на новой заявке → заявку получает один, второй видит «Уже взял …».
6. [ ] Пользователь шлёт фото → Ерлан получает фото с подписью `#1 · ФИО`.
7. [ ] Ерлан пишет текст и голосовое → пользователь получает оба в WhatsApp; Ерлану приходит «→ #1 … ✓».
8. [ ] Вторая заявка от другого номера у Ерлана; «Ответить» на сообщение заявки #1 уходит в #1, обычный текст — в активную.
9. [ ] «↩️ Вернуть» → карточка снова у всех на смене. «✅ Закрыть» → пользователю «Заявка выполнена»; новое сообщение создаёт новую заявку.
10. [ ] Заявку никто не берёт 10 минут → приходит «⏰ … ждёт 10 мин».
11. [ ] `/off` у всех → новая заявка: админу «на смене никого»; `/on` у Тимура → карточка приходит ему.
12. [ ] `/my`, `/open`, `/find 1`, `/find <ФИО>`, «Показать переписку» показывают верные данные.
13. [ ] `/stats` → цифры совпадают с реальностью; `/export` → Excel открывается, кириллица и даты корректны.
14. [ ] Казахский язык: новый номер, ответ «2» → все сообщения на казахском.
15. [ ] `docker compose restart bot` во время переписки → сообщения не теряются, отправка продолжается.
16. [ ] `/staff` → отключить Тимура с заявкой в работе → заявка вернулась в очередь, Тимур получает «Доступ закрыт».
````

- [ ] **Step 9: Финальная проверка и commit**

Run: `make check`
Expected: ruff без ошибок, все тесты PASS.

```bash
git add -A
git commit -m "feat: WhatsApp poller, app entrypoint, Docker deployment, README

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
