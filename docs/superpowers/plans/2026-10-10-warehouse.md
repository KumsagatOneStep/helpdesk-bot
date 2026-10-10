# Склад техники и расходников — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Кнопка «📦 Склад» в боте: остатки по категориям и состояниям, специалист берёт (−), координатор ведёт приход, исправления, позиции, минимум, загрузку xlsx; выгрузка «Склад.xlsx»; предупреждение координаторам при падении до минимума.

**Architecture:** Пакет `helpdesk/stock/`: `catalog.py` (фиксированные категории и состояния), `service.py` (все изменения остатков под блокировкой строки позиции + история + порог минимума), `render.py` (тексты и кнопки), `xlsx.py` (шаблон, разбор загрузки, выгрузка). Общие xlsx-помощники выносятся из `stats/export.py` в `helpdesk/xlsx_util.py`. Обработчики — новый роутер `telegram/stock_handlers.py` с FSM `StockForm`, подключается между `task_handlers.router` и общим роутером. Одна миграция (3 таблицы).

**Tech Stack:** Python 3.12, aiogram 3.31, SQLAlchemy 2 async + asyncpg, Postgres 16, alembic, openpyxl, pytest.

**Spec:** `docs/superpowers/specs/2026-10-10-warehouse-design.md`

## Global Constraints

- Ветка `warehouse` (от `voice-input`). Перед началом `make test-db && make check` — 267 тестов зелёные; после каждой задачи — тоже.
- Перед каждым `make check`: `uv run ruff format src tests`. Импорты в начале файлов.
- Тесты — против тестовой Postgres (фикстуры `s`, `sf`, `clock_` из `tests/conftest.py`), команда для отдельного файла: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q <файл>`.
- Состояния: коды `new`/`used`/`repaired`, подписи «новые»/«б/у»/«после ремонта»; категории — ровно 13 кодов из спеки §2 в том же порядке.
- Лимиты: приход кнопкой 1…1000; исправление остатка, минимум, количество в xlsx 0…100000; xlsx ≤ 2 МБ и ≤ 1000 строк данных; название 1…200 символов; страница категории — 10 позиций; поиск — до 20.
- Остаток не бывает < 0: каждое изменение — под `SELECT … FOR UPDATE` строки `stock_items`, вместе с записью в `stock_moves`, в транзакции обработчика.
- Права: координатор = `staff.role == "coordinator"` (и `status == "active"`); админ без этой роли на складе — специалист. Проверка и в сервисе (`NotCoordinator`), и в обработчиках (кнопки скрыты + alert «Только для координатора»).
- Telegram: без `parse_mode`. Новые callback-данные начинаются с `sk_`; id в callback — `\d{1,9}`.
- Ни один путь склада не создаёт outbox `wa`; ввод посреди диалога склада не уходит пользователю WhatsApp.
- Текст в xlsx — только через `xlsx_util.safe` (защита от формул).
- Сообщения коммитов на английском, последняя строка: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Две одновременные выдачи последней штуки** — одна успешна, вторая «осталось 0», `qty` не < 0. Тест в задаче 1 (`test_two_takes_of_last_unit`).
2. **Порог минимума шлёт одно сообщение на пересечение**, а не после каждой выдачи ниже минимума; снова — только после подъёма выше. Тест в задаче 2 (`test_low_stock_alert_once_per_crossing`).
3. **Формула в ячейке xlsx не исполняется и не попадает в выгрузку как формула**: `=1+1` в «Количестве» → ошибка строки; `=cmd` в названии позиции → в выгрузке текст с `'`. Тесты в задаче 3 (`test_parse_formula_cell_is_not_evaluated`, `test_export_neutralizes_formulas`).
4. **Специалист не может выполнить действие координатора даже прямым callback** (`sk_in:…`, `sk_archok:…`, `sk_upl`). Тесты в задачах 5 и 6 (`test_coordinator_callbacks_rejected_for_specialist`, `test_upload_rejected_for_specialist`).
5. **Ввод посреди диалога склада не уходит в WhatsApp и кнопки меню выводят из диалога**. Тест в задаче 4 (`test_stock_dialog_routing`).

---

### Task 1: Категории, модели, миграция, базовый сервис

**Files:**
- Create: `src/helpdesk/stock/__init__.py` (пустой), `src/helpdesk/stock/catalog.py`, `src/helpdesk/stock/service.py`, `migrations/versions/d5e1f7a2b8c9_stock.py`
- Modify: `src/helpdesk/db/models.py`
- Test: `tests/test_stock_catalog.py`, `tests/test_stock_service.py`

**Interfaces:**
- Produces: `catalog.CATEGORIES: dict[str,str]`, `catalog.CONDITIONS: dict[str,str]`, `catalog.CONDITION_ONE: dict[str,str]`, `catalog.plain(code) -> str`, `catalog.parse_category(v) -> str|None`, `catalog.parse_condition(v) -> str|None`; модели `StockItem`, `StockLevel`, `StockMove`; `service.StockError`, `NotCoordinator`, `Archived`, `BadQty`, `DuplicateName(item_id)`, `TakeResult(ok, left, available)`, `MAX_IN=1000`, `MAX_LEVEL=100_000`, `is_coordinator(staff)`, `find_active(s, category, name)`, `create_item(s, staff, category, name, min_qty=None)`, `get_item(s, item_id)`, `levels(s, item_id) -> dict[str,int]` (все три состояния), `receive(s, staff, item_id, condition, qty, *, kind="in") -> int`, `take(s, staff, item_id, condition, qty) -> TakeResult`, внутренние `_locked`, `_add`, `_notify_low` (в задаче 1 — заглушка без уведомлений, наполняется в задаче 2).

- [ ] **Step 1: Тесты**

Create `tests/test_stock_catalog.py`:

```python
from helpdesk.stock import catalog


def test_categories_fixed_order():
    assert list(catalog.CATEGORIES) == [
        "printers", "pc", "monitors", "cartridges", "cables", "storage", "adapters",
        "hubs", "headsets", "batteries", "mice", "keyboards", "other",
    ]
    assert catalog.CATEGORIES["cartridges"] == "🖋 Картриджи"
    assert catalog.plain("cartridges") == "Картриджи"


def test_parse_category_accepts_code_label_and_plain():
    for v in ("cartridges", "🖋 Картриджи", "картриджи", "  КАРТРИДЖИ "):
        assert catalog.parse_category(v) == "cartridges", v
    assert catalog.parse_category("Принтер") is None
    assert catalog.parse_category(None) is None


def test_parse_condition():
    for v, code in [("new", "new"), ("Новые", "new"), ("новое", "new"), ("новый", "new"),
                    (None, "new"), ("", "new"), ("б/у", "used"), ("БУ", "used"),
                    ("used", "used"), ("после ремонта", "repaired"), ("repaired", "repaired")]:
        assert catalog.parse_condition(v) == code, v
    assert catalog.parse_condition("сломан") is None
```

Create `tests/test_stock_service.py`:

```python
import asyncio

import pytest
from sqlalchemy import select

from helpdesk.db.models import Staff, StockMove
from helpdesk.stock import service as stock
from tests.factories import make_staff


async def _people(s):
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    return coord, erlan


async def test_create_receive_take(s, clock_):
    coord, erlan = await _people(s)
    item = await stock.create_item(s, coord, "cartridges", "  Картридж   HP 85A ", min_qty=3)
    assert (item.name, item.category, item.min_qty, item.archived) == (
        "Картридж HP 85A", "cartridges", 3, False,
    )
    assert await stock.levels(s, item.id) == {"new": 0, "used": 0, "repaired": 0}
    assert await stock.receive(s, coord, item.id, "new", 7) == 7
    r = await stock.take(s, erlan, item.id, "new", 2)
    assert (r.ok, r.left, r.available) == (True, 5, 7)
    r = await stock.take(s, erlan, item.id, "new", 9)
    assert (r.ok, r.left, r.available) == (False, 5, 5)
    assert await stock.levels(s, item.id) == {"new": 5, "used": 0, "repaired": 0}
    moves = (await s.scalars(select(StockMove).order_by(StockMove.id))).all()
    assert [(m.kind, m.delta, m.condition, m.staff_id) for m in moves] == [
        ("in", 7, "new", coord.id),
        ("out", -2, "new", erlan.id),
    ]


async def test_take_from_empty_condition(s, clock_):
    coord, erlan = await _people(s)
    item = await stock.create_item(s, coord, "mice", "Мышь USB")
    r = await stock.take(s, erlan, item.id, "used", 1)
    assert (r.ok, r.available) == (False, 0)


async def test_duplicate_name_case_insensitive(s, clock_):
    coord, _ = await _people(s)
    item = await stock.create_item(s, coord, "mice", "Мышь USB")
    with pytest.raises(stock.DuplicateName) as e:
        await stock.create_item(s, coord, "mice", "мышь usb")
    assert e.value.item_id == item.id
    other = await stock.create_item(s, coord, "keyboards", "Мышь USB")  # другая категория — можно
    assert other.id != item.id


async def test_specialist_cannot_create_or_receive(s, clock_):
    coord, erlan = await _people(s)
    with pytest.raises(stock.NotCoordinator):
        await stock.create_item(s, erlan, "mice", "Мышь")
    item = await stock.create_item(s, coord, "mice", "Мышь")
    with pytest.raises(stock.NotCoordinator):
        await stock.receive(s, erlan, item.id, "new", 1)
    admin = await make_staff(s, tg_id=9, name="Админ", is_admin=True)
    assert not stock.is_coordinator(admin)


async def test_qty_limits(s, clock_):
    coord, erlan = await _people(s)
    item = await stock.create_item(s, coord, "mice", "Мышь")
    for bad in (0, -1, 1001):
        with pytest.raises(stock.BadQty):
            await stock.receive(s, coord, item.id, "new", bad)
    assert await stock.receive(s, coord, item.id, "new", 5000, kind="import") == 5000
    with pytest.raises(stock.BadQty):
        await stock.take(s, erlan, item.id, "new", 0)


async def test_two_takes_of_last_unit(sf, clock_):
    async with sf() as s, s.begin():
        coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
        a = await make_staff(s, tg_id=1, name="Ерлан")
        b = await make_staff(s, tg_id=2, name="Тимур")
        item = await stock.create_item(s, coord, "cartridges", "HP 85A")
        await stock.receive(s, coord, item.id, "new", 1)
        item_id, ids = item.id, (a.id, b.id)

    async def grab(staff_id):
        async with sf() as s2, s2.begin():
            return await stock.take(s2, await s2.get(Staff, staff_id), item_id, "new", 1)

    results = await asyncio.gather(*(grab(i) for i in ids))
    assert sorted(r.ok for r in results) == [False, True]
    async with sf() as s:
        assert (await stock.levels(s, item_id))["new"] == 0
```

- [ ] **Step 2: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_stock_catalog.py tests/test_stock_service.py`
Expected: FAIL (`ModuleNotFoundError: No module named 'helpdesk.stock'`).

- [ ] **Step 3: `src/helpdesk/stock/catalog.py`**

```python
CATEGORIES: dict[str, str] = {
    "printers": "🖨 Принтеры и оргтехника",
    "pc": "🖥 Системные блоки",
    "monitors": "📺 Мониторы",
    "cartridges": "🖋 Картриджи",
    "cables": "🔌 Кабели (VGA, HDMI)",
    "storage": "💾 Диски (SSD, HDD)",
    "adapters": "🔀 Переходники (сеть→USB и др.)",
    "hubs": "🔗 USB-хабы",
    "headsets": "🎧 Наушники",
    "batteries": "🔋 Батарейки",
    "mice": "🖱 Мыши",
    "keyboards": "⌨️ Клавиатуры",
    "other": "📦 Прочее",
}
CONDITIONS: dict[str, str] = {"new": "новые", "used": "б/у", "repaired": "после ремонта"}
# для «×1 (новый)»
CONDITION_ONE: dict[str, str] = {"new": "новый", "used": "б/у", "repaired": "после ремонта"}

_CONDITION_ALIASES = {
    "new": ("new", "новые", "новое", "новый", "новая"),
    "used": ("used", "б/у", "бу", "б у", "старые", "старое"),
    "repaired": ("repaired", "после ремонта", "отремонтированные"),
}


def _norm(v) -> str:
    return " ".join(str(v).split()).lower() if v is not None else ""


def plain(code: str) -> str:
    """Подпись категории без эмодзи."""
    return CATEGORIES[code].split(" ", 1)[1]


def parse_category(v) -> str | None:
    t = _norm(v)
    for code, label in CATEGORIES.items():
        if t in (code, label.lower(), plain(code).lower()):
            return code
    return None


def parse_condition(v) -> str | None:
    t = _norm(v)
    if not t:
        return "new"
    for code, aliases in _CONDITION_ALIASES.items():
        if t in aliases:
            return code
    return None
```

- [ ] **Step 4: Модели (`src/helpdesk/db/models.py`)**

Добавить `CheckConstraint`, `func` в импорты из `sqlalchemy`. В конец файла:

```python
class StockItem(Base):
    __tablename__ = "stock_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(24))
    name: Mapped[str] = mapped_column(String(200))
    min_qty: Mapped[int | None]
    archived: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(default=_now)


# одно активное имя на категорию без учёта регистра; в архиве дубли допустимы
Index(
    "uq_stock_items_active_name",
    StockItem.category,
    func.lower(StockItem.name),
    unique=True,
    postgresql_where=StockItem.archived.is_(False),
)


class StockLevel(Base):
    __tablename__ = "stock_levels"
    __table_args__ = (CheckConstraint("qty >= 0", name="ck_stock_levels_qty"),)
    item_id: Mapped[int] = mapped_column(ForeignKey("stock_items.id"), primary_key=True)
    condition: Mapped[str] = mapped_column(String(16), primary_key=True)
    qty: Mapped[int] = mapped_column(default=0)


class StockMove(Base):
    __tablename__ = "stock_moves"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("stock_items.id"), index=True)
    condition: Mapped[str] = mapped_column(String(16))
    delta: Mapped[int]
    kind: Mapped[str] = mapped_column(String(8))  # in | out | fix | import
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(default=_now, index=True)
```

- [ ] **Step 5: Миграция `migrations/versions/d5e1f7a2b8c9_stock.py`**

`down_revision` — текущая голова. Проверить: `uv run alembic heads` (ожидается `c4d2e8f1a6b7`); если другая — подставить её.

```python
"""stock

Revision ID: d5e1f7a2b8c9
Revises: c4d2e8f1a6b7
Create Date: 2026-10-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5e1f7a2b8c9"
down_revision: str | Sequence[str] | None = "c4d2e8f1a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "stock_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("min_qty", sa.Integer(), nullable=True),
        sa.Column("archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index(
        "uq_stock_items_active_name",
        "stock_items",
        ["category", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("NOT archived"),
    )
    op.create_table(
        "stock_levels",
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("stock_items.id"), primary_key=True),
        sa.Column("condition", sa.String(16), primary_key=True),
        sa.Column("qty", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("qty >= 0", name="ck_stock_levels_qty"),
    )
    op.create_table(
        "stock_moves",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("stock_items.id"), nullable=False),
        sa.Column("condition", sa.String(16), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("staff_id", sa.Integer(), sa.ForeignKey("staff.id"), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_stock_moves_item_id", "stock_moves", ["item_id"])
    op.create_index("ix_stock_moves_created_at", "stock_moves", ["created_at"])


def downgrade() -> None:
    op.drop_table("stock_moves")
    op.drop_table("stock_levels")
    op.drop_index("uq_stock_items_active_name", table_name="stock_items")
    op.drop_table("stock_items")
```

- [ ] **Step 6: `src/helpdesk/stock/service.py`**

```python
from dataclasses import dataclass

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from helpdesk.db.models import Staff, StockItem, StockLevel, StockMove
from helpdesk.stock.catalog import CATEGORIES, CONDITIONS

log = structlog.get_logger()

MAX_IN = 1000  # приход кнопкой
MAX_LEVEL = 100_000  # исправление, минимум, количество в xlsx


class StockError(Exception):
    pass


class NotCoordinator(StockError):
    pass


class Archived(StockError):
    """Позиции нет или она в архиве."""


class BadQty(StockError):
    pass


class DuplicateName(StockError):
    def __init__(self, item_id: int):
        super().__init__(item_id)
        self.item_id = item_id


@dataclass
class TakeResult:
    ok: bool
    left: int
    available: int


def is_coordinator(staff: Staff) -> bool:
    return staff.status == "active" and staff.role == "coordinator"


def _need_coordinator(staff: Staff) -> None:
    if not is_coordinator(staff):
        raise NotCoordinator()


def _check_qty(qty, lo: int, hi: int) -> None:
    if isinstance(qty, bool) or not isinstance(qty, int) or not lo <= qty <= hi:
        raise BadQty()


def _check_condition(condition: str) -> None:
    if condition not in CONDITIONS:
        raise ValueError(condition)


def clean_name(name: str) -> str:
    return " ".join(name.split())


async def find_active(s, category: str, name: str) -> StockItem | None:
    q = select(StockItem).where(
        StockItem.category == category,
        func.lower(StockItem.name) == clean_name(name).lower(),
        StockItem.archived.is_(False),
    )
    return await s.scalar(q)


async def create_item(s, staff: Staff, category: str, name: str, min_qty=None) -> StockItem:
    _need_coordinator(staff)
    if category not in CATEGORIES:
        raise ValueError(category)
    name = clean_name(name)
    if not 1 <= len(name) <= 200:
        raise ValueError("name")
    if min_qty is not None:
        _check_qty(min_qty, 0, MAX_LEVEL)
    if dup := await find_active(s, category, name):
        raise DuplicateName(dup.id)
    item = StockItem(category=category, name=name, min_qty=min_qty)
    s.add(item)
    await s.flush()
    return item


async def get_item(s, item_id: int) -> StockItem | None:
    return await s.get(StockItem, item_id)


async def levels(s, item_id: int) -> dict[str, int]:
    out = dict.fromkeys(CONDITIONS, 0)
    q = select(StockLevel.condition, StockLevel.qty).where(StockLevel.item_id == item_id)
    for condition, qty in (await s.execute(q)).all():
        out[condition] = qty
    return out


async def _locked(s, item_id: int) -> StockItem:
    """Блокировка позиции: все изменения её остатков идут по очереди."""
    item = await s.get(StockItem, item_id, with_for_update=True, populate_existing=True)
    if item is None or item.archived:
        raise Archived()
    return item


async def _add(s, staff: Staff, item: StockItem, condition: str, delta: int, kind: str) -> None:
    stmt = insert(StockLevel).values(item_id=item.id, condition=condition, qty=delta)
    stmt = stmt.on_conflict_do_update(
        index_elements=["item_id", "condition"],
        set_={"qty": StockLevel.__table__.c.qty + delta},
    )
    await s.execute(stmt)
    s.add(StockMove(item_id=item.id, condition=condition, delta=delta, kind=kind, staff_id=staff.id))
    await s.flush()


async def _notify_low(s, item: StockItem, before: int, after: int) -> None:
    """Порог минимума — задача 2."""


async def receive(s, staff: Staff, item_id: int, condition: str, qty: int, *, kind="in") -> int:
    _need_coordinator(staff)
    _check_condition(condition)
    _check_qty(qty, 1, MAX_IN if kind == "in" else MAX_LEVEL)
    item = await _locked(s, item_id)
    await _add(s, staff, item, condition, qty, kind)
    return (await levels(s, item.id))[condition]


async def take(s, staff: Staff, item_id: int, condition: str, qty: int) -> TakeResult:
    _check_condition(condition)
    _check_qty(qty, 1, MAX_LEVEL)
    item = await _locked(s, item_id)
    lv = await levels(s, item.id)
    before, available = sum(lv.values()), lv[condition]
    if qty > available:
        return TakeResult(False, available, available)
    await _add(s, staff, item, condition, -qty, "out")
    await _notify_low(s, item, before, before - qty)
    return TakeResult(True, available - qty, available)
```

- [ ] **Step 7: Проверить и закоммитить**

Run: `uv run ruff format src tests && make check` — Expected: PASS (267 + 9 новых).

```bash
git add src/helpdesk/stock src/helpdesk/db/models.py migrations/versions/d5e1f7a2b8c9_stock.py tests/test_stock_catalog.py tests/test_stock_service.py
git commit -m "feat(stock): catalog, tables and locked receive/take service"
```

---

### Task 2: Исправление, минимум, архив, порог, выборки

**Files:**
- Modify: `src/helpdesk/stock/service.py`
- Test: `tests/test_stock_service.py`

**Interfaces:**
- Consumes: всё из задачи 1; `helpdesk.tasks.rules.coordinators(s)`; `helpdesk.delivery.outbox.tg_send`.
- Produces: `set_level(s, staff, item_id, condition, new_qty)`, `set_min(s, staff, item_id, min_qty | None)`, `archive(s, staff, item_id)`, `@dataclass ItemRow(item, levels)` со свойствами `total: int`, `low: bool`; `category_totals(s) -> dict[str,int]` (все 13 кодов), `PER_PAGE = 10`, `items_in(s, category, page) -> tuple[list[ItemRow], int, int]` (строки, номер страницы после ограничения, всего страниц ≥ 1), `search(s, text, limit=20) -> list[ItemRow]`, `row(s, item) -> ItemRow`, `rows_for(s, items) -> list[ItemRow]` (уровни одной выборкой).

- [ ] **Step 1: Тесты** — в конец `tests/test_stock_service.py` (импорты `payloads`, `clear_outbox` из `tests.factories` добавить вверх):

```python
async def test_set_level_writes_difference(s, clock_):
    coord, _ = await _people(s)
    item = await stock.create_item(s, coord, "batteries", "AA")
    await stock.receive(s, coord, item.id, "new", 10)
    await stock.set_level(s, coord, item.id, "new", 7)
    await stock.set_level(s, coord, item.id, "new", 7)  # без изменений — без записи
    await stock.set_level(s, coord, item.id, "used", 2)
    moves = (await s.scalars(select(StockMove).order_by(StockMove.id))).all()
    assert [(m.kind, m.condition, m.delta) for m in moves] == [
        ("in", "new", 10), ("fix", "new", -3), ("fix", "used", 2),
    ]
    assert await stock.levels(s, item.id) == {"new": 7, "used": 2, "repaired": 0}


async def test_low_stock_alert_once_per_crossing(s, clock_):
    coord, erlan = await _people(s)
    coord2 = await make_staff(s, tg_id=4, name="Данияр", role="coordinator")
    item = await stock.create_item(s, coord, "cartridges", "HP 85A", min_qty=3)
    await stock.receive(s, coord, item.id, "new", 5)
    await stock.take(s, erlan, item.id, "new", 1)  # 4 — выше минимума
    assert await payloads(s, "tg") == []
    await stock.take(s, erlan, item.id, "new", 2)  # 2 — пересечение
    ps = await payloads(s, "tg")
    assert sorted(p["chat_id"] for p in ps) == [coord.tg_chat_id, coord2.tg_chat_id]
    assert {p["text"] for p in ps} == {"⚠️ HP 85A: осталось 2 (минимум 3)"}
    await clear_outbox(s)
    await stock.take(s, erlan, item.id, "new", 1)  # уже ниже — молчим
    assert await payloads(s, "tg") == []
    await stock.receive(s, coord, item.id, "new", 5)  # 6
    await stock.set_level(s, coord, item.id, "new", 3)  # ровно минимум — пересечение
    assert [p["text"] for p in await payloads(s, "tg")] == [
        "⚠️ HP 85A: осталось 3 (минимум 3)"
    ] * 2
    assert await payloads(s, "wa") == []


async def test_low_stock_without_coordinators_or_min(s, clock_):
    coord, erlan = await _people(s)
    item = await stock.create_item(s, coord, "mice", "Мышь", min_qty=1)
    plain = await stock.create_item(s, coord, "mice", "Мышь б/п")
    await stock.receive(s, coord, item.id, "new", 2)
    await stock.receive(s, coord, plain.id, "new", 1)
    coord.role = "helpdesk"
    await s.flush()
    await stock.take(s, erlan, item.id, "new", 2)
    await stock.take(s, erlan, plain.id, "new", 1)
    assert await payloads(s, "tg") == []


async def test_archive_and_min(s, clock_):
    coord, erlan = await _people(s)
    item = await stock.create_item(s, coord, "mice", "Мышь")
    await stock.set_min(s, coord, item.id, 2)
    assert (await stock.get_item(s, item.id)).min_qty == 2
    await stock.set_min(s, coord, item.id, None)
    assert (await stock.get_item(s, item.id)).min_qty is None
    with pytest.raises(stock.NotCoordinator):
        await stock.archive(s, erlan, item.id)
    await stock.archive(s, coord, item.id)
    for call in (
        stock.take(s, erlan, item.id, "new", 1),
        stock.receive(s, coord, item.id, "new", 1),
        stock.set_level(s, coord, item.id, "new", 1),
        stock.set_min(s, coord, item.id, 1),
    ):
        with pytest.raises(stock.Archived):
            await call
    again = await stock.create_item(s, coord, "mice", "Мышь")  # имя освободилось
    assert again.id != item.id


async def test_totals_pages_and_search(s, clock_):
    coord, _ = await _people(s)
    for i in range(12):
        it = await stock.create_item(s, coord, "cables", f"Кабель {i:02d}")
        await stock.receive(s, coord, it.id, "new", 1)
    hdmi = await stock.create_item(s, coord, "cables", "HDMI 2м", min_qty=5)
    await stock.receive(s, coord, hdmi.id, "used", 3)
    gone = await stock.create_item(s, coord, "cables", "HDMI старый")
    await stock.receive(s, coord, gone.id, "new", 100)
    await stock.archive(s, coord, gone.id)
    totals = await stock.category_totals(s)
    assert totals["cables"] == 15 and totals["mice"] == 0 and len(totals) == 13
    rows, page, pages = await stock.items_in(s, "cables", 0)
    assert (len(rows), page, pages) == (10, 0, 2)
    assert rows[0].item.name == "HDMI 2м" and rows[0].low and rows[0].total == 3
    rows, page, _ = await stock.items_in(s, "cables", 99)  # за краем — последняя
    assert (len(rows), page) == (3, 1)
    rows, page, pages = await stock.items_in(s, "mice", 0)
    assert (rows, page, pages) == ([], 0, 1)
    found = await stock.search(s, "hdmi")
    assert [r.item.name for r in found] == ["HDMI 2м"]
    assert await stock.search(s, "%") == []  # спецсимволы LIKE экранируются
    assert len(await stock.search(s, "кабель")) == 12
```

- [ ] **Step 2: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_stock_service.py`
Expected: FAIL (`AttributeError: ... 'set_level'`).

- [ ] **Step 3: Реализация в `service.py`**

Импорты: `from math import ceil`, `from helpdesk.delivery import outbox`, `from helpdesk.tasks import rules`. Заменить заглушку `_notify_low` и добавить:

```python
PER_PAGE = 10


async def _notify_low(s, item: StockItem, before: int, after: int) -> None:
    """Одно предупреждение при пересечении минимума сверху вниз."""
    m = item.min_qty
    if m is None or not before > m >= after:
        return
    coords = await rules.coordinators(s)
    if not coords:
        log.warning("stock low, no coordinators", item_id=item.id)
        return
    for c in coords:
        outbox.tg_send(s, c.tg_chat_id, text=f"⚠️ {item.name}: осталось {after} (минимум {m})")
    await s.flush()


async def set_level(s, staff: Staff, item_id: int, condition: str, new_qty: int) -> None:
    _need_coordinator(staff)
    _check_condition(condition)
    _check_qty(new_qty, 0, MAX_LEVEL)
    item = await _locked(s, item_id)
    lv = await levels(s, item.id)
    delta = new_qty - lv[condition]
    if delta == 0:
        return
    before = sum(lv.values())
    await _add(s, staff, item, condition, delta, "fix")
    await _notify_low(s, item, before, before + delta)


async def set_min(s, staff: Staff, item_id: int, min_qty: int | None) -> None:
    _need_coordinator(staff)
    if min_qty is not None:
        _check_qty(min_qty, 0, MAX_LEVEL)
    item = await _locked(s, item_id)
    item.min_qty = min_qty
    await s.flush()


async def archive(s, staff: Staff, item_id: int) -> StockItem:
    _need_coordinator(staff)
    item = await _locked(s, item_id)
    item.archived = True
    await s.flush()
    return item


@dataclass
class ItemRow:
    item: StockItem
    levels: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.levels.values())

    @property
    def low(self) -> bool:
        return self.item.min_qty is not None and self.total <= self.item.min_qty


async def rows_for(s, items: list[StockItem]) -> list[ItemRow]:
    by_id = {i.id: dict.fromkeys(CONDITIONS, 0) for i in items}
    if by_id:
        q = select(StockLevel.item_id, StockLevel.condition, StockLevel.qty).where(
            StockLevel.item_id.in_(by_id)
        )
        for item_id, condition, qty in (await s.execute(q)).all():
            by_id[item_id][condition] = qty
    return [ItemRow(i, by_id[i.id]) for i in items]


async def row(s, item: StockItem) -> ItemRow:
    return (await rows_for(s, [item]))[0]


async def category_totals(s) -> dict[str, int]:
    out = dict.fromkeys(CATEGORIES, 0)
    q = (
        select(StockItem.category, func.coalesce(func.sum(StockLevel.qty), 0))
        .outerjoin(StockLevel, StockLevel.item_id == StockItem.id)
        .where(StockItem.archived.is_(False))
        .group_by(StockItem.category)
    )
    for category, total in (await s.execute(q)).all():
        if category in out:
            out[category] = int(total)
    return out


async def items_in(s, category: str, page: int) -> tuple[list[ItemRow], int, int]:
    active = (StockItem.category == category, StockItem.archived.is_(False))
    count = await s.scalar(select(func.count()).select_from(StockItem).where(*active))
    pages = max(1, ceil(count / PER_PAGE))
    page = min(max(page, 0), pages - 1)
    q = (
        select(StockItem)
        .where(*active)
        .order_by(func.lower(StockItem.name), StockItem.id)
        .offset(page * PER_PAGE)
        .limit(PER_PAGE)
    )
    return await rows_for(s, list((await s.scalars(q)).all())), page, pages


async def search(s, text: str, limit: int = 20) -> list[ItemRow]:
    t = clean_name(text)
    if not t:
        return []
    esc = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    q = (
        select(StockItem)
        .where(StockItem.archived.is_(False), StockItem.name.ilike(f"%{esc}%", escape="\\"))
        .order_by(func.lower(StockItem.name), StockItem.id)
        .limit(limit)
    )
    return await rows_for(s, list((await s.scalars(q)).all()))
```

Примечание к `test_totals_pages_and_search`: «HDMI 2м» сортируется перед «Кабель …» (латиница раньше кириллицы в `lower()` при C/UTF-8 collation тестовой БД). Если collation БД сортирует иначе — поправить ожидание в тесте на фактический порядок `lower(name)`, сохранив проверку `low`/`total` для позиции «HDMI 2м» (найти её в `rows` по имени).

- [ ] **Step 4: Проверить и закоммитить**

Run: `uv run ruff format src tests && make check` — Expected: PASS.

```bash
git add src/helpdesk/stock/service.py tests/test_stock_service.py
git commit -m "feat(stock): fix levels, minimum alerts, archive, category totals, paging and search"
```

---

### Task 3: xlsx — общий модуль, шаблон, разбор загрузки, импорт, выгрузка

**Files:**
- Create: `src/helpdesk/xlsx_util.py`, `src/helpdesk/stock/xlsx.py`
- Modify: `src/helpdesk/stats/export.py` (использовать `xlsx_util`), `src/helpdesk/stock/service.py` (`ImportRow`, `preview_import`, `apply_import`)
- Test: `tests/test_stock_xlsx.py`

**Interfaces:**
- Consumes: сервис задач 1–2, `catalog`.
- Produces: `xlsx_util.safe(v)`, `xlsx_util.naive(dt)`, `xlsx_util.layout(ws, widths)`; `service.ImportRow(line, category, name, condition, qty, min_qty)` (dataclass, хранится в FSM как `asdict`), `service.preview_import(s, rows) -> tuple[int, int]` (новых позиций, строк по существующим), `service.apply_import(s, staff, rows) -> tuple[int, int]` (создано позиций, строк с приходом); `stock_xlsx.MAX_BYTES`, `MAX_ROWS`, `BadFile(Exception)` (текст — причина для пользователя), `parse(data: bytes) -> tuple[list[ImportRow], list[str]]`, `template() -> bytes`, `async export(s) -> bytes`, `STOCK_HEADERS`, `MOVE_HEADERS`.

- [ ] **Step 1: Тесты**

Create `tests/test_stock_xlsx.py`:

```python
from datetime import datetime
from io import BytesIO

import pytest
from openpyxl import Workbook, load_workbook

from helpdesk.stock import service as stock
from helpdesk.stock import xlsx as stock_xlsx
from tests.factories import make_staff


def _book(rows, header=True) -> bytes:
    wb = Workbook()
    ws = wb.active
    if header:
        ws.append(["Категория", "Название", "Состояние", "Количество", "Минимум"])
    for r in rows:
        ws.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_valid_rows():
    rows, errors = stock_xlsx.parse(_book([
        ["Картриджи", "Картридж HP 85A", "новые", 5, 3],
        ["mice", " Мышь   USB ", None, "2", None],
        [None, None, None, None, None],  # пустая строка пропускается
        ["🖥 Системные блоки", "Dell OptiPlex", "после ремонта", 1.0, ""],
    ]))
    assert errors == []
    assert [(r.line, r.category, r.name, r.condition, r.qty, r.min_qty) for r in rows] == [
        (2, "cartridges", "Картридж HP 85A", "new", 5, 3),
        (3, "mice", "Мышь USB", "new", 2, None),
        (5, "pc", "Dell OptiPlex", "repaired", 1, None),
    ]


def test_parse_reports_each_bad_field_with_line():
    _, errors = stock_xlsx.parse(_book([
        ["Принтер", "X", "новые", 1, None],
        ["Мыши", "", "новые", 1, None],
        ["Мыши", "Y", "сломан", 1, None],
        ["Мыши", "Z", "новые", "пять", None],
        ["Мыши", "W", "новые", 1, -2],
        ["Мыши", "V", "новые", 100001, None],
    ]))
    assert errors == [
        "строка 2: неизвестная категория «Принтер»",
        "строка 3: пустое или слишком длинное название",
        "строка 4: неизвестное состояние «сломан»",
        "строка 5: количество «пять» — нужно целое от 0 до 100000",
        "строка 6: минимум «-2» — нужно целое от 0 до 100000",
        "строка 7: количество «100001» — нужно целое от 0 до 100000",
    ]


def test_parse_formula_cell_is_not_evaluated():
    rows, errors = stock_xlsx.parse(_book([["Мыши", "Мышь", "новые", "=1+1", None]]))
    assert rows == [] and errors == ["строка 2: количество «» — нужно целое от 0 до 100000"]


def test_parse_limits():
    with pytest.raises(stock_xlsx.BadFile, match="Больше 1000 строк"):
        stock_xlsx.parse(_book([["Мыши", f"М{i}", "новые", 1, None] for i in range(1001)]))
    with pytest.raises(stock_xlsx.BadFile, match="больше 2 МБ"):
        stock_xlsx.parse(b"x" * (stock_xlsx.MAX_BYTES + 1))
    with pytest.raises(stock_xlsx.BadFile, match="Не удалось прочитать"):
        stock_xlsx.parse(b"not a zip")


def test_template_has_headers_and_help():
    wb = load_workbook(BytesIO(stock_xlsx.template()))
    assert wb.sheetnames == ["Склад", "Справка"]
    assert [c.value for c in wb["Склад"][1]] == [
        "Категория", "Название", "Состояние", "Количество", "Минимум",
    ]
    assert wb["Склад"].max_row == 1  # примеры только в «Справке» — случайно не загрузятся
    help_text = "\n".join(str(c.value) for row in wb["Справка"].iter_rows() for c in row if c.value)
    assert "Картриджи" in help_text and "после ремонта" in help_text
    assert "прибавляет" in help_text


async def test_preview_and_apply_import_adds_up(s, clock_):
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    existing = await stock.create_item(s, coord, "mice", "Мышь USB", min_qty=1)
    rows, _ = stock_xlsx.parse(_book([
        ["Мыши", "мышь usb", "б/у", 2, 4],
        ["Картриджи", "HP 85A", "новые", 5, None],
        ["Картриджи", "HP 85A", "новые", 1, 3],
        ["Батарейки", "AA", "новые", 0, 10],
    ]))
    assert await stock.preview_import(s, rows) == (2, 1)
    assert await stock.apply_import(s, coord, rows) == (2, 3)
    hp = await stock.find_active(s, "cartridges", "hp 85a")
    assert (await stock.levels(s, hp.id))["new"] == 6 and hp.min_qty == 3
    assert (await stock.levels(s, existing.id))["used"] == 2
    assert (await stock.get_item(s, existing.id)).min_qty == 4
    aa = await stock.find_active(s, "batteries", "AA")
    assert aa.min_qty == 10 and sum((await stock.levels(s, aa.id)).values()) == 0
    await stock.apply_import(s, coord, rows)  # повторная загрузка прибавляет
    assert (await stock.levels(s, hp.id))["new"] == 12


async def test_apply_import_requires_coordinator(s, clock_):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    rows, _ = stock_xlsx.parse(_book([["Мыши", "Мышь", "новые", 1, None]]))
    with pytest.raises(stock.NotCoordinator):
        await stock.apply_import(s, erlan, rows)


async def test_export_sheets(s, clock_):
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    hp = await stock.create_item(s, coord, "cartridges", "HP 85A", min_qty=3)
    await stock.receive(s, coord, hp.id, "new", 4)
    clock_.advance(hours=1)
    await stock.take(s, erlan, hp.id, "new", 2)
    gone = await stock.create_item(s, coord, "mice", "Старая мышь")
    await stock.archive(s, coord, gone.id)
    wb = load_workbook(BytesIO(await stock_xlsx.export(s)))
    assert wb.sheetnames == ["Остатки", "Движение"]
    left = list(wb["Остатки"].iter_rows(values_only=True))
    assert list(left[0]) == stock_xlsx.STOCK_HEADERS
    assert left[1:] == [("Картриджи", "HP 85A", 2, 0, 0, 2, 3, "⚠️")]
    moves = list(wb["Движение"].iter_rows(values_only=True))
    assert list(moves[0]) == stock_xlsx.MOVE_HEADERS
    assert moves[1:] == [
        (datetime(2026, 10, 7, 15, 0), "Ерлан", "Картриджи", "HP 85A", "новые", -2, "выдача"),
        (datetime(2026, 10, 7, 14, 0), "Айжан", "Картриджи", "HP 85A", "новые", 4, "приход"),
    ]


async def test_export_neutralizes_formulas(s, clock_):
    coord = await make_staff(s, tg_id=3, name="=cmd", role="coordinator")
    item = await stock.create_item(s, coord, "other", "=HYPERLINK(\"x\")")
    await stock.receive(s, coord, item.id, "new", 1)
    wb = load_workbook(BytesIO(await stock_xlsx.export(s)))
    assert wb["Остатки"]["B2"].value == "'=HYPERLINK(\"x\")"
    assert wb["Движение"]["B2"].value == "'=cmd"
```

(Время: `clock_` стартует 2026-10-07 09:00 UTC = 14:00 в Asia/Almaty, UTC+5; выгрузка показывает местное время без tz, как текущая выгрузка заявок.)

- [ ] **Step 2: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_stock_xlsx.py tests/test_stats.py`
Expected: FAIL (`ImportError: cannot import name 'xlsx'`); `test_stats.py` пока зелёный.

- [ ] **Step 3: `src/helpdesk/xlsx_util.py` и перевод `stats/export.py` на него**

```python
from datetime import date, datetime

from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from helpdesk import clock


def safe(v: str | None) -> str | None:
    """Текст в ячейку: без управляющих символов и без исполнения как формулы."""
    if v is None:
        return None
    v = ILLEGAL_CHARACTERS_RE.sub("", v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


def naive(dt: datetime | None) -> datetime | None:
    return clock.local(dt).replace(tzinfo=None) if dt else None


def layout(ws, widths: list[int]) -> None:
    for i, width in enumerate(widths):
        ws.column_dimensions[chr(ord("A") + i)].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, datetime):
                cell.number_format = "DD.MM.YYYY HH:MM"
            elif isinstance(cell.value, date):
                cell.number_format = "DD.MM.YYYY"
```

В `stats/export.py`: удалить `_safe`, `_naive`, `_layout` и импорт `ILLEGAL_CHARACTERS_RE`; `from helpdesk.xlsx_util import layout, naive, safe`; заменить вызовы `_safe(` → `safe(`, `_naive(` → `naive(`, `_layout(` → `layout(`. `_minutes` и `_late` остаются. `test_stats.py` должен пройти без изменений.

- [ ] **Step 4: Импорт в `service.py`**

Добавить (импорт `dataclass` уже есть):

```python
@dataclass
class ImportRow:
    line: int
    category: str
    name: str
    condition: str
    qty: int
    min_qty: int | None


def _key(category: str, name: str) -> tuple[str, str]:
    return category, clean_name(name).lower()


async def preview_import(s, rows: list[ImportRow]) -> tuple[int, int]:
    """(новых позиций, строк по уже существующим) — ничего не пишет."""
    known: dict[tuple[str, str], bool] = {}
    existing_rows = 0
    for r in rows:
        k = _key(r.category, r.name)
        if k not in known:
            known[k] = await find_active(s, r.category, r.name) is not None
        existing_rows += known[k]
    return sum(1 for exists in known.values() if not exists), existing_rows


async def apply_import(s, staff: Staff, rows: list[ImportRow]) -> tuple[int, int]:
    """(создано позиций, строк с приходом). Количество прибавляется к остатку."""
    _need_coordinator(staff)
    items: dict[tuple[str, str], StockItem] = {}
    created = received = 0
    for r in rows:
        k = _key(r.category, r.name)
        item = items.get(k) or await find_active(s, r.category, r.name)
        if item is None:
            item = await create_item(s, staff, r.category, r.name, r.min_qty)
            created += 1
        elif r.min_qty is not None:
            await set_min(s, staff, item.id, r.min_qty)
        items[k] = item
        if r.qty > 0:
            await receive(s, staff, item.id, r.condition, r.qty, kind="import")
            received += 1
    return created, received
```

- [ ] **Step 5: `src/helpdesk/stock/xlsx.py`**

```python
from io import BytesIO

from openpyxl import Workbook, load_workbook
from sqlalchemy import select

from helpdesk.db import queries
from helpdesk.db.models import StockItem, StockMove
from helpdesk.stock import catalog
from helpdesk.stock import service as stock
from helpdesk.xlsx_util import layout, naive, safe

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 1000
HEADERS = ["Категория", "Название", "Состояние", "Количество", "Минимум"]
STOCK_HEADERS = ["Категория", "Позиция", "Новые", "Б/у", "После ремонта", "Всего", "Минимум", "⚠️"]
STOCK_WIDTHS = [26, 40, 9, 9, 15, 9, 10, 5]
MOVE_HEADERS = ["Дата", "Кто", "Категория", "Позиция", "Состояние", "Изменение", "Тип"]
MOVE_WIDTHS = [17, 18, 26, 40, 15, 11, 13]
KIND_RU = {"in": "приход", "out": "выдача", "fix": "исправление", "import": "загрузка"}


class BadFile(Exception):
    """Файл целиком не принят; текст — причина для пользователя."""


def _int(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _shown(v) -> str:
    return "" if v is None else str(v)


def _blank(v) -> bool:
    return v is None or str(v).strip() == ""


def parse(data: bytes) -> tuple[list[stock.ImportRow], list[str]]:
    if len(data) > MAX_BYTES:
        raise BadFile("Файл больше 2 МБ.")
    try:
        wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # не xlsx, битый архив и т.п.
        raise BadFile("Не удалось прочитать файл — нужен .xlsx.") from e
    raw = []
    try:
        ws = wb.worksheets[0]
        for line, values in enumerate(ws.iter_rows(min_row=2, max_col=5, values_only=True), 2):
            values = (tuple(values) + (None,) * 5)[:5]
            if all(_blank(v) for v in values):
                continue
            raw.append((line, values))
            if len(raw) > MAX_ROWS:
                raise BadFile(f"Больше {MAX_ROWS} строк — разбейте файл на части.")
    finally:
        wb.close()

    rows, errors = [], []
    top = stock.MAX_LEVEL
    for line, (cat, name, cond, qty, mn) in raw:
        problems = []
        category = catalog.parse_category(cat)
        if category is None:
            problems.append(f"неизвестная категория «{_shown(cat)}»")
        clean = stock.clean_name(_shown(name))
        if not 1 <= len(clean) <= 200:
            problems.append("пустое или слишком длинное название")
        condition = catalog.parse_condition(cond)
        if condition is None:
            problems.append(f"неизвестное состояние «{_shown(cond)}»")
        q = _int(qty)
        if q is None or not 0 <= q <= top:
            problems.append(f"количество «{_shown(qty)}» — нужно целое от 0 до {top}")
        m = None
        if not _blank(mn):
            m = _int(mn)
            if m is None or not 0 <= m <= top:
                problems.append(f"минимум «{_shown(mn)}» — нужно целое от 0 до {top}")
        if problems:
            errors.append(f"строка {line}: " + "; ".join(problems))
        else:
            rows.append(stock.ImportRow(line, category, clean, condition, q, m))
    return rows, errors


def template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Склад"
    ws.append(HEADERS)
    layout(ws, [26, 40, 15, 12, 10])
    h = wb.create_sheet("Справка")
    h.append(["Как заполнять лист «Склад»"])
    h.append(["Одна строка — одна позиция в одном состоянии. Первую строку (заголовки) не трогайте."])
    h.append(["Повторная загрузка прибавляет количество к остатку — не загружайте один файл дважды."])
    h.append(["Пустое состояние = новые. Минимум можно не указывать."])
    h.append([])
    h.append(["Категории:"])
    for code in catalog.CATEGORIES:
        h.append([catalog.plain(code)])
    h.append([])
    h.append(["Состояния:"])
    for label in catalog.CONDITIONS.values():
        h.append([label])
    h.append([])
    h.append(["Пример строк:"])
    h.append(HEADERS)
    h.append(["Картриджи", "Картридж HP 85A", "новые", 5, 3])
    h.append(["Мыши", "Мышь USB Logitech", "б/у", 2, None])
    h.column_dimensions["A"].width = 90
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def export(s) -> bytes:
    order = {code: i for i, code in enumerate(catalog.CATEGORIES)}
    items = (await s.scalars(select(StockItem).where(StockItem.archived.is_(False)))).all()
    items = sorted(items, key=lambda i: (order.get(i.category, 99), i.name.lower(), i.id))
    wb = Workbook()
    ws = wb.active
    ws.title = "Остатки"
    ws.append(STOCK_HEADERS)
    for r in await stock.rows_for(s, list(items)):
        lv = r.levels
        ws.append([
            catalog.plain(r.item.category),
            safe(r.item.name),
            lv["new"],
            lv["used"],
            lv["repaired"],
            r.total,
            r.item.min_qty,
            "⚠️" if r.low else None,
        ])
    layout(ws, STOCK_WIDTHS)

    names = await queries.staff_names(s)
    q = (
        select(StockMove, StockItem)
        .join(StockItem, StockItem.id == StockMove.item_id)
        .order_by(StockMove.id.desc())
    )
    wm = wb.create_sheet("Движение")
    wm.append(MOVE_HEADERS)
    for m, item in (await s.execute(q)).all():
        wm.append([
            naive(m.created_at),
            safe(names.get(m.staff_id)),
            catalog.plain(item.category) if item.category in catalog.CATEGORIES else item.category,
            safe(item.name),
            catalog.CONDITIONS.get(m.condition, m.condition),
            m.delta,
            KIND_RU.get(m.kind, m.kind),
        ])
    layout(wm, MOVE_WIDTHS)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
```


- [ ] **Step 6: Проверить и закоммитить**

Run: `uv run ruff format src tests && make check` — Expected: PASS (включая неизменённый `test_stats.py`).

```bash
git add src/helpdesk/xlsx_util.py src/helpdesk/stats/export.py src/helpdesk/stock tests/test_stock_xlsx.py
git commit -m "feat(stock): xlsx template, validated import that adds up, stock and movement export"
```

---

### Task 4: Меню «📦 Склад», просмотр, поиск, «Взять»

**Files:**
- Create: `src/helpdesk/stock/render.py`, `src/helpdesk/telegram/stock_handlers.py`
- Modify: `src/helpdesk/telegram/menu.py`, `src/helpdesk/telegram/handlers.py` (`setup`), `tests/test_menu.py`
- Test: `tests/test_stock_handlers.py`

**Interfaces:**
- Consumes: сервис (задачи 1–2): `category_totals`, `items_in`, `search`, `get_item`, `row`, `take`, `TakeResult`, `Archived`, `is_coordinator`, `ItemRow`; `catalog`.
- Produces: `menu.STOCK = "📦 Склад"` (в `MENU_TEXTS`, строка `[STOCK]` сразу после `[NEW_TASK]`); `render.qty_line(levels)`, `render.item_label(row)`, `render.card(row)`, `render.home_buttons(totals, coord)`, `render.category_buttons(rows, category, page, pages)`, `render.card_buttons(row, coord)`; в `stock_handlers`: `router`, `StockForm` (состояния `take_qty`, `in_qty`, `fix_qty`, `min_qty`, `new_name`, `new_min`, `search`, `import_file`, `import_confirm` — объявить все сразу), `parse_int(text, lo, hi) -> int | None`, `ONLY_COORD = "Только для координатора"`, `async _deny(cq, staff) -> bool` (alert и True, если не координатор), обработчики `stock_home`, `cb_home`, `cb_category`, `cb_item`, `cb_find`, `got_search`, `cb_take`, `cb_take_cond`, `cb_take_qty`, `got_take_qty`, `cb_cancel`, `leave_dialog`, `stray_input`, `stale`.

- [ ] **Step 1: Тесты**

В `tests/test_menu.py` ожидаемое меню специалиста:

```python
    assert menu.menu_rows(_staff()) == [
        ["📋 Мои заявки", "📂 Открытые"],
        ["➕ Новая задача"],
        ["📦 Склад"],
        ["🔍 Поиск", "📊 Статистика"],
        ["📥 Выгрузка", "🔴 Уйти со смены"],
    ]
```

и в `test_shift_button_follows_state_and_admin_row`: `rows[4][1]` вместо `rows[3][1]`.

Create `tests/test_stock_handlers.py`:

```python
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from helpdesk.stock import service as stock
from helpdesk.telegram import menu, stock_handlers
from helpdesk.telegram.stock_handlers import StockForm
from tests.factories import make_staff, payloads


class Msg:
    def __init__(self, text=None, **kw):
        self.text = text
        self.document = kw.get("document")
        self.answers = []  # (text, reply_markup)
        self.edits = []
        self.docs = []

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))
        return SimpleNamespace(message_id=1)

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))

    async def edit_reply_markup(self, **kw):
        pass

    async def answer_document(self, document, caption=None, **kw):
        self.docs.append((document, caption))


class Cq:
    def __init__(self, data):
        self.data = data
        self.message = Msg()
        self.alerts = []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append(text)


def kb(markup_):
    if markup_ is None or not hasattr(markup_, "inline_keyboard"):
        return None
    return [[(b.text, b.callback_data) for b in row] for row in markup_.inline_keyboard]


def datas(markup_):
    return [d for row in kb(markup_) or [] for _, d in row]


@pytest.fixture
def state():
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))


async def _setup(s):
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    hp = await stock.create_item(s, coord, "cartridges", "Картридж HP 85A", min_qty=3)
    await stock.receive(s, coord, hp.id, "new", 7)
    return coord, erlan, hp


def test_parse_int():
    assert stock_handlers.parse_int(" 12 ", 1, 1000) == 12
    for bad in ("0", "1001", "1.5", "-1", "два", ""):
        assert stock_handlers.parse_int(bad, 1, 1000) is None, bad


async def test_home_specialist_vs_coordinator(s, clock_, state):
    coord, erlan, _ = await _setup(s)
    m = Msg(menu.STOCK)
    await stock_handlers.stock_home(m, state, s, erlan)
    text, markup_ = m.answers[-1]
    assert text == "📦 Склад"
    rows = kb(markup_)
    assert ("🖋 Картриджи · 7", "sk_cat:cartridges:0") in [b for row in rows for b in row]
    assert ("🖱 Мыши · 0", "sk_cat:mice:0") in [b for row in rows for b in row]
    assert rows[-1] == [("🔍 Найти", "sk_find"), ("📥 Склад.xlsx", "sk_xlsx")]
    m = Msg(menu.STOCK)
    await stock_handlers.stock_home(m, state, s, coord)
    assert kb(m.answers[-1][1])[-1] == [("➕ Новая позиция", "sk_new"), ("📤 Загрузить xlsx", "sk_upl")]


async def test_category_and_card(s, clock_, state):
    coord, erlan, hp = await _setup(s)
    cq = Cq("sk_cat:cartridges:0")
    await stock_handlers.cb_category(cq, s)
    text, markup_ = cq.message.edits[-1]
    assert text == "🖋 Картриджи"
    assert kb(markup_) == [
        [("Картридж HP 85A — новые 7", f"sk_item:{hp.id}")],
        [("⬅️ Категории", "sk_home")],
    ]
    cq = Cq(f"sk_item:{hp.id}")
    await stock_handlers.cb_item(cq, s, erlan)
    text, markup_ = cq.message.edits[-1]
    assert text == (
        "Картридж HP 85A\n🖋 Картриджи\n\nновые: 7\nб/у: 0\nпосле ремонта: 0\nминимум: 3"
    )
    assert kb(markup_) == [[("➖ Взять", f"sk_take:{hp.id}")], [("⬅️ Назад", "sk_cat:cartridges:0")]]
    cq = Cq(f"sk_item:{hp.id}")
    await stock_handlers.cb_item(cq, s, coord)
    assert datas(cq.message.edits[-1][1]) == [
        f"sk_take:{hp.id}", f"sk_in:{hp.id}", f"sk_fix:{hp.id}", f"sk_min:{hp.id}",
        f"sk_arch:{hp.id}", "sk_cat:cartridges:0",
    ]


async def test_bad_category_code_is_stale(s, clock_):
    cq = Cq("sk_cat:nope:0")
    await stock_handlers.cb_category(cq, s)
    assert cq.alerts == ["Кнопка устарела"]


async def test_take_flow_single_condition_and_shortage(s, clock_, state):
    coord, erlan, hp = await _setup(s)
    cq = Cq(f"sk_take:{hp.id}")
    await stock_handlers.cb_take(cq, state, s, erlan)  # одно состояние — сразу количество
    assert await state.get_state() == StockForm.take_qty.state
    text, markup_ = cq.message.answers[-1]
    assert text == "Картридж HP 85A (новый), есть 7. Сколько взять?"
    assert kb(markup_) == [[("1", "sk_tq:1"), ("2", "sk_tq:2"), ("3", "sk_tq:3"),
                            ("Другое", "sk_tq:other")]]
    await stock.take(s, coord, hp.id, "new", 6)  # кто-то успел раньше: осталась 1
    cq = Cq("sk_tq:2")
    await stock_handlers.cb_take_qty(cq, state, s, erlan)
    assert cq.message.answers[0][0] == "Осталось только 1."
    assert await state.get_state() == StockForm.take_qty.state
    m = Msg("1")
    await stock_handlers.got_take_qty(m, state, s, erlan)
    assert m.answers[-1][0] == "✅ Взято: Картридж HP 85A ×1 (новый). Осталось 0."
    assert await state.get_state() is None
    assert await payloads(s, "wa") == []


async def test_take_flow_choose_condition_and_other(s, clock_, state):
    coord, erlan, hp = await _setup(s)
    await stock.receive(s, coord, hp.id, "used", 2)
    cq = Cq(f"sk_take:{hp.id}")
    await stock_handlers.cb_take(cq, state, s, erlan)
    assert kb(cq.message.answers[-1][1]) == [[
        ("новые · 7", f"sk_tc:{hp.id}:new"), ("б/у · 2", f"sk_tc:{hp.id}:used"),
    ]]
    cq = Cq(f"sk_tc:{hp.id}:used")
    await stock_handlers.cb_take_cond(cq, state, s, erlan)
    assert kb(cq.message.answers[-1][1]) == [[("1", "sk_tq:1"), ("2", "sk_tq:2"),
                                              ("Другое", "sk_tq:other")]]
    cq = Cq("sk_tq:other")
    await stock_handlers.cb_take_qty(cq, state, s, erlan)
    assert cq.message.answers[-1][0] == "Введите число от 1 до 2."
    bad = Msg("пять")
    await stock_handlers.got_take_qty(bad, state, s, erlan)
    assert bad.answers[-1][0] == "Введите число от 1 до 2."
    ok = Msg("2")
    await stock_handlers.got_take_qty(ok, state, s, erlan)
    assert ok.answers[-1][0] == "✅ Взято: Картридж HP 85A ×2 (б/у). Осталось 0."


async def test_take_nothing_and_archived(s, clock_, state):
    coord, erlan, hp = await _setup(s)
    empty = await stock.create_item(s, coord, "mice", "Мышь")
    cq = Cq(f"sk_take:{empty.id}")
    await stock_handlers.cb_take(cq, state, s, erlan)
    assert cq.alerts == ["Нет в наличии"]
    cq = Cq(f"sk_take:{hp.id}")
    await stock_handlers.cb_take(cq, state, s, erlan)
    await stock.archive(s, coord, hp.id)
    m = Msg("1")
    await stock_handlers.got_take_qty(m, state, s, erlan)
    assert m.answers[-1][0] == "Позиция в архиве."
    assert await state.get_state() is None


async def test_search(s, clock_, state):
    coord, erlan, hp = await _setup(s)
    cq = Cq("sk_find")
    await stock_handlers.cb_find(cq, state)
    assert await state.get_state() == StockForm.search.state
    m = Msg("85a")
    await stock_handlers.got_search(m, state, s, erlan)
    assert kb(m.answers[-1][1]) == [[("Картридж HP 85A — новые 7", f"sk_item:{hp.id}")]]
    assert await state.get_state() is None
    await stock_handlers.cb_find(Cq("sk_find"), state)
    m = Msg("hdmi")
    await stock_handlers.got_search(m, state, s, erlan)
    assert m.answers[-1][0] == "Ничего не найдено."


def _message(**kw) -> Message:
    return Message.model_validate({
        "message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"},
        "from": {"id": 1, "is_bot": False, "first_name": "E"}, **kw,
    })


async def _matched_names(msg, raw_state, staff):
    out = []
    for h in stock_handlers.router.message.handlers:
        ok, _ = await h.check(msg, staff=staff, raw_state=raw_state)
        if ok:
            out.append(h.callback.__name__)
    return out


async def test_stock_dialog_routing():
    staff = SimpleNamespace(status="active", is_admin=False, role="helpdesk")
    photo = [{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}]
    for st in StockForm.__all_states__:
        for text in (menu.MY, "/my", menu.NEW_TASK):
            assert await _matched_names(_message(text=text), st.state, staff) == [
                "leave_dialog"
            ], (st, text)
        assert await _matched_names(_message(photo=photo), st.state, staff), st
        assert await _matched_names(_message(text=menu.CANCEL), st.state, staff) == [], st
        assert (await _matched_names(_message(text=menu.STOCK), st.state, staff))[0] == (
            "stock_home"
        ), st


async def test_stale_buttons(s, clock_, state):
    cq = Cq("sk_tq:1")  # нет диалога
    await stock_handlers.stale(cq)
    assert cq.alerts == ["Диалог уже завершён"]
```

- [ ] **Step 2: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_stock_handlers.py tests/test_menu.py`
Expected: FAIL (`ImportError: cannot import name 'stock_handlers'`, меню без «📦 Склад»).

- [ ] **Step 3: Меню (`menu.py`)**

`STOCK = "📦 Склад"`; добавить в `MENU_TEXTS`; в `menu_rows` после `[NEW_TASK]` строка `[STOCK]`.

- [ ] **Step 4: `src/helpdesk/stock/render.py`**

```python
from helpdesk.stock.catalog import CATEGORIES, CONDITIONS
from helpdesk.stock.service import ItemRow


def qty_line(levels: dict[str, int]) -> str:
    parts = [f"{CONDITIONS[c]} {n}" for c, n in levels.items() if n]
    return " · ".join(parts) or "нет"


def item_label(row: ItemRow) -> str:
    return f"{row.item.name} — {qty_line(row.levels)}" + (" ⚠️" if row.low else "")


def card(row: ItemRow) -> str:
    item = row.item
    lines = [item.name, CATEGORIES[item.category], ""]
    lines += [f"{CONDITIONS[c]}: {n}" for c, n in row.levels.items()]
    if item.min_qty is not None:
        lines.append(f"минимум: {item.min_qty}" + (" ⚠️" if row.low else ""))
    return "\n".join(lines)


def home_buttons(totals: dict[str, int], coord: bool):
    cats = [(f"{CATEGORIES[c]} · {totals.get(c, 0)}", f"sk_cat:{c}:0") for c in CATEGORIES]
    rows = [cats[i : i + 2] for i in range(0, len(cats), 2)]
    rows.append([("🔍 Найти", "sk_find"), ("📥 Склад.xlsx", "sk_xlsx")])
    if coord:
        rows.append([("➕ Новая позиция", "sk_new"), ("📤 Загрузить xlsx", "sk_upl")])
    return rows


def category_buttons(rows: list[ItemRow], category: str, page: int, pages: int):
    out = [[(item_label(r), f"sk_item:{r.item.id}")] for r in rows]
    nav = []
    if page > 0:
        nav.append(("◀️", f"sk_cat:{category}:{page - 1}"))
    if page < pages - 1:
        nav.append(("▶️", f"sk_cat:{category}:{page + 1}"))
    if nav:
        out.append(nav)
    out.append([("⬅️ Категории", "sk_home")])
    return out


def card_buttons(row: ItemRow, coord: bool):
    i = row.item.id
    out = []
    if row.total > 0:
        out.append([("➖ Взять", f"sk_take:{i}")])
    if coord:
        out.append([("➕ Приход", f"sk_in:{i}"), ("✏️ Исправить", f"sk_fix:{i}")])
        out.append([("🔔 Минимум", f"sk_min:{i}"), ("🗄 В архив", f"sk_arch:{i}")])
    out.append([("⬅️ Назад", f"sk_cat:{row.item.category}:0")])
    return out
```

- [ ] **Step 5: `src/helpdesk/telegram/stock_handlers.py`**

```python
from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from helpdesk.db.models import Staff
from helpdesk.stock import render
from helpdesk.stock import service as stock
from helpdesk.stock.catalog import CATEGORIES, CONDITION_ONE, CONDITIONS
from helpdesk.telegram import menu
from helpdesk.telegram.filters import ID_DATA, IsActive, arg_id
from helpdesk.telegram.gateway import markup

router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

ONLY_COORD = "Только для координатора"
COND_RE = "(new|used|repaired)"
NOT_CANCEL = F.text != menu.CANCEL


class StockForm(StatesGroup):
    take_qty = State()
    in_qty = State()
    fix_qty = State()
    min_qty = State()
    new_name = State()
    new_min = State()
    search = State()
    import_file = State()
    import_confirm = State()


def leaves_dialog(text: str | None) -> bool:
    """Команда или кнопка меню (кроме «Отмена» и «Склад») посреди диалога склада."""
    if not text:
        return False
    return text.startswith("/") or (
        text in menu.MENU_TEXTS and text not in (menu.CANCEL, menu.STOCK)
    )


NOT_LEAVING = ~F.text.func(leaves_dialog)


def parse_int(text: str | None, lo: int, hi: int) -> int | None:
    t = (text or "").strip()
    if not t.isdigit():
        return None
    n = int(t)
    return n if lo <= n <= hi else None


async def _deny(cq: CallbackQuery, staff: Staff) -> bool:
    if stock.is_coordinator(staff):
        return False
    await cq.answer(ONLY_COORD, show_alert=True)
    return True


async def _drop_markup(cq: CallbackQuery) -> None:
    try:
        await cq.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


async def _edit(cq: CallbackQuery, text: str, buttons) -> None:
    try:
        await cq.message.edit_text(text, reply_markup=markup(buttons))
    except TelegramBadRequest:  # «message is not modified» и т.п.
        pass


async def _home(s, staff: Staff):
    totals = await stock.category_totals(s)
    return "📦 Склад", render.home_buttons(totals, stock.is_coordinator(staff))


@router.message(F.text == menu.STOCK, IsActive())
async def stock_home(message: Message, state: FSMContext, s, staff: Staff):
    await state.clear()
    text, buttons = await _home(s, staff)
    await message.answer(text, reply_markup=markup(buttons))


@router.message(StateFilter(StockForm), F.text.func(leaves_dialog))
async def leave_dialog(message: Message, state: FSMContext):
    await state.clear()
    raise SkipHandler()


@router.callback_query(F.data == "sk_home", IsActive())
async def cb_home(cq: CallbackQuery, s, staff: Staff):
    await _edit(cq, *await _home(s, staff))
    await cq.answer()


@router.callback_query(F.data.regexp(r"^sk_cat:[a-z]{1,24}:\d{1,4}$"), IsActive())
async def cb_category(cq: CallbackQuery, s):
    _, code, page = cq.data.split(":")
    if code not in CATEGORIES:
        await cq.answer("Кнопка устарела", show_alert=True)
        return
    rows, page, pages = await stock.items_in(s, code, int(page))
    title = CATEGORIES[code] + (f" · стр. {page + 1}/{pages}" if pages > 1 else "")
    if not rows:
        title += "\n\nПока пусто."
    await _edit(cq, title, render.category_buttons(rows, code, page, pages))
    await cq.answer()


async def _active_row(cq: CallbackQuery, s, item_id: int):
    item = await stock.get_item(s, item_id)
    if item is None or item.archived:
        await cq.answer("Позиция в архиве", show_alert=True)
        return None
    return await stock.row(s, item)


@router.callback_query(F.data.regexp(ID_DATA.format("sk_item")), IsActive())
async def cb_item(cq: CallbackQuery, s, staff: Staff):
    row = await _active_row(cq, s, arg_id(cq))
    if row is None:
        return
    await _edit(cq, render.card(row), render.card_buttons(row, stock.is_coordinator(staff)))
    await cq.answer()


@router.callback_query(F.data == "sk_find", IsActive())
async def cb_find(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(StockForm.search)
    await cq.message.answer("Введите часть названия:", reply_markup=menu.cancel_menu())
    await cq.answer()


@router.message(StockForm.search, IsActive(), F.text.func(menu.is_client_reply_text))
async def got_search(message: Message, state: FSMContext, s, staff: Staff):
    await state.clear()
    rows = await stock.search(s, message.text)
    if not rows:
        await message.answer("Ничего не найдено.", reply_markup=menu.main_menu(staff))
        return
    await message.answer("Найдено:", reply_markup=menu.main_menu(staff))
    buttons = [[(render.item_label(r), f"sk_item:{r.item.id}")] for r in rows]
    await message.answer("Выберите позицию:", reply_markup=markup(buttons))


# ── Взять ──────────────────────────────────────────────────────────────


async def _ask_take_qty(message, state: FSMContext, row, condition: str) -> None:
    available = row.levels[condition]
    await state.clear()
    await state.set_state(StockForm.take_qty)
    await state.update_data(item_id=row.item.id, condition=condition)
    buttons = [[(str(n), f"sk_tq:{n}") for n in (1, 2, 3) if n <= available]]
    buttons[0].append(("Другое", "sk_tq:other"))
    await message.answer(
        f"{row.item.name} ({CONDITION_ONE[condition]}), есть {available}. Сколько взять?",
        reply_markup=markup(buttons),
    )


@router.callback_query(F.data.regexp(ID_DATA.format("sk_take")), IsActive())
async def cb_take(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    row = await _active_row(cq, s, arg_id(cq))
    if row is None:
        return
    have = [c for c in CONDITIONS if row.levels[c] > 0]
    if not have:
        await cq.answer("Нет в наличии", show_alert=True)
        return
    if len(have) == 1:
        await _ask_take_qty(cq.message, state, row, have[0])
    else:
        buttons = [[(f"{CONDITIONS[c]} · {row.levels[c]}", f"sk_tc:{row.item.id}:{c}") for c in have]]
        await cq.message.answer(f"{row.item.name}: какое состояние?", reply_markup=markup(buttons))
    await cq.answer()


@router.callback_query(F.data.regexp(rf"^sk_tc:\d{{1,9}}:{COND_RE}$"), IsActive())
async def cb_take_cond(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    _, item_id, condition = cq.data.split(":")
    row = await _active_row(cq, s, int(item_id))
    if row is None:
        return
    await _drop_markup(cq)
    if row.levels[condition] == 0:
        await cq.message.answer("Этого уже нет в наличии.")
    else:
        await _ask_take_qty(cq.message, state, row, condition)
    await cq.answer()


async def _do_take(message, state: FSMContext, s, staff: Staff, qty: int) -> None:
    data = await state.get_data()
    try:
        r = await stock.take(s, staff, data["item_id"], data["condition"], qty)
    except stock.Archived:
        await state.clear()
        await message.answer("Позиция в архиве.", reply_markup=menu.main_menu(staff))
        return
    if not r.ok:
        if r.available == 0:
            await state.clear()
            await message.answer("Уже закончилось.", reply_markup=menu.main_menu(staff))
        else:
            await message.answer(f"Осталось только {r.available}.")
        return
    await state.clear()
    item = await stock.get_item(s, data["item_id"])
    await message.answer(
        f"✅ Взято: {item.name} ×{qty} ({CONDITION_ONE[data['condition']]}). Осталось {r.left}.",
        reply_markup=menu.main_menu(staff),
    )


@router.callback_query(StockForm.take_qty, F.data.regexp(r"^sk_tq:(\d{1,4}|other)$"), IsActive())
async def cb_take_qty(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    arg = cq.data.split(":")[1]
    if arg == "other":
        data = await state.get_data()
        have = (await stock.levels(s, data["item_id"]))[data["condition"]]
        await cq.message.answer(f"Введите число от 1 до {max(have, 1)}.", reply_markup=menu.cancel_menu())
    else:
        await _do_take(cq.message, state, s, staff, int(arg))
    await cq.answer()


@router.message(StockForm.take_qty, IsActive(), F.text.func(menu.is_client_reply_text))
async def got_take_qty(message: Message, state: FSMContext, s, staff: Staff):
    data = await state.get_data()
    have = (await stock.levels(s, data["item_id"]))[data["condition"]]
    qty = parse_int(message.text, 1, max(have, 1))
    if qty is None:
        await message.answer(f"Введите число от 1 до {max(have, 1)}.")
        return
    await _do_take(message, state, s, staff, qty)


@router.callback_query(F.data == "sk_cancel", IsActive())
async def cb_cancel(cq: CallbackQuery, state: FSMContext, staff: Staff):
    await state.clear()
    await _drop_markup(cq)
    await cq.message.answer("Отменено.", reply_markup=menu.main_menu(staff))
    await cq.answer()


# Обработчики задач 5–6 вставлять ВЫШЕ этой строки: stray_input и stale — последние.


@router.message(StateFilter(StockForm), IsActive(), NOT_CANCEL, NOT_LEAVING)
async def stray_input(message: Message):
    """Лишний ввод посреди диалога склада не должен уйти пользователю WhatsApp."""
    await message.answer("Используйте кнопки выше или «✖️ Отмена».")


@router.callback_query(F.data.startswith("sk_"))
async def stale(cq: CallbackQuery):
    await cq.answer("Диалог уже завершён", show_alert=True)
```

Примечание к тесту `test_take_flow_single_condition_and_shortage`: после «Осталось только 1.» кнопки `sk_tq:*` прежние; `got_take_qty("1")` проходит. Если `archive` в `test_take_nothing_and_archived` вызывается при открытом диалоге — `stock.levels` для архивной позиции вернёт уровни, `parse_int` пропустит «1», и `_do_take` получит `Archived` — ответ «Позиция в архиве.».

- [ ] **Step 6: Подключить роутер (`handlers.py::setup`)**

```python
    for r in (task_handlers.router, stock_handlers.router, router):
        r.message.outer_middleware(mw)
        r.callback_query.outer_middleware(mw)
    dp.include_router(task_handlers.router)  # раньше общего: у общего есть catch-all
    dp.include_router(stock_handlers.router)
    dp.include_router(router)
```

Импорт: `from helpdesk.telegram import menu, stock_handlers, task_handlers, voice`.

- [ ] **Step 7: Проверить и закоммитить**

Run: `uv run ruff format src tests && make check` — Expected: PASS (включая `test_menu_input_in_dialog_only_matched_by_leave_dialog` задач: «📦 Склад» в `MENU_TEXTS` → выводит из диалога задачи).

```bash
git add src/helpdesk/stock/render.py src/helpdesk/telegram/stock_handlers.py src/helpdesk/telegram/menu.py src/helpdesk/telegram/handlers.py tests/test_menu.py tests/test_stock_handlers.py
git commit -m "feat(stock): stock menu, categories, item cards, search and take dialog"
```

---

### Task 5: Действия координатора — приход, исправление, минимум, архив, новая позиция

**Files:**
- Modify: `src/helpdesk/telegram/stock_handlers.py` (вставлять перед `stray_input`)
- Test: `tests/test_stock_handlers.py`

**Interfaces:**
- Consumes: `StockForm`, `parse_int`, `_deny`, `_active_row`, `_drop_markup`, `_edit`, `ONLY_COORD`, `COND_RE` (задача 4); сервис `receive`, `set_level`, `set_min`, `archive`, `create_item`, `find_active`, `DuplicateName`, `NotCoordinator`, `Archived`, `MAX_IN`, `MAX_LEVEL`.
- Produces: обработчики `cb_in`, `cb_in_cond`, `got_in_qty`, `cb_fix`, `cb_fix_cond`, `got_fix_qty`, `cb_min`, `got_min`, `cb_min_clear`, `cb_arch`, `cb_arch_ok`, `cb_new`, `cb_new_cat`, `got_new_name`, `got_new_min`, `cb_new_skip_min`.

- [ ] **Step 1: Тесты** — в конец `tests/test_stock_handlers.py`:

```python
async def test_receive_and_fix(s, clock_, state):
    coord, _, hp = await _setup(s)
    cq = Cq(f"sk_in:{hp.id}")
    await stock_handlers.cb_in(cq, state, s, coord)
    assert kb(cq.message.answers[-1][1]) == [[
        ("новые", f"sk_ic:{hp.id}:new"), ("б/у", f"sk_ic:{hp.id}:used"),
        ("после ремонта", f"sk_ic:{hp.id}:repaired"),
    ]]
    await stock_handlers.cb_in_cond(Cq(f"sk_ic:{hp.id}:repaired"), state, s, coord)
    assert await state.get_state() == StockForm.in_qty.state
    bad = Msg("1001")
    await stock_handlers.got_in_qty(bad, state, s, coord)
    assert bad.answers[-1][0] == "Введите число от 1 до 1000."
    m = Msg("2")
    await stock_handlers.got_in_qty(m, state, s, coord)
    assert m.answers[-1][0] == "➕ Картридж HP 85A +2 (после ремонта). Теперь 2."
    await stock_handlers.cb_fix(Cq(f"sk_fix:{hp.id}"), state, s, coord)
    await stock_handlers.cb_fix_cond(Cq(f"sk_fc:{hp.id}:new"), state, s, coord)
    assert await state.get_state() == StockForm.fix_qty.state
    m = Msg("0")
    await stock_handlers.got_fix_qty(m, state, s, coord)
    assert m.answers[-1][0] == "✏️ Картридж HP 85A (новый): теперь 0."
    assert await stock.levels(s, hp.id) == {"new": 0, "used": 0, "repaired": 2}


async def test_min_and_archive(s, clock_, state):
    coord, _, hp = await _setup(s)
    await stock_handlers.cb_min(Cq(f"sk_min:{hp.id}"), state, s, coord)
    m = Msg("5")
    await stock_handlers.got_min(m, state, s, coord)
    assert m.answers[-1][0] == "🔔 Минимум для «Картридж HP 85A»: 5."
    await stock_handlers.cb_min(Cq(f"sk_min:{hp.id}"), state, s, coord)
    cq = Cq("sk_minx")
    await stock_handlers.cb_min_clear(cq, state, s, coord)
    assert cq.message.answers[-1][0] == "🔔 Минимум для «Картридж HP 85A» убран."
    assert (await stock.get_item(s, hp.id)).min_qty is None
    cq = Cq(f"sk_arch:{hp.id}")
    await stock_handlers.cb_arch(cq, s, coord)
    assert kb(cq.message.answers[-1][1]) == [[("Да, в архив", f"sk_archok:{hp.id}"), ("Отмена", "sk_cancel")]]
    cq = Cq(f"sk_archok:{hp.id}")
    await stock_handlers.cb_arch_ok(cq, s, coord)
    assert cq.message.edits[-1][0] == "🗄 Картридж HP 85A — в архиве."
    assert (await stock.get_item(s, hp.id)).archived


async def test_new_item_flow_and_duplicate(s, clock_, state):
    coord, _, hp = await _setup(s)
    cq = Cq("sk_new")
    await stock_handlers.cb_new(cq, state, coord)
    assert ("🖱 Мыши", "sk_nc:mice") in [b for row in kb(cq.message.answers[-1][1]) for b in row]
    await stock_handlers.cb_new_cat(Cq("sk_nc:cartridges"), state, coord)
    assert await state.get_state() == StockForm.new_name.state
    dup = Msg("картридж hp 85a")
    await stock_handlers.got_new_name(dup, state, s, coord)
    assert dup.answers[-1][0] == "Уже есть: Картридж HP 85A"
    assert kb(dup.answers[-1][1]) == [[("Открыть", f"sk_item:{hp.id}")]]
    assert await state.get_state() is None
    await stock_handlers.cb_new_cat(Cq("sk_nc:cartridges"), state, coord)
    long = Msg("x" * 201)
    await stock_handlers.got_new_name(long, state, s, coord)
    assert long.answers[-1][0] == "Название от 1 до 200 символов."
    await stock_handlers.got_new_name(Msg("Canon 728"), state, s, coord)
    assert await state.get_state() == StockForm.new_min.state
    m = Msg("2")
    await stock_handlers.got_new_min(m, state, s, coord)
    item = await stock.find_active(s, "cartridges", "canon 728")
    assert item.min_qty == 2
    text, markup_ = m.answers[-1]
    assert text == "Позиция «Canon 728» создана. Что пришло?"
    assert datas(markup_) == [
        f"sk_ic:{item.id}:new", f"sk_ic:{item.id}:used", f"sk_ic:{item.id}:repaired",
        f"sk_item:{item.id}",
    ]
    await stock_handlers.cb_new_cat(Cq("sk_nc:mice"), state, coord)
    await stock_handlers.got_new_name(Msg("Мышь"), state, s, coord)
    cq = Cq("sk_skipmin")
    await stock_handlers.cb_new_skip_min(cq, state, s, coord)
    assert (await stock.find_active(s, "mice", "Мышь")).min_qty is None


async def test_coordinator_callbacks_rejected_for_specialist(s, clock_, state):
    _, erlan, hp = await _setup(s)
    calls = [
        (stock_handlers.cb_in, Cq(f"sk_in:{hp.id}"), (state, s, erlan)),
        (stock_handlers.cb_in_cond, Cq(f"sk_ic:{hp.id}:new"), (state, s, erlan)),
        (stock_handlers.cb_fix, Cq(f"sk_fix:{hp.id}"), (state, s, erlan)),
        (stock_handlers.cb_fix_cond, Cq(f"sk_fc:{hp.id}:new"), (state, s, erlan)),
        (stock_handlers.cb_min, Cq(f"sk_min:{hp.id}"), (state, s, erlan)),
        (stock_handlers.cb_arch, Cq(f"sk_arch:{hp.id}"), (s, erlan)),
        (stock_handlers.cb_arch_ok, Cq(f"sk_archok:{hp.id}"), (s, erlan)),
        (stock_handlers.cb_new, Cq("sk_new"), (state, erlan)),
        (stock_handlers.cb_new_cat, Cq("sk_nc:mice"), (state, erlan)),
    ]
    for fn, cq, args in calls:
        await fn(cq, *args)
        assert cq.alerts == ["Только для координатора"], fn.__name__
        assert cq.message.answers == [] and cq.message.edits == [], fn.__name__
    assert await state.get_state() is None
    assert not (await stock.get_item(s, hp.id)).archived
    # текстовый ввод в «координаторском» состоянии от специалиста тоже не проходит
    await state.set_state(StockForm.in_qty)
    await state.update_data(item_id=hp.id, condition="new")
    m = Msg("5")
    await stock_handlers.got_in_qty(m, state, s, erlan)
    assert m.answers[-1][0] == "Только для координатора."
    assert (await stock.levels(s, hp.id))["new"] == 7
```

- [ ] **Step 2: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_stock_handlers.py`
Expected: FAIL (`AttributeError: ... 'cb_in'`).

- [ ] **Step 3: Реализация** — в `stock_handlers.py` перед комментарием «Обработчики задач 5–6 вставлять ВЫШЕ…»:

```python
# ── Координатор ────────────────────────────────────────────────────────


def _cond_buttons(prefix: str, item_id: int):
    return [[(label, f"{prefix}:{item_id}:{c}") for c, label in CONDITIONS.items()]]


async def _coord_text(message: Message, state: FSMContext, staff: Staff) -> bool:
    """Текстовый шаг координаторского диалога: специалиста не пускаем."""
    if stock.is_coordinator(staff):
        return True
    await state.clear()
    await message.answer(f"{ONLY_COORD}.", reply_markup=menu.main_menu(staff))
    return False


@router.callback_query(F.data.regexp(ID_DATA.format("sk_in")), IsActive())
async def cb_in(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    if await _deny(cq, staff) or (row := await _active_row(cq, s, arg_id(cq))) is None:
        return
    await state.clear()
    await cq.message.answer(f"{row.item.name}: что пришло?", reply_markup=markup(_cond_buttons("sk_ic", row.item.id)))
    await cq.answer()


@router.callback_query(F.data.regexp(rf"^sk_ic:\d{{1,9}}:{COND_RE}$"), IsActive())
async def cb_in_cond(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    _, item_id, condition = cq.data.split(":")
    if await _deny(cq, staff) or (row := await _active_row(cq, s, int(item_id))) is None:
        return
    await _drop_markup(cq)
    await state.clear()
    await state.set_state(StockForm.in_qty)
    await state.update_data(item_id=row.item.id, condition=condition)
    await cq.message.answer(
        f"{row.item.name} ({CONDITION_ONE[condition]}): сколько пришло? 1–{stock.MAX_IN}",
        reply_markup=menu.cancel_menu(),
    )
    await cq.answer()


@router.message(StockForm.in_qty, IsActive(), F.text.func(menu.is_client_reply_text))
async def got_in_qty(message: Message, state: FSMContext, s, staff: Staff):
    if not await _coord_text(message, state, staff):
        return
    qty = parse_int(message.text, 1, stock.MAX_IN)
    if qty is None:
        await message.answer(f"Введите число от 1 до {stock.MAX_IN}.")
        return
    data = await state.get_data()
    await state.clear()
    try:
        now = await stock.receive(s, staff, data["item_id"], data["condition"], qty)
    except stock.Archived:
        await message.answer("Позиция в архиве.", reply_markup=menu.main_menu(staff))
        return
    item = await stock.get_item(s, data["item_id"])
    await message.answer(
        f"➕ {item.name} +{qty} ({CONDITION_ONE[data['condition']]}). Теперь {now}.",
        reply_markup=menu.main_menu(staff),
    )


@router.callback_query(F.data.regexp(ID_DATA.format("sk_fix")), IsActive())
async def cb_fix(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    if await _deny(cq, staff) or (row := await _active_row(cq, s, arg_id(cq))) is None:
        return
    await state.clear()
    await cq.message.answer(
        f"{row.item.name}: какое состояние исправить?",
        reply_markup=markup(_cond_buttons("sk_fc", row.item.id)),
    )
    await cq.answer()


@router.callback_query(F.data.regexp(rf"^sk_fc:\d{{1,9}}:{COND_RE}$"), IsActive())
async def cb_fix_cond(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    _, item_id, condition = cq.data.split(":")
    if await _deny(cq, staff) or (row := await _active_row(cq, s, int(item_id))) is None:
        return
    await _drop_markup(cq)
    await state.clear()
    await state.set_state(StockForm.fix_qty)
    await state.update_data(item_id=row.item.id, condition=condition)
    await cq.message.answer(
        f"{row.item.name} ({CONDITION_ONE[condition]}): сейчас {row.levels[condition]}. "
        f"Сколько на самом деле? 0–{stock.MAX_LEVEL}",
        reply_markup=menu.cancel_menu(),
    )
    await cq.answer()


@router.message(StockForm.fix_qty, IsActive(), F.text.func(menu.is_client_reply_text))
async def got_fix_qty(message: Message, state: FSMContext, s, staff: Staff):
    if not await _coord_text(message, state, staff):
        return
    qty = parse_int(message.text, 0, stock.MAX_LEVEL)
    if qty is None:
        await message.answer(f"Введите число от 0 до {stock.MAX_LEVEL}.")
        return
    data = await state.get_data()
    await state.clear()
    try:
        await stock.set_level(s, staff, data["item_id"], data["condition"], qty)
    except stock.Archived:
        await message.answer("Позиция в архиве.", reply_markup=menu.main_menu(staff))
        return
    item = await stock.get_item(s, data["item_id"])
    await message.answer(
        f"✏️ {item.name} ({CONDITION_ONE[data['condition']]}): теперь {qty}.",
        reply_markup=menu.main_menu(staff),
    )


@router.callback_query(F.data.regexp(ID_DATA.format("sk_min")), IsActive())
async def cb_min(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    if await _deny(cq, staff) or (row := await _active_row(cq, s, arg_id(cq))) is None:
        return
    await state.clear()
    await state.set_state(StockForm.min_qty)
    await state.update_data(item_id=row.item.id)
    now = row.item.min_qty if row.item.min_qty is not None else "не задан"
    await cq.message.answer(
        f"Минимум для «{row.item.name}» (сейчас {now}). Введите число 0–{stock.MAX_LEVEL}:",
        reply_markup=markup([[("Убрать", "sk_minx")]]),
    )
    await cq.answer()


async def _apply_min(message, state: FSMContext, s, staff: Staff, value: int | None) -> None:
    data = await state.get_data()
    await state.clear()
    try:
        await stock.set_min(s, staff, data["item_id"], value)
    except stock.Archived:
        await message.answer("Позиция в архиве.", reply_markup=menu.main_menu(staff))
        return
    item = await stock.get_item(s, data["item_id"])
    text = f"🔔 Минимум для «{item.name}»: {value}." if value is not None else f"🔔 Минимум для «{item.name}» убран."
    await message.answer(text, reply_markup=menu.main_menu(staff))


@router.message(StockForm.min_qty, IsActive(), F.text.func(menu.is_client_reply_text))
async def got_min(message: Message, state: FSMContext, s, staff: Staff):
    if not await _coord_text(message, state, staff):
        return
    value = parse_int(message.text, 0, stock.MAX_LEVEL)
    if value is None:
        await message.answer(f"Введите число от 0 до {stock.MAX_LEVEL} или нажмите «Убрать».")
        return
    await _apply_min(message, state, s, staff, value)


@router.callback_query(StockForm.min_qty, F.data == "sk_minx", IsActive())
async def cb_min_clear(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    if await _deny(cq, staff):
        return
    await _drop_markup(cq)
    await _apply_min(cq.message, state, s, staff, None)
    await cq.answer()


@router.callback_query(F.data.regexp(ID_DATA.format("sk_arch")), IsActive())
async def cb_arch(cq: CallbackQuery, s, staff: Staff):
    if await _deny(cq, staff) or (row := await _active_row(cq, s, arg_id(cq))) is None:
        return
    await cq.message.answer(
        f"Убрать «{row.item.name}» в архив? История выдач сохранится.",
        reply_markup=markup([[("Да, в архив", f"sk_archok:{row.item.id}"), ("Отмена", "sk_cancel")]]),
    )
    await cq.answer()


@router.callback_query(F.data.regexp(ID_DATA.format("sk_archok")), IsActive())
async def cb_arch_ok(cq: CallbackQuery, s, staff: Staff):
    if await _deny(cq, staff):
        return
    try:
        item = await stock.archive(s, staff, arg_id(cq))
    except stock.Archived:
        await cq.answer("Позиция в архиве", show_alert=True)
        return
    await _edit(cq, f"🗄 {item.name} — в архиве.", None)
    await cq.answer()


@router.callback_query(F.data == "sk_new", IsActive())
async def cb_new(cq: CallbackQuery, state: FSMContext, staff: Staff):
    if await _deny(cq, staff):
        return
    await state.clear()
    cats = [(label, f"sk_nc:{code}") for code, label in CATEGORIES.items()]
    rows = [cats[i : i + 2] for i in range(0, len(cats), 2)]
    await cq.message.answer("Новая позиция — категория:", reply_markup=markup(rows))
    await cq.answer()


@router.callback_query(F.data.regexp(r"^sk_nc:[a-z]{1,24}$"), IsActive())
async def cb_new_cat(cq: CallbackQuery, state: FSMContext, staff: Staff):
    if await _deny(cq, staff):
        return
    code = cq.data.split(":")[1]
    if code not in CATEGORIES:
        await cq.answer("Кнопка устарела", show_alert=True)
        return
    await _drop_markup(cq)
    await state.clear()
    await state.set_state(StockForm.new_name)
    await state.update_data(category=code)
    await cq.message.answer(f"{CATEGORIES[code]}: название позиции?", reply_markup=menu.cancel_menu())
    await cq.answer()


@router.message(StockForm.new_name, IsActive(), F.text.func(menu.is_client_reply_text))
async def got_new_name(message: Message, state: FSMContext, s, staff: Staff):
    if not await _coord_text(message, state, staff):
        return
    name = stock.clean_name(message.text)
    if not 1 <= len(name) <= 200:
        await message.answer("Название от 1 до 200 символов.")
        return
    category = (await state.get_data())["category"]
    if dup := await stock.find_active(s, category, name):
        await state.clear()
        await message.answer("Отменено.", reply_markup=menu.main_menu(staff))
        await message.answer(f"Уже есть: {dup.name}", reply_markup=markup([[("Открыть", f"sk_item:{dup.id}")]]))
        return
    await state.update_data(name=name)
    await state.set_state(StockForm.new_min)
    await message.answer(
        f"Минимум для «{name}»? Число 0–{stock.MAX_LEVEL} или «Пропустить».",
        reply_markup=markup([[("Пропустить", "sk_skipmin")]]),
    )


async def _create(message, state: FSMContext, s, staff: Staff, min_qty: int | None) -> None:
    data = await state.get_data()
    await state.clear()
    try:
        item = await stock.create_item(s, staff, data["category"], data["name"], min_qty)
    except stock.DuplicateName as e:
        await message.answer("Уже есть такая позиция.", reply_markup=markup([[("Открыть", f"sk_item:{e.item_id}")]]))
        return
    await message.answer("Готово.", reply_markup=menu.main_menu(staff))
    buttons = _cond_buttons("sk_ic", item.id) + [[("Без прихода — открыть", f"sk_item:{item.id}")]]
    await message.answer(f"Позиция «{item.name}» создана. Что пришло?", reply_markup=markup(buttons))


@router.message(StockForm.new_min, IsActive(), F.text.func(menu.is_client_reply_text))
async def got_new_min(message: Message, state: FSMContext, s, staff: Staff):
    if not await _coord_text(message, state, staff):
        return
    value = parse_int(message.text, 0, stock.MAX_LEVEL)
    if value is None:
        await message.answer(f"Введите число от 0 до {stock.MAX_LEVEL} или нажмите «Пропустить».")
        return
    await _create(message, state, s, staff, value)


@router.callback_query(StockForm.new_min, F.data == "sk_skipmin", IsActive())
async def cb_new_skip_min(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    if await _deny(cq, staff):
        return
    await _drop_markup(cq)
    await _create(cq.message, state, s, staff, None)
    await cq.answer()
```

Тексты в тестах проверяют `answers[-1]` — порядок ответов в обработчиках выше (сначала «Отменено.»/«Готово.» с главным меню, затем сообщение с инлайн-кнопками) это учитывает.

- [ ] **Step 4: Проверить и закоммитить**

Run: `uv run ruff format src tests && make check` — Expected: PASS (`test_stock_dialog_routing` — без изменений).

```bash
git add src/helpdesk/telegram/stock_handlers.py tests/test_stock_handlers.py
git commit -m "feat(stock): coordinator receive, fix, minimum, archive and new item dialogs"
```

---

### Task 6: xlsx в боте и документация

**Files:**
- Modify: `src/helpdesk/telegram/stock_handlers.py` (перед `stray_input`), `README.md`
- Test: `tests/test_stock_handlers.py`

**Interfaces:**
- Consumes: `stock_xlsx.template`, `parse`, `export`, `BadFile`, `MAX_BYTES`; `service.preview_import`, `apply_import`, `ImportRow` (задача 3); `StockForm.import_file`, `import_confirm`, `_deny`, `_coord_text` (задачи 4–5).
- Produces: обработчики `cb_export`, `cb_upload`, `got_import_file`, `cb_upload_ok`.

- [ ] **Step 1: Тесты** — в конец `tests/test_stock_handlers.py` (импорты вверх: `from io import BytesIO`, `from openpyxl import Workbook`):

```python
def _xlsx(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["Категория", "Название", "Состояние", "Количество", "Минимум"])
    for r in rows:
        ws.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


class _Bot:
    def __init__(self, data: bytes):
        self.data = data

    async def download(self, obj, destination):
        destination.write(self.data)


def _doc(name="Склад.xlsx", size=100):
    return SimpleNamespace(file_name=name, file_size=size, file_id="f", file_unique_id="u")


async def test_export_button(s, clock_):
    _, erlan, _ = await _setup(s)
    cq = Cq("sk_xlsx")
    await stock_handlers.cb_export(cq, s, erlan)
    doc, caption = cq.message.docs[-1]
    assert doc.filename == "Склад.xlsx" and caption == "Склад: остатки и движение"


async def test_upload_flow(s, clock_, state):
    coord, _, hp = await _setup(s)
    cq = Cq("sk_upl")
    await stock_handlers.cb_upload(cq, state, coord)
    assert await state.get_state() == StockForm.import_file.state
    assert cq.message.docs[-1][0].filename == "Склад-шаблон.xlsx"
    data = _xlsx([
        ["Картриджи", "Картридж HP 85A", "новые", 3, None],
        ["Мыши", "Мышь USB", "б/у", 4, 1],
        ["Принтер", "X", "новые", 1, None],
    ])
    m = Msg(document=_doc(size=len(data)))
    await stock_handlers.got_import_file(m, state, s, coord, _Bot(data))
    text, markup_ = m.answers[-1]
    assert text == (
        "Новых позиций: 1, приход по существующим: 1, ошибки: 1\n"
        "строка 4: неизвестная категория «Принтер»"
    )
    assert kb(markup_) == [[("✅ Загрузить (без ошибочных строк)", "sk_uplok"), ("✖️ Отмена", "sk_cancel")]]
    assert await state.get_state() == StockForm.import_confirm.state
    assert (await stock.levels(s, hp.id))["new"] == 7  # ничего не записано до подтверждения
    cq = Cq("sk_uplok")
    await stock_handlers.cb_upload_ok(cq, state, s, coord)
    assert cq.message.answers[-1][0] == "✅ Загружено: новых позиций 1, строк с приходом 2."
    assert (await stock.levels(s, hp.id))["new"] == 10
    assert await state.get_state() is None


async def test_upload_rejects_bad_files(s, clock_, state):
    coord, _, _ = await _setup(s)
    await stock_handlers.cb_upload(Cq("sk_upl"), state, coord)
    m = Msg(document=_doc(name="склад.csv"))
    await stock_handlers.got_import_file(m, state, s, coord, _Bot(b""))
    assert m.answers[-1][0] == "Нужен файл .xlsx."
    m = Msg(document=_doc(size=3 * 1024 * 1024))
    await stock_handlers.got_import_file(m, state, s, coord, _Bot(b""))
    assert m.answers[-1][0] == "Файл больше 2 МБ."
    m = Msg(document=_doc())
    await stock_handlers.got_import_file(m, state, s, coord, _Bot(b"garbage"))
    assert m.answers[-1][0] == "Не удалось прочитать файл — нужен .xlsx."
    data = _xlsx([["Принтер", "X", "новые", 1, None]])
    m = Msg(document=_doc(size=len(data)))
    await stock_handlers.got_import_file(m, state, s, coord, _Bot(data))
    assert m.answers[-1][0].startswith("Нет строк для загрузки.\nстрока 2:")
    assert await state.get_state() == StockForm.import_file.state  # можно прислать другой


async def test_upload_rejected_for_specialist(s, clock_, state):
    _, erlan, _ = await _setup(s)
    cq = Cq("sk_upl")
    await stock_handlers.cb_upload(cq, state, erlan)
    assert cq.alerts == ["Только для координатора"] and cq.message.docs == []
    await state.set_state(StockForm.import_confirm)
    await state.update_data(rows=[{"line": 2, "category": "mice", "name": "Мышь",
                                   "condition": "new", "qty": 5, "min_qty": None}])
    cq = Cq("sk_uplok")
    await stock_handlers.cb_upload_ok(cq, state, s, erlan)
    assert cq.alerts == ["Только для координатора"]
    assert await stock.find_active(s, "mice", "Мышь") is None


async def test_document_in_import_state_routed_to_import():
    staff = SimpleNamespace(status="active", is_admin=False, role="coordinator")
    doc = {"file_id": "f", "file_unique_id": "u", "file_name": "a.xlsx", "file_size": 10}
    names = await _matched_names(_message(document=doc), StockForm.import_file.state, staff)
    assert names[0] == "got_import_file"
```

- [ ] **Step 2: Запустить — падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test uv run pytest -q tests/test_stock_handlers.py`
Expected: FAIL (`AttributeError: ... 'cb_export'`).

- [ ] **Step 3: Реализация** — импорты в `stock_handlers.py`: `from dataclasses import asdict`, `from io import BytesIO`, `from aiogram.types import BufferedInputFile`, `from helpdesk.stock import xlsx as stock_xlsx`. Перед `stray_input`:

```python
# ── xlsx ───────────────────────────────────────────────────────────────


@router.callback_query(F.data == "sk_xlsx", IsActive())
async def cb_export(cq: CallbackQuery, s, staff: Staff):
    data = await stock_xlsx.export(s)
    await cq.message.answer_document(
        BufferedInputFile(data, filename="Склад.xlsx"), caption="Склад: остатки и движение"
    )
    await cq.answer()


@router.callback_query(F.data == "sk_upl", IsActive())
async def cb_upload(cq: CallbackQuery, state: FSMContext, staff: Staff):
    if await _deny(cq, staff):
        return
    await state.clear()
    await state.set_state(StockForm.import_file)
    await cq.message.answer_document(
        BufferedInputFile(stock_xlsx.template(), filename="Склад-шаблон.xlsx"),
        caption=(
            "Заполните лист «Склад» и пришлите файл сюда (.xlsx, до 2 МБ, до 1000 строк). "
            "Количество прибавляется к остатку — не загружайте один файл дважды."
        ),
        reply_markup=menu.cancel_menu(),
    )
    await cq.answer()


@router.message(StockForm.import_file, IsActive(), F.document)
async def got_import_file(message: Message, state: FSMContext, s, staff: Staff, bot):
    if not await _coord_text(message, state, staff):
        return
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".xlsx"):
        await message.answer("Нужен файл .xlsx.")
        return
    if (doc.file_size or 0) > stock_xlsx.MAX_BYTES:
        await message.answer("Файл больше 2 МБ.")
        return
    buf = BytesIO()
    await bot.download(doc, destination=buf)
    try:
        rows, errors = stock_xlsx.parse(buf.getvalue())
    except stock_xlsx.BadFile as e:
        await message.answer(str(e))
        return
    shown = "\n".join(errors[:10]) + (f"\n…и ещё {len(errors) - 10}" if len(errors) > 10 else "")
    if not rows:
        await message.answer("Нет строк для загрузки." + (f"\n{shown}" if shown else ""))
        return
    new, existing = await stock.preview_import(s, rows)
    text = f"Новых позиций: {new}, приход по существующим: {existing}, ошибки: {len(errors)}"
    if shown:
        text += f"\n{shown}"
    await state.set_state(StockForm.import_confirm)
    await state.update_data(rows=[asdict(r) for r in rows])
    await message.answer(
        text,
        reply_markup=markup([[("✅ Загрузить (без ошибочных строк)", "sk_uplok"), ("✖️ Отмена", "sk_cancel")]]),
    )


@router.callback_query(StockForm.import_confirm, F.data == "sk_uplok", IsActive())
async def cb_upload_ok(cq: CallbackQuery, state: FSMContext, s, staff: Staff):
    if await _deny(cq, staff):
        return
    rows = [stock.ImportRow(**r) for r in (await state.get_data())["rows"]]
    await state.clear()
    await _drop_markup(cq)
    created, received = await stock.apply_import(s, staff, rows)
    await cq.message.answer(
        f"✅ Загружено: новых позиций {created}, строк с приходом {received}.",
        reply_markup=menu.main_menu(staff),
    )
    await cq.answer()
```

Если в aiogram 3.31 `answer_document` не принимает `ReplyKeyboardMarkup` вместе с документом — передать `reply_markup=menu.cancel_menu()` отдельным `cq.message.answer("Жду файл.", reply_markup=menu.cancel_menu())` (тест проверяет только документ).

- [ ] **Step 4: README**

В раздел возможностей бота — абзац:

```markdown
**📦 Склад.** Остатки техники и расходников по категориям и состояниям (новые / б/у / после ремонта). Специалист берёт (−), координатор ведёт приход, исправления, минимум, архив и загрузку xlsx по шаблону. При падении остатка до минимума координаторы получают предупреждение. «📥 Склад.xlsx» — остатки и вся история движения.
```

В конец «Чек-листа ручной приёмки» (продолжая нумерацию — проверить последний номер, сейчас 35):

```markdown
### Склад

36. [ ] «📦 Склад» у специалиста: категории с количеством, «🔍 Найти», «📥 Склад.xlsx»; нет «➕ Новая позиция» и «📤 Загрузить xlsx».
37. [ ] Координатор: «📤 Загрузить xlsx» → пришёл шаблон → заполнить 3–4 строки (одну с ошибкой) → итог с ошибкой строки → «✅ Загрузить» → позиции появились.
38. [ ] Специалист: категория → позиция → «➖ Взять» → количество → «✅ Взято … Осталось N».
39. [ ] Взять так, чтобы остаток опустился до минимума → координатору «⚠️ … осталось N (минимум M)», один раз.
40. [ ] Координатор: «➕ Приход», «✏️ Исправить», «🔔 Минимум», «🗄 В архив» на одной позиции.
41. [ ] «🔍 Найти» по части названия, без учёта регистра.
42. [ ] Посреди диалога склада нажать «📋 Мои заявки» — диалог закрыт, открылись заявки; текст посреди диалога не ушёл пользователю WhatsApp.
43. [ ] «📥 Склад.xlsx» — листы «Остатки» и «Движение».
```

- [ ] **Step 5: Финальная проверка и коммит**

Run: `uv run ruff format src tests && make test-db && make check` — Expected: PASS.

```bash
git add src/helpdesk/telegram/stock_handlers.py tests/test_stock_handlers.py README.md
git commit -m "feat(stock): xlsx template, import with preview and confirm, stock export; docs"
```

Ручная проверка — пункты 36–43 на запущенном боте (`docker compose up -d --build`; миграция применится при старте).
