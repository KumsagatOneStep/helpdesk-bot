import asyncio

import pytest
from sqlalchemy import select

from helpdesk.db.models import Staff, StockMove
from helpdesk.stock import service as stock
from tests.factories import clear_outbox, make_staff, payloads


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


async def test_set_level_writes_difference(s, clock_):
    coord, _ = await _people(s)
    item = await stock.create_item(s, coord, "batteries", "AA")
    await stock.receive(s, coord, item.id, "new", 10)
    await stock.set_level(s, coord, item.id, "new", 7)
    await stock.set_level(s, coord, item.id, "new", 7)  # без изменений — без записи
    await stock.set_level(s, coord, item.id, "used", 2)
    moves = (await s.scalars(select(StockMove).order_by(StockMove.id))).all()
    assert [(m.kind, m.condition, m.delta) for m in moves] == [
        ("in", "new", 10),
        ("fix", "new", -3),
        ("fix", "used", 2),
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
    assert [p["text"] for p in await payloads(s, "tg")] == ["⚠️ HP 85A: осталось 3 (минимум 3)"] * 2
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
