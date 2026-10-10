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
        "Картридж HP 85A",
        "cartridges",
        3,
        False,
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
