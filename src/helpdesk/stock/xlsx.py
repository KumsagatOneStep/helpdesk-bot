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
    h.append(
        ["Одна строка — одна позиция в одном состоянии. Первую строку (заголовки) не трогайте."]
    )
    h.append(
        ["Повторная загрузка прибавляет количество к остатку — не загружайте один файл дважды."]
    )
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
        ws.append(
            [
                catalog.plain(r.item.category),
                safe(r.item.name),
                lv["new"],
                lv["used"],
                lv["repaired"],
                r.total,
                r.item.min_qty,
                "⚠️" if r.low else None,
            ]
        )
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
        wm.append(
            [
                naive(m.created_at),
                safe(names.get(m.staff_id)),
                catalog.plain(item.category)
                if item.category in catalog.CATEGORIES
                else item.category,
                safe(item.name),
                catalog.CONDITIONS.get(m.condition, m.condition),
                m.delta,
                KIND_RU.get(m.kind, m.kind),
            ]
        )
    layout(wm, MOVE_WIDTHS)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
