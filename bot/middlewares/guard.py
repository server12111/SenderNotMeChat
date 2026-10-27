from __future__ import annotations

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery
from sqlalchemy import func, select

from bot.database.models import Account, Mailing
from bot.services.usage import remaining_today


class PlanGuardMiddleware(BaseMiddleware):
    """Права перед созданием и запуском рассылки:
      ml:new            — есть подписка и хотя бы один рабочий аккаунт;
      ml:tg: / ml:launch: — при ВКЛЮЧЕНИИ: подписка, не превышен лимит активных рассылок, не исчерпан дневной лимит.
    Остановка рассылки не блокируется никогда."""

    async def __call__(self, handler, event: CallbackQuery, data: dict):
        cb = event.data or ""
        if not cb.startswith(("ml:new", "ml:tg:", "ml:launch:")):
            return await handler(event, data)

        s, user, plan = data["session"], data["user"], data["plan"]
        if cb.startswith("ml:tg:"):
            m = await s.get(Mailing, int(cb.split(":")[2]))
            if m is None or m.user_id != user.id or m.is_active:
                return await handler(event, data)  # чужая/выключение — пропускаем, дальше проверит хендлер

        if plan.key == "none":
            return await event.answer("💎 Нужна активная подписка PRO или VIP.\nОформить: меню → Подписка", show_alert=True)

        if cb.startswith("ml:new"):
            n_acc = (await s.execute(select(func.count()).select_from(Account).where(
                Account.user_id == user.id, Account.is_active.is_(True), Account.status != "dead"))).scalar_one()
            if n_acc == 0:
                return await event.answer("📱 Сначала добавьте рабочий аккаунт: Аккаунты → Добавить", show_alert=True)
            return await handler(event, data)

        n_active = (await s.execute(select(func.count()).select_from(Mailing).where(
            Mailing.user_id == user.id, Mailing.is_active.is_(True)))).scalar_one()
        if n_active >= plan.max_campaigns:
            return await event.answer(
                f"🚫 Тариф {plan.title}: не более {plan.max_campaigns} активных рассылок.\n💎 Расширьте тариф или остановите другую.",
                show_alert=True)
        remaining, cap, used = await remaining_today(s, user.id, plan)
        if remaining <= 0:
            return await event.answer(f"📉 Дневной лимит исчерпан: {used}/{cap}.\nСчётчик сбрасывается в 00:00 UTC.", show_alert=True)
        return await handler(event, data)
