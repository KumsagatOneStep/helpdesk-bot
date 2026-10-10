from dataclasses import dataclass

import structlog
from sqlalchemy import func, select, update
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
