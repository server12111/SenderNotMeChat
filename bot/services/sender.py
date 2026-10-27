"""Рассылки: по одной фоновой задаче на включённую рассылку.

Цикл рассылки:  круг по всем чатам (пауза «КД между чатами» после каждой отправки)
                -> пауза «КД между циклами» -> следующий круг -> ...
Состояние целиком в БД (цикл, у каждого чата done_cycle, next_cycle_at), поэтому после рестарта
start() просто поднимает задачи включённых рассылок и они продолжают с того же места.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import random
import time
from dataclasses import dataclass
from datetime import timedelta

from aiogram import Bot
from sqlalchemy import func, select, update
from telethon.errors import (
    ChannelPrivateError, ChatAdminRequiredError, ChatGuestSendForbiddenError, ChatRestrictedError,
    ChatWriteForbiddenError, FloodWaitError, InviteHashExpiredError, InviteHashInvalidError,
    InviteRequestSentError, PeerFloodError, PeerIdInvalidError, RPCError, SlowModeWaitError,
    UserBannedInChannelError, UserNotParticipantError, UsernameInvalidError, UsernameNotOccupiedError,
)

from bot.database.db import Session
from bot.database.models import Account, Mailing, MailingMessage, MailingTarget, SendLog, User, utcnow
from bot.services import settings
from bot.services.clients import DEAD_ERRORS, ClientError, ClientManager
from bot.services.joiner import JoinCooldown, JoinLimit, Runner, ensure_folder, resolve
from bot.services.plans import Plan, clamp_delays, effective_plan
from bot.services.usage import day_start, sent_today_user, user_daily_cap
from bot.utils.text import SEP, fmt_dt, hours_wait, render_spintax

log = logging.getLogger(__name__)

# проблема конкретного чата: пишем в last_error и идём дальше (следующий цикл попробуем снова)
TARGET_ERRORS = (ChatWriteForbiddenError, UserBannedInChannelError, ChannelPrivateError, ChatAdminRequiredError,
                 ChatGuestSendForbiddenError, ChatRestrictedError, UsernameNotOccupiedError, UsernameInvalidError,
                 InviteHashExpiredError, InviteHashInvalidError, InviteRequestSentError, SlowModeWaitError,
                 UserNotParticipantError, PeerIdInvalidError)

LONG_WAIT = 15 * 60  # аккаунты ждать дольше — отпускаем клиентов и спим


class PrioritySlots:
    """Ограничитель одновременных отправок: освободившийся слот получает самый приоритетный (VIP) из ожидающих."""

    def __init__(self, n: int):
        self.free = n
        self._heap: list = []
        self._seq = itertools.count()

    async def acquire(self, prio: int) -> None:
        if self.free > 0 and not self._heap:
            self.free -= 1
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (prio, next(self._seq), fut))
        await fut

    def release(self) -> None:
        while self._heap:
            _, _, fut = heapq.heappop(self._heap)
            if not fut.done():
                fut.set_result(None)
                return
        self.free += 1


@dataclass
class Snap:
    id: int
    user_id: int
    name: str
    active: bool
    account_ids: list
    interval: int
    dmin: int
    dmax: int
    spintax: bool
    hours: list | None
    folders: list
    cycle: int
    next_cycle_at: object
    plan: Plan
    tz: int


class SenderService:
    def __init__(self, bot: Bot, clients: ClientManager, max_running: int = 20):
        self.bot = bot
        self.clients = clients
        self.slots = PrioritySlots(max_running)
        self._tasks: dict[int, asyncio.Task] = {}
        self._wake: dict[int, asyncio.Event] = {}

    # ───────────── управление ─────────────
    async def start(self) -> None:
        async with Session() as s:
            ids = (await s.execute(select(Mailing.id).where(Mailing.is_active.is_(True)))).scalars().all()
        for mid in ids:
            self._spawn(mid)
        log.info("SenderService: resumed %s mailings", len(ids))

    async def stop(self) -> None:
        for t in list(self._tasks.values()):
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        await self.clients.stop_all()

    async def activate(self, mid: int) -> None:
        async with Session() as s:
            await s.execute(update(Mailing).where(Mailing.id == mid).values(is_active=True))
            await s.commit()
        self._spawn(mid)

    async def deactivate(self, mid: int) -> None:
        async with Session() as s:
            await s.execute(update(Mailing).where(Mailing.id == mid).values(is_active=False))
            await s.commit()
        self._poke(mid)

    def is_running(self, mid: int) -> bool:
        return mid in self._tasks

    def _spawn(self, mid: int) -> None:
        if mid in self._tasks:
            self._poke(mid)
            return
        self._tasks[mid] = asyncio.create_task(self._supervise(mid))

    def _poke(self, mid: int) -> None:
        ev = self._wake.get(mid)
        if ev:
            ev.set()

    async def _sleep(self, mid: int, seconds: float) -> None:
        """Прерываемый сон: выключение рассылки будит задачу мгновенно."""
        ev = self._wake.setdefault(mid, asyncio.Event())
        ev.clear()
        try:
            await asyncio.wait_for(ev.wait(), max(0.5, seconds))
        except asyncio.TimeoutError:
            pass

    async def _supervise(self, mid: int) -> None:
        me = asyncio.current_task()
        try:
            while True:
                try:
                    await self._loop(mid)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("mailing %s crashed, restart in 30s", mid)
                    await asyncio.sleep(30)
        finally:
            if self._tasks.get(mid) is me:
                self._tasks.pop(mid, None)
            self._wake.pop(mid, None)

    # ───────────── основной цикл ─────────────
    async def _loop(self, mid: int) -> None:
        held: dict[int, Runner] = {}  # account_id -> Runner (мы держим ссылку в ClientManager)
        folders_done: set[int] = set()
        warned: set[tuple] = set()
        idx = -1
        since_long = 0
        cap_notified_day = None
        try:
            while True:
                m = await self._snap(mid)
                if m is None or not m.active:
                    return
                if m.plan.key == "none":
                    await self._halt(m, "нет активной подписки. Продлите тариф и запустите рассылку снова")
                    return

                now = utcnow()
                if m.next_cycle_at and m.next_cycle_at > now:
                    await self._release_all(held, folders_done)
                    await self._sleep(mid, (m.next_cycle_at - now).total_seconds())
                    continue

                wait = hours_wait(m.hours, now)
                if wait > 0:  # вне времени активности
                    await self._release_all(held, folders_done)
                    await self._sleep(mid, wait)
                    continue

                async with Session() as s:
                    cap = await user_daily_cap(s, m.plan)
                    used = await sent_today_user(s, m.user_id)
                    g_min = await settings.get_int(s, settings.GLOBAL_MIN_DELAY)
                    msgs = (await s.execute(select(MailingMessage).where(MailingMessage.mailing_id == mid))).scalars().all()
                if used >= cap:
                    await self._release_all(held, folders_done)
                    if cap_notified_day != day_start():
                        cap_notified_day = day_start()
                        await self._notify(m.user_id, f"⏳ Рассылка «{m.name}»: достигнут дневной лимит ({cap}). Продолжу в 00:00 UTC.", force=True)
                    await self._sleep(mid, (day_start() + timedelta(days=1, seconds=5) - now).total_seconds())
                    continue
                if not msgs:
                    await self._halt(m, "нет ни одного сообщения")
                    return

                target = await self._next_target(mid, m.cycle)
                if target is None:
                    if await self._count_targets(mid) == 0:
                        await self._halt(m, "нет целевых чатов")
                        return
                    await self._finish_cycle(m)
                    await self._release_all(held, folders_done)
                    continue

                alive = await self._ensure_runners(m, held, folders_done, warned)
                if alive is None:
                    return  # рассылка остановлена (все аккаунты непригодны)
                if not alive:
                    await self._sleep(mid, 120)  # временные проблемы (сеть/прокси) — повторим
                    continue

                i, wait = self._pick(held, m.account_ids, idx)
                if i is None:
                    if wait is None:
                        await self._halt(m, "все аккаунты рассылки отключены")
                        return
                    if wait > LONG_WAIT:
                        await self._release_all(held, folders_done)
                    await self._sleep(mid, wait)
                    continue
                idx = i
                r = held[idx]

                msg = random.choice(msgs)
                await self.slots.acquire(m.plan.priority)
                try:
                    outcome = await self._send_one(m, r, target, msg)
                finally:
                    self.slots.release()

                if outcome in ("sent", "failed"):
                    since_long += 1
                    dmin, dmax = clamp_delays(m.plan, m.dmin, m.dmax, g_min)
                    delay = random.uniform(dmin, dmax)
                    if m.plan.extended:  # VIP: «человеческая» вариативность и редкие длинные паузы
                        delay *= random.uniform(0.85, 1.25)
                        if since_long >= random.randint(6, 14):
                            delay += random.uniform(dmax, dmax * 3)
                            since_long = 0
                    await self._sleep(mid, delay)
        finally:
            await self._release_all(held, folders_done)

    # ───────────── аккаунты ─────────────
    async def _ensure_runners(self, m: Snap, held: dict[int, Runner], folders_done: set, warned: set) -> list | None:
        """Подключает недостающие аккаунты. -> список живых; [] — временные проблемы; None — рассылка остановлена."""
        for aid in [a for a in held if a not in m.account_ids]:
            await self.clients.release(aid)
            held.pop(aid)
            folders_done.discard(aid)
        permanent = 0
        async with Session() as s:
            accs = (await s.execute(select(Account).where(
                Account.id.in_(m.account_ids), Account.user_id == m.user_id, Account.is_active.is_(True)))).scalars().all()
        for acc in accs:
            if acc.id in held and not held[acc.id].dead and held[acc.id].client.is_connected():
                held[acc.id].auto_subscribe = acc.auto_subscribe
                continue
            if acc.id in held:
                await self.clients.release(acc.id)
                held.pop(acc.id)
            try:
                r = await self.clients.acquire(acc)
            except ClientError as e:
                if e.permanent:
                    permanent += 1
                if (acc.id, str(e)) not in warned:
                    warned.add((acc.id, str(e)))
                    await self._notify(m.user_id, f"⚠️ Рассылка «{m.name}», аккаунт <b>{acc.phone}</b>: {e}.", force=True)
                continue
            held[acc.id] = r
        for aid, r in list(held.items()):  # папки: добавляем на каждый аккаунт, если их там ещё нет (раз за цикл)
            if aid in folders_done:
                continue
            folders_done.add(aid)
            for slug in m.folders:
                try:
                    await ensure_folder(r.client, slug)
                except FloodWaitError as e:
                    await self.clients.cooldown(r, e.seconds + 1)
                except Exception as e:
                    log.warning("folder %s on account %s: %r", slug, aid, e)
                    await self._notify(m.user_id, f"⚠️ Не удалось добавить папку на <b>{r.phone}</b>: <code>{type(e).__name__}</code>", force=True)
        if not held and accs and permanent == len(accs):
            await self._halt(m, "все аккаунты рассылки отключены или недоступны")
            return None
        if not held and not accs:
            await self._halt(m, "у рассылки нет доступных аккаунтов")
            return None
        return list(held.values())

    async def _release_all(self, held: dict[int, Runner], folders_done: set) -> None:
        for aid in list(held):
            await self.clients.release(aid)
        held.clear()
        folders_done.clear()

    @staticmethod
    def _pick(held: dict[int, Runner], order: list, last: int) -> tuple[int | None, float | None]:
        """Ротация аккаунтов по кругу. -> (account_id, None) | (None, секунд_ожидания) | (None, None)."""
        ids = [a for a in order if a in held and not held[a].dead]
        if not ids:
            return None, None
        now = time.time()
        start = ids.index(last) + 1 if last in ids else 0
        for step in range(len(ids)):
            aid = ids[(start + step) % len(ids)]
            if held[aid].cooldown_until <= now:
                return aid, None
        return None, max(1.0, min(held[a].cooldown_until for a in ids) - now)

    # ───────────── отправка ─────────────
    async def _send_one(self, m: Snap, r: Runner, target: tuple[int, str], msg: MailingMessage) -> str:
        """-> sent | failed | retry (цель остаётся в очереди цикла, аккаунт ушёл в cooldown)."""
        tid, value = target
        try:
            entity = await resolve(r, value)
            text = render_spintax(msg.text) if m.spintax else msg.text
            if msg.media_path:
                await r.client.send_file(entity, msg.media_path, caption=text or None, parse_mode="html")
            else:
                await r.client.send_message(entity, text, parse_mode="html", link_preview=False)
        except FloodWaitError as e:
            # не роняем рассылку: аккаунт «остывает» e.seconds, остальные продолжают
            log.info("FloodWait %ss on account %s", e.seconds, r.id)
            await self.clients.cooldown(r, e.seconds + 1)
            return "retry"
        except PeerFloodError:
            await self.clients.cooldown(r, 6 * 3600, status="limited")
            await self._notify(m.user_id, f"🟠 Аккаунт <b>{r.phone}</b> получил PeerFlood (спам-ограничение) и отдыхает 6 ч.", force=True)
            return "retry"
        except DEAD_ERRORS:
            await self.clients.mark_dead(r)
            await self._notify(m.user_id, f"🔴 Аккаунт <b>{r.phone}</b> отключён или заблокирован Telegram.", force=True)
            return "retry"
        except JoinCooldown as e:  # кулдаун вступлений: чат остаётся в очереди, аккаунт ждёт
            r.cooldown_until = time.time() + e.secs
            return "retry"
        except JoinLimit:
            await self._mark(m, tid, r, "JoinDailyLimit")
            return "failed"
        except TARGET_ERRORS as e:
            await self._mark(m, tid, r, type(e).__name__.removesuffix("Error"))
            return "failed"
        except (ConnectionError, OSError, asyncio.TimeoutError):
            r.errors += 1
            await self.clients.cooldown(r, min(60 * r.errors, 600))
            return "retry"
        except RPCError as e:
            await self._mark(m, tid, r, type(e).__name__.removesuffix("Error"))
            return "failed"
        except Exception as e:
            log.warning("send failed (%s): %r", value, e)
            await self._mark(m, tid, r, type(e).__name__)
            return "failed"
        r.errors = 0
        await self._mark(m, tid, r, None)
        return "sent"

    # ───────────── БД ─────────────
    @staticmethod
    async def _snap(mid: int) -> Snap | None:
        async with Session() as s:
            c = await s.get(Mailing, mid)
            if not c:
                return None
            plan, _ = await effective_plan(s, c.user_id)
            u = await s.get(User, c.user_id)
            return Snap(c.id, c.user_id, c.name, c.is_active, list(c.account_ids or []), c.interval_seconds,
                        c.delay_min, c.delay_max, c.spintax, c.hours, list(c.folders or []), c.cycle,
                        c.next_cycle_at, plan, u.tz_offset if u else 0)

    @staticmethod
    async def _next_target(mid: int, cycle: int) -> tuple[int, str] | None:
        async with Session() as s:
            row = (await s.execute(select(MailingTarget.id, MailingTarget.target).where(
                MailingTarget.mailing_id == mid, MailingTarget.done_cycle < cycle)
                .order_by(MailingTarget.id).limit(1))).first()
        return (row[0], row[1]) if row else None

    @staticmethod
    async def _count_targets(mid: int) -> int:
        async with Session() as s:
            return (await s.execute(select(func.count()).select_from(MailingTarget).where(MailingTarget.mailing_id == mid))).scalar_one()

    @staticmethod
    async def _mark(m: Snap, tid: int, r: Runner, error: str | None) -> None:
        ok = error is None
        async with Session() as s:
            await s.execute(update(MailingTarget).where(MailingTarget.id == tid).values(done_cycle=m.cycle, last_error=error))
            vals = {"cycle_sent": Mailing.cycle_sent + 1, "last_sent_at": utcnow()} if ok else {"cycle_failed": Mailing.cycle_failed + 1}
            await s.execute(update(Mailing).where(Mailing.id == m.id).values(**vals))
            if ok:
                s.add(SendLog(user_id=m.user_id, account_id=r.id, mailing_id=m.id))
            await s.commit()

    async def _finish_cycle(self, m: Snap) -> None:
        async with Session() as s:
            c = await s.get(Mailing, m.id)
            sent, failed = c.cycle_sent, c.cycle_failed
            c.cycle += 1
            c.cycle_sent = c.cycle_failed = 0
            c.next_cycle_at = utcnow() + timedelta(seconds=c.interval_seconds)
            nxt = c.next_cycle_at
            await s.commit()
        await self._notify(m.user_id, (
            f"✅ <b>Цикл №{m.cycle + 1} · «{m.name}»</b>\n{SEP}\n"
            f"✅ Доставлено: <b>{sent}</b>\n❌ Ошибки: <b>{failed}</b>\n"
            f"⏭ Следующий цикл: <b>{fmt_dt(nxt, m.tz)}</b>"))

    async def _halt(self, m: Snap, reason: str) -> None:
        await self.deactivate(m.id)
        await self._notify(m.user_id, f"⏹ <b>Рассылка «{m.name}» остановлена:</b> {reason}.", force=True)

    async def _notify(self, user_id: int, text: str, force: bool = False) -> None:
        try:
            if not force:
                async with Session() as s:
                    u = await s.get(User, user_id)
                    if u and not u.notify:
                        return
            await self.bot.send_message(user_id, text)
        except Exception:
            pass
