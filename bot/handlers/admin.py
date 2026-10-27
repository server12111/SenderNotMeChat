from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from bot.config import config
from bot.database.db import Session
from bot.database.models import Account, Mailing, SendLog, Subscription, User, utcnow
from bot.handlers.states import Admin
from bot.keyboards.admin import BACK_ADM, admin_menu, system_kb, user_card_kb
from bot.services import billing, settings
from bot.services.plans import PLANS, effective_plan, grant, lim, revoke
from bot.services.usage import day_start, sent_today_user
from bot.utils.text import SEP, fmt_dt
from bot.utils.ui import esc, ikb, render

log = logging.getLogger(__name__)
router = Router(name="admin")
# доступ строго по ADMIN_IDS — на уровне роутера, чужие апдейты сюда не доходят
router.message.filter(F.from_user.id.in_(config.admin_ids))
router.callback_query.filter(F.from_user.id.in_(config.admin_ids))


@router.message(F.text == "/admin")
async def cmd_admin(m: Message, state: FSMContext, session):
    await state.clear()
    sent = await m.answer(f"🛡 <b>Админ-панель</b>\n{SEP}", reply_markup=admin_menu(await billing.count_all_pending(session)))
    await state.update_data(anchor=sent.message_id)


@router.callback_query(F.data == "adm:main")
async def cb_main(call: CallbackQuery, state: FSMContext, session):
    await state.clear()
    await call.answer()
    await render(call, f"🛡 <b>Админ-панель</b>\n{SEP}\nВыберите раздел:", admin_menu(await billing.count_all_pending(session)), state)


# ───────────── дашборд ─────────────
@router.callback_query(F.data == "adm:dash")
async def cb_dash(call: CallbackQuery, session):
    await call.answer()
    now, today = utcnow(), day_start()

    async def one(q):
        return (await session.execute(q)).scalar_one()

    total = await one(select(func.count()).select_from(User))
    new = await one(select(func.count()).select_from(User).where(User.created_at >= today))
    active = await one(select(func.count()).select_from(User).where(User.last_seen >= now - timedelta(days=7)))
    running = await one(select(func.count()).select_from(Mailing).where(Mailing.is_active.is_(True)))
    sent_all = await one(select(func.count()).select_from(SendLog))
    sent_today = await one(select(func.count()).select_from(SendLog).where(SendLog.at >= today))
    paid = Subscription.plan.in_(("pro", "vip"))
    subs_active = await one(select(func.count()).select_from(Subscription).where(paid, Subscription.expires_at > now))
    subs_expired = await one(select(func.count()).select_from(Subscription).where(paid, Subscription.expires_at <= now))
    by_plan = (await session.execute(select(Subscription.plan, func.count()).where(
        paid, Subscription.expires_at > now).group_by(Subscription.plan))).all()
    plans_line = " · ".join(f"{PLANS[p].emoji} {n}" for p, n in by_plan if p in PLANS) or "—"
    text = (f"📊 <b>Дашборд</b>\n{SEP}\n"
            f"👥 Пользователей: <b>{total}</b>\n└ новых сегодня: <b>{new}</b> · активных (7д): <b>{active}</b>\n\n"
            f"🚀 Активных рассылок: <b>{running}</b>\n"
            f"📤 Отправлено: <b>{sent_all}</b> всего · <b>{sent_today}</b> сегодня\n\n"
            f"💎 Подписки: <b>{subs_active}</b> активных · <b>{subs_expired}</b> истекших\n└ {plans_line}")
    await render(call, text, ikb([[("🔄 Обновить", "adm:dash")], [BACK_ADM]]))


# ───────────── пользователи ─────────────
@router.callback_query(F.data == "adm:users")
async def cb_users(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(Admin.search)
    await render(call, f"👥 <b>Пользователи</b>\n{SEP}\nОтправьте <b>ID</b> или <b>@username</b>:", ikb([[BACK_ADM]]), state)


async def user_card(event, state, session, uid: int, prefix: str = ""):
    u = await session.get(User, uid)
    if not u:
        return await render(event, "❌ Пользователь не найден.", ikb([[("🔍 Искать снова", "adm:users"), BACK_ADM]]), state)
    plan, exp = await effective_plan(session, uid)
    n_acc = (await session.execute(select(func.count()).select_from(Account).where(Account.user_id == uid))).scalar_one()
    n_run = (await session.execute(select(func.count()).select_from(Mailing).where(
        Mailing.user_id == uid, Mailing.is_active.is_(True)))).scalar_one()
    text = (f"{prefix}👤 <b>{esc(u.full_name)}</b> {'⛔ ЗАБЛОКИРОВАН' if u.is_banned else ''}\n{SEP}\n"
            f"🆔 <code>{u.id}</code> · {('@' + esc(u.username)) if u.username else 'без username'}\n"
            f"{plan.emoji} Тариф: <b>{plan.title}</b> · до {fmt_dt(exp)} UTC\n"
            f"📱 Аккаунтов: <b>{n_acc}</b> · 🚀 Активных задач: <b>{n_run}</b>\n"
            f"📤 Сегодня: <b>{await sent_today_user(session, uid)}</b> / {lim(plan.daily_limit)}\n"
            f"📅 Регистрация: {fmt_dt(u.created_at)} · был: {fmt_dt(u.last_seen)}")
    await render(event, text, user_card_kb(uid, u.is_banned), state)


@router.message(Admin.search, F.text)
async def on_search(m: Message, state: FSMContext, session):
    q = m.text.strip().lstrip("@")
    cond = User.id == int(q) if q.isdigit() else func.lower(User.username) == q.lower()
    u = (await session.execute(select(User).where(cond))).scalar_one_or_none()
    await state.set_state(None)
    await user_card(m, state, session, u.id if u else 0)


@router.callback_query(F.data.startswith("adm:u:"))
async def cb_user(call: CallbackQuery, state: FSMContext, session):
    await call.answer()
    await user_card(call, state, session, int(call.data.split(":")[2]))


async def _tell(bot: Bot, uid: int, text: str) -> None:
    try:
        await bot.send_message(uid, text)
    except Exception:
        pass


@router.callback_query(F.data.startswith("adm:g:"))
async def cb_grant(call: CallbackQuery, state: FSMContext, session, bot: Bot):
    _, _, uid, plan_key, days = call.data.split(":")
    until = await grant(session, int(uid), plan_key, int(days))
    await call.answer(f"✅ {PLANS[plan_key].title} до {fmt_dt(until)}", show_alert=True)
    await _tell(bot, int(uid), f"🎁 Вам выдан тариф <b>{PLANS[plan_key].title}</b> до <b>{fmt_dt(until)} UTC</b>")
    await user_card(call, state, session, int(uid))


@router.callback_query(F.data.startswith("adm:x:"))
async def cb_extend(call: CallbackQuery, state: FSMContext, session, bot: Bot):
    _, _, uid, days = call.data.split(":")
    plan, _exp = await effective_plan(session, int(uid))
    if plan.key == "none":
        return await call.answer("У пользователя нет активного платного тарифа — выдайте PRO/VIP", show_alert=True)
    until = await grant(session, int(uid), plan.key, int(days))
    await call.answer(f"✅ Продлено до {fmt_dt(until)}", show_alert=True)
    await _tell(bot, int(uid), f"➕ Тариф <b>{plan.title}</b> продлён до <b>{fmt_dt(until)} UTC</b>")
    await user_card(call, state, session, int(uid))


@router.callback_query(F.data.startswith("adm:rv:"))
async def cb_revoke(call: CallbackQuery, state: FSMContext, session, bot: Bot):
    uid = int(call.data.split(":")[2])
    await revoke(session, uid)
    await call.answer("🚫 Тариф аннулирован", show_alert=True)
    await _tell(bot, uid, "⌛ Ваш тариф аннулирован администратором. Доступ к рассылкам приостановлен.")
    await user_card(call, state, session, uid)


@router.callback_query(F.data.startswith("adm:ban:"))
async def cb_ban(call: CallbackQuery, state: FSMContext, session, sender):
    uid = int(call.data.split(":")[2])
    u = await session.get(User, uid)
    if uid in config.admin_ids:
        return await call.answer("Нельзя заблокировать администратора", show_alert=True)
    u.is_banned = not u.is_banned
    await session.commit()
    if u.is_banned:  # остановить все активные рассылки заблокированного
        ids = (await session.execute(select(Mailing.id).where(
            Mailing.user_id == uid, Mailing.is_active.is_(True)))).scalars().all()
        for mid in ids:
            await sender.deactivate(mid)
    await call.answer("⛔ Заблокирован, рассылки остановлены" if u.is_banned else "✅ Разблокирован", show_alert=True)
    await user_card(call, state, session, uid)


@router.callback_query(F.data.startswith("adm:acc:"))
async def cb_user_accounts(call: CallbackQuery, session):
    uid = int(call.data.split(":")[2])
    accs = (await session.execute(select(Account).where(Account.user_id == uid))).scalars().all()
    await call.answer()
    lines = [f"{'🟢' if a.status == 'ok' and a.is_active else '🔴'} <code>{esc(a.phone)}</code> · {esc(a.name)}"
             + (" · 🧦" if a.proxy else "") for a in accs] or ["Аккаунтов нет"]
    await render(call, f"📱 <b>Аккаунты {uid}</b>\n{SEP}\n" + "\n".join(lines), ikb([[("◀️ К карточке", f"adm:u:{uid}")]]))


@router.callback_query(F.data.startswith("adm:tasks:"))
async def cb_user_tasks(call: CallbackQuery, session):
    uid = int(call.data.split(":")[2])
    items = (await session.execute(select(Mailing).where(Mailing.user_id == uid).order_by(Mailing.id.desc()).limit(10))).scalars().all()
    await call.answer()
    lines = [f"{'🟢' if c.is_active else '🔴'} #{c.id} · {esc(c.name[:30])} · цикл {c.cycle + 1} · ✅{c.cycle_sent} ❌{c.cycle_failed}"
             for c in items] or ["Рассылок нет"]
    await render(call, f"🚀 <b>Задачи {uid}</b>\n{SEP}\n" + "\n".join(lines), ikb([[("◀️ К карточке", f"adm:u:{uid}")]]))


# ───────────── сервисная рассылка ─────────────
@router.callback_query(F.data == "adm:bc")
async def cb_bc(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(Admin.bc_msg)
    await render(call, f"📢 <b>Сервисная рассылка</b> · 1/3\n{SEP}\nОтправьте сообщение (текст/фото/видео) — оно уйдёт <b>всем</b> пользователям бота:",
                 ikb([[BACK_ADM]]), state)


@router.message(Admin.bc_msg)
async def on_bc_msg(m: Message, state: FSMContext):
    await state.update_data(bc_chat=m.chat.id, bc_mid=m.message_id)
    await state.set_state(Admin.bc_btn)
    await render(m, f"📢 <b>Сервисная рассылка</b> · 2/3\n{SEP}\nКнопки-ссылки, по одной на строку:\n<code>Текст | https://example.com</code>\n\nИли <code>-</code> без кнопок.",
                 ikb([[BACK_ADM]]), state)


def _parse_buttons(raw: str):
    rows = []
    for line in raw.splitlines():
        if "|" in line:
            t, u = (x.strip() for x in line.split("|", 1))
            if t and u.startswith(("http://", "https://", "tg://")):
                rows.append([(t, u)])
    return ikb(rows) if rows else None


@router.message(Admin.bc_btn, F.text)
async def on_bc_btn(m: Message, state: FSMContext, session):
    kb = _parse_buttons(m.text) if m.text.strip() != "-" else None
    await state.update_data(bc_buttons=None if kb is None else m.text)
    total = (await session.execute(select(func.count()).select_from(User).where(User.is_banned.is_(False)))).scalar_one()
    d = await state.get_data()
    await state.set_state(None)
    await render(m, f"📢 <b>Сервисная рассылка</b> · 3/3\n{SEP}\nПолучателей: <b>{total}</b>\nКнопок: <b>{len(kb.inline_keyboard) if kb else 0}</b>\n\nПредпросмотр ниже ⬇️",
                 ikb([[("🚀 Отправить всем", "adm:bc:go")], [BACK_ADM]]), state)
    await m.bot.copy_message(m.chat.id, d["bc_chat"], d["bc_mid"], reply_markup=kb)


@router.callback_query(F.data == "adm:bc:go")
async def cb_bc_go(call: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    if "bc_mid" not in d:
        return await call.answer("Черновик устарел", show_alert=True)
    await state.clear()
    await call.answer("🚀 Рассылка пошла", show_alert=False)
    kb = _parse_buttons(d["bc_buttons"]) if d.get("bc_buttons") else None
    status = await call.message.edit_text("📢 Отправка… 0%")
    asyncio.create_task(_broadcast(bot, status.chat.id, status.message_id, d["bc_chat"], d["bc_mid"], kb))


async def _broadcast(bot: Bot, chat_id: int, status_mid: int, src_chat: int, src_mid: int, kb) -> None:
    async with Session() as s:
        ids = (await s.execute(select(User.id).where(User.is_banned.is_(False)))).scalars().all()
    ok = fail = 0
    for i, uid in enumerate(ids, 1):
        try:
            await bot.copy_message(uid, src_chat, src_mid, reply_markup=kb)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.copy_message(uid, src_chat, src_mid, reply_markup=kb)
                ok += 1
            except Exception:
                fail += 1
        except TelegramForbiddenError:
            fail += 1  # пользователь заблокировал бота
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)  # ~20 сообщений/сек, лимит Telegram — 30
        if i % 50 == 0:
            try:
                await bot.edit_message_text(f"📢 Отправка… {i * 100 // len(ids)}% ({i}/{len(ids)})", chat_id=chat_id, message_id=status_mid)
            except Exception:
                pass
    await bot.edit_message_text(f"📢 <b>Рассылка завершена</b>\n{SEP}\n✅ Доставлено: <b>{ok}</b>\n❌ Не доставлено: <b>{fail}</b>",
                                chat_id=chat_id, message_id=status_mid, reply_markup=ikb([[BACK_ADM]]))


# ───────────── системные тумблеры ─────────────
DELAY_CYCLE = [0, 10, 20, 30, 60]
CAP_CYCLE = [0, 100, 500, 1000, 5000]


async def _sys(call, session, state=None):
    m = await settings.get(session, settings.MAINTENANCE) == "1"
    gd = await settings.get_int(session, settings.GLOBAL_MIN_DELAY)
    gc = await settings.get_int(session, settings.GLOBAL_DAILY_CAP)
    await render(call, f"⚙️ <b>Система</b>\n{SEP}\n"
                       f"🛠 Техобслуживание закрывает бота для всех, кроме админов.\n"
                       f"⏱ Глобальный минимум задержки перекрывает настройки тарифов (в большую сторону).\n"
                       f"📉 Глобальный потолок сообщений/сутки на пользователя (меньший из тарифа и потолка).",
                 system_kb(m, gd, gc), state)


@router.callback_query(F.data == "adm:sys")
async def cb_sys(call: CallbackQuery, session):
    await call.answer()
    await _sys(call, session)


@router.callback_query(F.data.startswith("adm:sys:"))
async def cb_sys_toggle(call: CallbackQuery, session):
    what = call.data.split(":")[2]
    if what == "maint":
        new = "0" if await settings.get(session, settings.MAINTENANCE) == "1" else "1"
        await settings.set_(session, settings.MAINTENANCE, new)
        await call.answer("🛠 Режим техобслуживания " + ("ВКЛЮЧЁН" if new == "1" else "выключен"), show_alert=new == "1")
    else:
        key, cycle = (settings.GLOBAL_MIN_DELAY, DELAY_CYCLE) if what == "delay" else (settings.GLOBAL_DAILY_CAP, CAP_CYCLE)
        cur = await settings.get_int(session, key)
        nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else 0
        await settings.set_(session, key, nxt)
        await call.answer(f"Установлено: {nxt or 'выкл'}")
    await _sys(call, session)
