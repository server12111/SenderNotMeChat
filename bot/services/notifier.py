from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import Bot
from sqlalchemy import select

from bot.database.db import Session
from bot.database.models import Subscription, utcnow
from bot.services.plans import PLANS
from bot.utils.text import SEP, fmt_dt
from bot.utils.ui import ikb

log = logging.getLogger(__name__)


async def _send(bot: Bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text, reply_markup=ikb([[("💎 Продлить", "sub:main")]]))
    except Exception:
        pass


async def check_expiring(bot: Bot) -> None:
    """Раз в час: предупреждения за 3 дня / 24 часа и уведомление об окончании. Каждый флаг шлётся один раз."""
    now = utcnow()
    async with Session() as s:
        subs = (await s.execute(select(Subscription).where(
            Subscription.plan.in_(("pro", "vip")), Subscription.expires_at.is_not(None)))).scalars().all()
        for sub in subs:
            title = PLANS[sub.plan].title if sub.plan in PLANS else sub.plan
            left = sub.expires_at - now
            if left <= timedelta(0):
                if not sub.notified_exp:
                    sub.notified_exp = True
                    await _send(bot, sub.user_id, f"⌛ <b>Тариф {title} закончился</b>\n{SEP}\nРассылки поставлены на паузу — продлите подписку, чтобы продолжить.")
            elif left <= timedelta(hours=24):
                if not sub.notified_1d:
                    sub.notified_1d = sub.notified_3d = True
                    await _send(bot, sub.user_id, f"🔔 <b>Тариф {title} истекает через 24 часа</b>\n{SEP}\nДо: <b>{fmt_dt(sub.expires_at)} UTC</b>")
            elif left <= timedelta(days=3):
                if not sub.notified_3d:
                    sub.notified_3d = True
                    await _send(bot, sub.user_id, f"🔔 <b>Тариф {title} истекает через 3 дня</b>\n{SEP}\nДо: <b>{fmt_dt(sub.expires_at)} UTC</b>")
        await s.commit()
