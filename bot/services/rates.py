"""Автообновление курсов для оплаты: BTC (Binance → CoinGecko) и гривна (monobank → НБУ)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import aiohttp

from bot.database.db import Session
from bot.database.models import utcnow
from bot.services import billing

log = logging.getLogger(__name__)

UPDATE_INTERVAL = 15 * 60  # секунд
_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def _get_json(session: aiohttp.ClientSession, url: str):
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def _btc_per_usd(session: aiohttp.ClientSession) -> float:
    """Сколько BTC стоит $1."""
    try:
        data = await _get_json(session, "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
        price = float(data["price"])
    except Exception:
        data = await _get_json(session, "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd")
        price = float(data["bitcoin"]["usd"])
    return 1 / price


async def _uah_per_usd(session: aiohttp.ClientSession) -> float:
    """Сколько гривен стоит $1: курс продажи monobank (рыночный), иначе официальный курс НБУ."""
    try:
        data = await _get_json(session, "https://api.monobank.ua/bank/currency")
        row = next(r for r in data if r["currencyCodeA"] == 840 and r["currencyCodeB"] == 980)
        return float(row.get("rateSell") or row["rateCross"])
    except Exception:
        data = await _get_json(session, "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&json")
        return float(data[0]["rate"])


# способ оплаты → функция получения курса (единиц валюты за $1)
AUTO_SOURCES: dict[str, Callable[[aiohttp.ClientSession], Awaitable[float]]] = {
    "btc": _btc_per_usd,
    "uah": _uah_per_usd,
}
SOURCE_NAMES = {"btc": "Binance", "uah": "monobank"}


def is_auto(method) -> bool:
    """Курс способа обновляется автоматически (по умолчанию — да, если есть источник)."""
    return method.key in AUTO_SOURCES and method.auto_rate is not False


async def update_rates(only: str | None = None) -> dict[str, float]:
    """Обновляет курсы всех «авто» способов (или одного). Возвращает новые значения."""
    updated: dict[str, float] = {}
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as http:
        for key, fetch in AUTO_SOURCES.items():
            if only and key != only:
                continue
            async with Session() as s:
                method = await billing.get_method(s, key)
            if method is None or not is_auto(method):
                continue
            try:
                rate = await fetch(http)
                if not 0 < rate < 10**7:
                    raise ValueError(f"странный курс {rate}")
            except Exception as e:
                log.warning("Не удалось обновить курс %s: %s", key, e)
                continue
            async with Session() as s:
                await billing.update_method(s, key, rate=rate, rate_updated_at=utcnow())
                await s.commit()
            updated[key] = rate
    return updated


class RateUpdater:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="rate-updater")

    async def _loop(self) -> None:
        while True:
            try:
                rates = await update_rates()
                if rates:
                    log.info("Курсы обновлены: %s", {k: f"{v:.8g}" for k, v in rates.items()})
            except Exception:
                log.exception("Ошибка обновления курсов")
            await asyncio.sleep(UPDATE_INTERVAL)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
