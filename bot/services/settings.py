from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import Setting

MAINTENANCE = "maintenance"
GLOBAL_MIN_DELAY = "global_min_delay"  # сек, 0 = выкл
GLOBAL_DAILY_CAP = "global_daily_cap"  # сообщений/сутки на пользователя, 0 = выкл


async def get(s: AsyncSession, key: str, default: str = "") -> str:
    row = await s.get(Setting, key)
    return row.value if row else default


async def get_int(s: AsyncSession, key: str) -> int:
    try:
        return int(await get(s, key, "0"))
    except ValueError:
        return 0


async def set_(s: AsyncSession, key: str, value: str | int) -> None:
    row = await s.get(Setting, key)
    if row:
        row.value = str(value)
    else:
        s.add(Setting(key=key, value=str(value)))
    await s.flush()
