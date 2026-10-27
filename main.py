from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.config import config
from bot.database.db import engine, init_db
from bot.handlers import routers
from bot.middlewares.access import AccessMiddleware, DbSessionMiddleware
from bot.services.notifier import check_expiring
from bot.services.rates import RateUpdater
from bot.services.clients import ClientManager
from bot.services.sender import SenderService


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.bot_token:
        raise SystemExit("Заполните BOT_TOKEN в .env (см. .env.example)")
    if not config.api_id or not config.api_hash:
        logging.warning("API_ID/API_HASH не заданы — добавление аккаунтов и отправка работать не будут")

    await init_db()
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(DbSessionMiddleware())
        observer.outer_middleware(AccessMiddleware())
    for r in routers:
        dp.include_router(r)

    clients = ClientManager()
    sender = SenderService(bot, clients, max_running=config.max_running)
    dp.workflow_data.update(sender=sender, clients=clients)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(check_expiring, "interval", hours=1, args=[bot], max_instances=1, coalesce=True)

    rate_updater = RateUpdater()
    rate_updater.start()
    await sender.start()  # поднимает задачи включённых рассылок: продолжают с того же места
    scheduler.start()
    await check_expiring(bot)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await rate_updater.stop()
        await sender.stop()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
