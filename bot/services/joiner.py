"""Вступление в чаты (с кулдауном), добавление папок и автоподписка на спонсоров.

Все вступления аккаунта идут через общий «гейт»: между двумя вступлениями — случайная пауза
JOIN_MIN..JOIN_MAX секунд и не более JOIN_DAILY вступлений в сутки. Так аккаунт не получает FloodWait/спамблок.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field

from telethon import events, utils
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.chatlists import CheckChatlistInviteRequest, JoinChatlistInviteRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import Channel, ChatInviteAlready
from telethon.tl.types.chatlists import ChatlistInviteAlready

from bot.utils.text import normalize_target

log = logging.getLogger(__name__)

JOIN_MIN, JOIN_MAX = 40, 120  # пауза между вступлениями, сек
JOIN_DAILY = 50  # вступлений в сутки на аккаунт
AFTER_JOIN_PAUSE = (3, 8)  # «осмотреться» после вступления, прежде чем писать

ADDLIST_RE = re.compile(r"(?:https?://)?t\.me/addlist/([\w-]+)", re.I)
TME_RE = re.compile(r"(?:https?://)?t\.me/[^\s)\]>\"']+", re.I)
CHECK_BTN_RE = re.compile(r"провер|подписа|готов|check|verify|joined|done|продолж|далее|я\s+в\b", re.I)
ASK_RE = re.compile(r"подпиш|подписа|subscribe|join|вступ", re.I)


class JoinCooldown(Exception):
    def __init__(self, secs: float):
        self.secs = secs


class JoinLimit(Exception):
    """Достигнут суточный лимит вступлений аккаунта."""


@dataclass
class Runner:
    id: int
    phone: str
    client: object
    sent_today: int = 0
    cooldown_until: float = 0.0
    dead: bool = False
    errors: int = 0
    cache: dict = field(default_factory=dict)
    next_join: float = 0.0
    joins_today: int = 0
    join_day: int = 0
    join_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set = field(default_factory=set)
    seen_msgs: set = field(default_factory=set)
    me_username: str = ""
    auto_subscribe: bool = True


async def _gate(r: Runner, wait: bool) -> None:
    today = int(time.time() // 86400)
    if r.join_day != today:
        r.join_day, r.joins_today = today, 0
    if r.joins_today >= JOIN_DAILY:
        raise JoinLimit
    left = r.next_join - time.time()
    if left > 0:
        if not wait:
            raise JoinCooldown(left)
        await asyncio.sleep(left)


def _joined(r: Runner) -> None:
    r.joins_today += 1
    r.next_join = time.time() + random.uniform(JOIN_MIN, JOIN_MAX)


async def _human_pause() -> None:
    await asyncio.sleep(random.uniform(*AFTER_JOIN_PAUSE))


# ───────────── папки (chatlist) ─────────────
async def inspect_folder(client, slug: str) -> tuple[str, list[str], int]:
    """-> (название, цели для рассылки, пропущено каналов без права писать)."""
    inv = await client(CheckChatlistInviteRequest(slug))
    title = getattr(inv, "title", None)
    title = getattr(title, "text", title) or "Папка"
    targets, skipped = [], 0
    for chat in inv.chats:
        if not isinstance(chat, Channel) and chat.__class__.__name__ != "Chat":
            continue
        if getattr(chat, "broadcast", False):  # в канал обычный аккаунт писать не может
            skipped += 1
            continue
        uname = getattr(chat, "username", None)
        targets.append("@" + uname.lower() if uname else str(utils.get_peer_id(chat)))
    return title, targets, skipped


async def ensure_folder(client, slug: str) -> int:
    """Добавляет папку на аккаунт, если её ещё нет (или не хватает чатов). -> сколько чатов добавлено."""
    inv = await client(CheckChatlistInviteRequest(slug))
    peers = inv.missing_peers if isinstance(inv, ChatlistInviteAlready) else inv.peers
    if not peers:
        return 0
    input_peers = [await client.get_input_entity(p) for p in peers]
    await client(JoinChatlistInviteRequest(slug, input_peers))
    return len(peers)


# ───────────── вступление в чат ─────────────
async def resolve(r: Runner, value: str, *, wait: bool = False):
    """value: @username | числовой id | +hash. Вступает, если аккаунта ещё нет в чате (через гейт-кулдаун)."""
    if value in r.cache:
        return r.cache[value]
    c = r.client
    if value.startswith("+"):
        chk = await c(CheckChatInviteRequest(value[1:]))
        if isinstance(chk, ChatInviteAlready):
            ent = chk.chat
        else:
            await _gate(r, wait)
            try:
                ent = (await c(ImportChatInviteRequest(value[1:]))).chats[0]
            except UserAlreadyParticipantError:
                ent = (await c(CheckChatInviteRequest(value[1:]))).chat
            _joined(r)
            await _human_pause()
    else:
        ent = await c.get_entity(value if value.startswith("@") else int(value))
        if getattr(ent, "left", False):
            await _gate(r, wait)
            await c(JoinChannelRequest(ent))
            _joined(r)
            await _human_pause()
    r.cache[value] = ent
    return ent


# ───────────── автоподписка на спонсоров ─────────────
def attach_sponsor_handler(r: Runner) -> None:
    """Если нас тегнули/ответили нам в чате, просят подписаться и есть кнопки — вступаем и жмём «проверить»."""

    async def handler(event):
        msg = event.message
        if not r.auto_subscribe or not msg.mentioned or event.is_private or msg.id in r.seen_msgs:
            return
        r.seen_msgs.add(msg.id)
        task = asyncio.create_task(_handle_sponsor(r, msg))
        r.tasks.add(task)
        task.add_done_callback(r.tasks.discard)

    r.client.add_event_handler(handler, events.NewMessage(incoming=True))


async def _handle_sponsor(r: Runner, msg) -> None:
    try:
        urls: list[str] = []
        callbacks = []
        for row in msg.buttons or []:
            for b in row:
                if b.url:
                    urls.append(b.url)
                elif b.data is not None:
                    callbacks.append(b)
        if not urls and ASK_RE.search(msg.raw_text or ""):  # без кнопок-ссылок: ссылки и @каналы из текста
            urls = TME_RE.findall(msg.raw_text or "")
            urls += [f"t.me/{u}" for u in re.findall(r"@([A-Za-z]\w{4,31})", msg.raw_text or "")
                     if u.lower() != r.me_username.lower()]
        if not urls:
            return

        joined_any = False
        async with r.join_lock:  # вступления строго по очереди, с кулдауном
            for url in dict.fromkeys(urls):
                try:
                    joined_any |= await _join_link(r, url)
                except JoinLimit:
                    log.info("sponsor: account %s hit daily join limit", r.id)
                    return
                except Exception as e:
                    log.info("sponsor join %s failed: %r", url, e)

        pick = [b for b in callbacks if CHECK_BTN_RE.search(b.text or "")] or (callbacks if len(callbacks) == 1 else [])
        if pick and joined_any:
            await asyncio.sleep(random.uniform(3, 7))
            await pick[0].click()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.info("sponsor handler error: %r", e)


async def _join_link(r: Runner, url: str) -> bool:
    m = ADDLIST_RE.search(url)
    if m:
        await _gate(r, True)
        n = await ensure_folder(r.client, m.group(1))
        if n:
            _joined(r)
        return bool(n)
    target = normalize_target(url if "t.me" in url else "t.me/" + url.lstrip("@"))
    if not target or target.lower().rstrip("_").endswith("bot"):  # на ботов не «подписываются»
        return False
    await resolve(r, target, wait=True)
    return True
