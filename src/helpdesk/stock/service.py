from dataclasses import dataclass
from math import ceil

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from helpdesk.db.models import Staff, StockItem, StockLevel, StockMove
from helpdesk.delivery import outbox
from helpdesk.stock.catalog import CATEGORIES, CONDITIONS
from helpdesk.tasks import rules

log = structlog.get_logger()

MAX_IN = 1000  # приход кнопкой
MAX_LEVEL = 100_000  # исправление, минимум, количество в xlsx
PER_PAGE = 10


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
        func.lower(StockItem.name) == func.lower(clean_name(name)),
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
    try:
        async with s.begin_nested():  # гонка за имя: откатываем только эту вставку
            s.add(item)
            await s.flush()
    except IntegrityError:
        if dup := await find_active(s, category, name):
            raise DuplicateName(dup.id) from None
        raise
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
    if delta < 0:
        # CHECK qty >= 0 проверяется на вставляемой строке ещё до ON CONFLICT,
        # поэтому списание — обычный UPDATE существующей строки
        stmt = (
            update(StockLevel)
            .where(StockLevel.item_id == item.id, StockLevel.condition == condition)
            .values(qty=StockLevel.qty + delta)
        )
        if (await s.execute(stmt)).rowcount != 1:
            raise BadQty()
    else:
        stmt = insert(StockLevel).values(item_id=item.id, condition=condition, qty=delta)
        stmt = stmt.on_conflict_do_update(
            index_elements=["item_id", "condition"],
            set_={"qty": StockLevel.__table__.c.qty + delta},
        )
        await s.execute(stmt)
    s.add(
        StockMove(item_id=item.id, condition=condition, delta=delta, kind=kind, staff_id=staff.id)
    )
    await s.flush()


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
