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
