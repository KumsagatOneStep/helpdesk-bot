from aiogram.filters import Filter
from aiogram.types import CallbackQuery

from helpdesk.db.models import Staff

# id из callback_data: только цифры и не больше int4 — подделка уходит в cb_fallback
ID_DATA = r"^{}:\d{{1,9}}$"


class IsActive(Filter):
    async def __call__(self, event, staff: Staff | None = None) -> bool:
        return staff is not None and staff.status == "active"


class IsAdmin(Filter):
    async def __call__(self, event, staff: Staff | None = None) -> bool:
        return staff is not None and staff.status == "active" and staff.is_admin


def arg_id(cq: CallbackQuery) -> int:
    return int(cq.data.split(":", 1)[1])
