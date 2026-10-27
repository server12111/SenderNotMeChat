"""Общий пул Telethon-клиентов: один живой клиент на аккаунт, даже если им пользуются несколько рассылок.

Две одновременные сессии с одним auth_key дают AuthKeyDuplicatedError и слёт аккаунта, поэтому все
(рассылки, проверка аккаунта, чтение папок) идут через этот менеджер. Клиент живёт, пока на него
есть ссылки (acquire/release), и закрывается, когда их не осталось.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from sqlalchemy import update
from telethon.errors import (
    AuthKeyDuplicatedError, AuthKeyUnregisteredError, SessionExpiredError, SessionRevokedError,
    UserDeactivatedBanError, UserDeactivatedError,
)

from bot.database.db import Session
from bot.database.models import Account, utcnow
from bot.services.accounts import check_proxy, client_for
from bot.services.joiner import Runner, attach_sponsor_handler

log = logging.getLogger(__name__)

DEAD_ERRORS = (AuthKeyUnregisteredError, AuthKeyDuplicatedError, SessionRevokedError, SessionExpiredError,
               UserDeactivatedError, UserDeactivatedBanError)


class ClientError(Exception):
    """Не удалось поднять клиент; текст — для пользователя. permanent — повторять бессмысленно."""

    def __init__(self, text: str, permanent: bool = False):
        super().__init__(text)
        self.permanent = permanent


class ClientManager:
    def __init__(self) -> None:
        self._runners: dict[int, Runner] = {}
        self._refs: dict[int, int] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def peek(self, account_id: int) -> Runner | None:
        return self._runners.get(account_id)

    async def acquire(self, acc: Account) -> Runner:
        """Вернуть подключённый Runner аккаунта (+1 ссылка). Бросает ClientError."""
        lock = self._locks.setdefault(acc.id, asyncio.Lock())
        async with lock:
            r = self._runners.get(acc.id)
            if r is not None and r.client.is_connected() and not r.dead:
                self._refs[acc.id] = self._refs.get(acc.id, 0) + 1
                r.auto_subscribe = acc.auto_subscribe
                return r
            if r is not None:
                await self._close(acc.id)

            if acc.status == "dead":
                raise ClientError("аккаунт отключён от Telegram — подключите заново", permanent=True)
            # SOCKS5 проверяем ДО подключения: иначе аккаунт засветит реальный IP
            if acc.proxy and not await check_proxy(acc.proxy):
                raise ClientError("прокси недоступен")
            client = client_for(acc, listen=True)
            try:
                await asyncio.wait_for(client.connect(), 30)
                if not await client.is_user_authorized():
                    raise AuthKeyUnregisteredError(request=None)
                me = await client.get_me()
            except DEAD_ERRORS:
                await self._mark(acc.id, status="dead")
                await client.disconnect()
                raise ClientError("сессия отозвана или аккаунт заблокирован", permanent=True)
            except Exception as e:
                await client.disconnect()
                raise ClientError(f"не удалось подключиться ({type(e).__name__})")

            r = Runner(acc.id, acc.phone, client, me_username=me.username or "", auto_subscribe=acc.auto_subscribe)
            if acc.cooldown_until and acc.cooldown_until > utcnow():
                r.cooldown_until = time.time() + (acc.cooldown_until - utcnow()).total_seconds()
            elif acc.status == "limited":
                await self._mark(acc.id, status="ok", cooldown_until=None)
            attach_sponsor_handler(r)
            self._runners[acc.id] = r
            self._refs[acc.id] = 1
            return r

    async def release(self, account_id: int) -> None:
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            n = self._refs.get(account_id, 0) - 1
            if n > 0:
                self._refs[account_id] = n
            else:
                await self._close(account_id)

    async def drop(self, account_id: int) -> None:
        """Принудительно закрыть (аккаунт удалён / сменили прокси)."""
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            await self._close(account_id)

    async def stop_all(self) -> None:
        for aid in list(self._runners):
            await self.drop(aid)

    def set_auto_subscribe(self, account_id: int, enabled: bool) -> None:
        r = self._runners.get(account_id)
        if r:
            r.auto_subscribe = enabled

    async def _close(self, account_id: int) -> None:
        r = self._runners.pop(account_id, None)
        self._refs.pop(account_id, None)
        if r is None:
            return
        for t in list(r.tasks):
            t.cancel()
        try:
            await r.client.disconnect()
        except Exception:
            pass

    # ───────────── состояние аккаунта в БД ─────────────
    @staticmethod
    async def _mark(account_id: int, **vals) -> None:
        async with Session() as s:
            await s.execute(update(Account).where(Account.id == account_id).values(**vals))
            await s.commit()

    async def mark_dead(self, r: Runner) -> None:
        r.dead = True
        await self._mark(r.id, status="dead")

    async def cooldown(self, r: Runner, seconds: float, status: str | None = None) -> None:
        r.cooldown_until = time.time() + seconds
        vals = {"cooldown_until": utcnow() + timedelta(seconds=seconds)}
        if status:
            vals["status"] = status
        await self._mark(r.id, **vals)

    async def check(self, acc: Account) -> None:
        """Проверка живости. Если клиент уже в работе — спрашиваем его же (вторую сессию не открываем)."""
        r = self._runners.get(acc.id)
        if r is not None:
            await r.client.get_me()
            return
        r = await self.acquire(acc)
        try:
            await r.client.get_me()
        finally:
            await self.release(acc.id)
