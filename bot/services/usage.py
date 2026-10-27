from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import SendLog, utcnow
from bot.services import settings
from bot.services.plans import Plan


def day_start() -> datetime:
    return utcnow().replace(hour=0, minute=0, second=0, microsecond=0)


async def sent_today_user(s: AsyncSession, user_id: int) -> int:
    q = select(func.count()).select_from(SendLog).where(SendLog.user_id == user_id, SendLog.at >= day_start())
    return (await s.execute(q)).scalar_one()


async def sent_today_account(s: AsyncSession, account_id: int) -> int:
    q = select(func.count()).select_from(SendLog).where(SendLog.account_id == account_id, SendLog.at >= day_start())
    return (await s.execute(q)).scalar_one()


async def user_daily_cap(s: AsyncSession, plan: Plan) -> int:
    cap = plan.daily_limit
    g = await settings.get_int(s, settings.GLOBAL_DAILY_CAP)
    return min(cap, g) if g else cap


async def remaining_today(s: AsyncSession, user_id: int, plan: Plan) -> tuple[int, int, int]:
    """-> (осталось, лимит, использовано)"""
    cap = await user_daily_cap(s, plan)
    used = await sent_today_user(s, user_id)
    return max(0, cap - used), cap, used
