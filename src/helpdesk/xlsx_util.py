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
