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
    rows, errors = stock_xlsx.parse(
        _book(
            [
                ["Картриджи", "Картридж HP 85A", "новые", 5, 3],
                ["mice", " Мышь   USB ", None, "2", None],
                [None, None, None, None, None],  # пустая строка пропускается
                ["🖥 Системные блоки", "Dell OptiPlex", "после ремонта", 1.0, ""],
            ]
        )
    )
    assert errors == []
    assert [(r.line, r.category, r.name, r.condition, r.qty, r.min_qty) for r in rows] == [
        (2, "cartridges", "Картридж HP 85A", "new", 5, 3),
        (3, "mice", "Мышь USB", "new", 2, None),
        (5, "pc", "Dell OptiPlex", "repaired", 1, None),
    ]


def test_parse_reports_each_bad_field_with_line():
    _, errors = stock_xlsx.parse(
        _book(
            [
                ["Принтер", "X", "новые", 1, None],
                ["Мыши", "", "новые", 1, None],
                ["Мыши", "Y", "сломан", 1, None],
                ["Мыши", "Z", "новые", "пять", None],
                ["Мыши", "W", "новые", 1, -2],
                ["Мыши", "V", "новые", 100001, None],
            ]
        )
    )
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


def test_parse_superscript_digit_is_an_error_not_a_crash():
    _, errors = stock_xlsx.parse(_book([["Мыши", "Мышь", "новые", "²", None]]))
    assert errors == ["строка 2: количество «²» — нужно целое от 0 до 100000"]


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
        "Категория",
        "Название",
        "Состояние",
        "Количество",
        "Минимум",
    ]
    assert wb["Склад"].max_row == 1  # примеры только в «Справке» — случайно не загрузятся
    help_text = "\n".join(str(c.value) for row in wb["Справка"].iter_rows() for c in row if c.value)
    assert "Картриджи" in help_text and "после ремонта" in help_text
    assert "прибавляет" in help_text


async def test_preview_and_apply_import_adds_up(s, clock_):
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    existing = await stock.create_item(s, coord, "mice", "Мышь USB", min_qty=1)
    rows, _ = stock_xlsx.parse(
        _book(
            [
                ["Мыши", "мышь usb", "б/у", 2, 4],
                ["Картриджи", "HP 85A", "новые", 5, None],
                ["Картриджи", "HP 85A", "новые", 1, 3],
                ["Батарейки", "AA", "новые", 0, 10],
            ]
        )
    )
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
    item = await stock.create_item(s, coord, "other", '=HYPERLINK("x")')
    await stock.receive(s, coord, item.id, "new", 1)
    wb = load_workbook(BytesIO(await stock_xlsx.export(s)))
    assert wb["Остатки"]["B2"].value == '\'=HYPERLINK("x")'
    assert wb["Движение"]["B2"].value == "'=cmd"
