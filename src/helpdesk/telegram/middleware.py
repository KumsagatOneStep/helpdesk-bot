from aiogram import BaseMiddleware
from sqlalchemy import select

from helpdesk.db.models import Staff


class DbMiddleware(BaseMiddleware):
    def __init__(self, sf):
        self.sf = sf

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        async with self.sf() as s, s.begin():
            data["s"] = s
            data["staff"] = (
                await s.scalar(select(Staff).where(Staff.tg_user_id == user.id)) if user else None
            )
            return await handler(event, data)
