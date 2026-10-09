import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand

from helpdesk import clock, reminders
from helpdesk.config import Settings
from helpdesk.db.session import make_engine, make_session_factory
from helpdesk.delivery import worker
from helpdesk.telegram.gateway import AiogramSender
from helpdesk.telegram.handlers import BOT_COMMANDS, setup
from helpdesk.whatsapp import poller
from helpdesk.whatsapp.client import GreenApiClient


async def amain() -> None:
    settings = Settings()
    clock.set_local_tz(settings.tz)
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # в URL Green API есть токен
    Path(settings.files_dir).mkdir(parents=True, exist_ok=True)

    engine = make_engine(settings.database_url)
    sf = make_session_factory(engine)
    bot = Bot(settings.telegram_bot_token)
    green = GreenApiClient(
        settings.green_api_url,
        settings.media_url,
        settings.green_api_id_instance,
        settings.green_api_token,
    )
    dp = Dispatcher(events_isolation=SimpleEventIsolation())
    dp["settings"] = settings
    setup(dp, sf)

    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(worker.run(sf, green, AiogramSender(bot), stop)),
        asyncio.create_task(poller.run(sf, green, settings.files_dir, stop)),
        asyncio.create_task(
            reminders.run(
                sf, settings.remind_after_min, stop, task_remind_hour=settings.task_remind_hour
            )
        ),
    ]
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in BOT_COMMANDS])
        await dp.start_polling(bot)
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await green.aclose()
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
