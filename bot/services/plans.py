from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import Subscription, utcnow

UNLIMITED = 10**9


@dataclass(frozen=True)
class Plan:
    key: str
    title: str
    emoji: str
    max_accounts: int
    max_campaigns: int  # одновременно активных
    daily_limit: int  # сообщений в сутки на пользователя
    min_delay: int  # минимальная задержка между отправками, сек
    spintax: bool
    repeat: bool
    proxy: bool
    extended: bool  # расширенная рандомизация пауз
    priority: int  # меньше = раньше в очереди


PLANS: dict[str, Plan] = {
    "pro": Plan("pro", "PRO", "⭐", 5, 10, UNLIMITED, 15, True, True, False, False, 1),
    "vip": Plan("vip", "VIP", "👑", UNLIMITED, 100, UNLIMITED, 5, True, True, True, True, 0),
}


# Без активной подписки пользоваться отправкой нельзя: все лимиты нулевые
NO_PLAN = Plan("none", "нет", "⛔", 0, 0, 0, 30, False, False, False, False, 3)


def lim(n: int) -> str:
    return "∞" if n >= UNLIMITED else str(n)


async def get_sub(s: AsyncSession, user_id: int) -> Subscription | None:
    return (await s.execute(select(Subscription).where(Subscription.user_id == user_id))).scalar_one_or_none()


async def effective_plan(s: AsyncSession, user_id: int) -> tuple[Plan, datetime | None]:
    """Нет активной подписки -> NO_PLAN."""
    sub = await get_sub(s, user_id)
    if sub and sub.plan in PLANS and sub.expires_at and sub.expires_at > utcnow():
        return PLANS[sub.plan], sub.expires_at
    return NO_PLAN, None


async def grant(s: AsyncSession, user_id: int, plan_key: str, days: int) -> datetime:
    """Выдать/продлить. Продление того же активного тарифа прибавляет дни к текущему сроку."""
    sub = await get_sub(s, user_id)
    if sub is None:
        sub = Subscription(user_id=user_id)
        s.add(sub)
    now = utcnow()
    same_active = sub.plan == plan_key and sub.expires_at and sub.expires_at > now
    base = sub.expires_at if same_active else now
    sub.plan = plan_key
    sub.expires_at = base + timedelta(days=days)
    sub.notified_3d = sub.notified_1d = sub.notified_exp = False
    await s.flush()
    return sub.expires_at


async def revoke(s: AsyncSession, user_id: int) -> None:
    sub = await get_sub(s, user_id)
    if sub:
        sub.plan, sub.expires_at = "none", None
        sub.notified_3d = sub.notified_1d = sub.notified_exp = False


def clamp_delays(plan: Plan, dmin: int, dmax: int, global_min: int = 0) -> tuple[int, int]:
    floor = max(plan.min_delay, global_min)
    dmin = max(dmin, floor)
    return dmin, max(dmax, dmin)
