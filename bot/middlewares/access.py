from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.config import config
from bot.database.db import Session
from bot.database.models import Subscription, User, utcnow
from bot.services import settings
from bot.services.plans import effective_plan


class DbSessionMiddleware(BaseMiddleware):
    """Одна сессия на апдейт, commit при успехе."""

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]], event: TelegramObject, data: dict):
        async with Session() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise


class AccessMiddleware(BaseMiddleware):
    """Регистрация пользователя, бан, техобслуживание; кладёт в data: user, plan, plan_expires, is_admin."""

    async def __call__(self, handler, event: Message | CallbackQuery, data: dict):
        tg = data.get("event_from_user")
        if tg is None or tg.is_bot:
            return await handler(event, data)

        session = data["session"]
        user = await session.get(User, tg.id)
        if user is None:
            user = User(id=tg.id, username=tg.username, full_name=tg.full_name)
            session.add(user)
            session.add(Subscription(user_id=tg.id, plan="none"))
            await session.flush()
        else:
            user.username, user.full_name, user.last_seen = tg.username, tg.full_name, utcnow()

        is_admin = tg.id in config.admin_ids
        if not is_admin:
            if user.is_banned:
                return await self._deny(event, "🚫 Доступ к сервису заблокирован.")
            if await settings.get(session, settings.MAINTENANCE) == "1":
                return await self._deny(event, "🛠 Технические работы. Скоро вернёмся!")

        plan, expires = await effective_plan(session, user.id)
        data.update(user=user, plan=plan, plan_expires=expires, is_admin=is_admin)
        return await handler(event, data)

    @staticmethod
    async def _deny(event: Message | CallbackQuery, text: str) -> None:
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)
