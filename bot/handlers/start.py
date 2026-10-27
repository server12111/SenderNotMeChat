from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from bot.database.models import Account, Mailing, User
from bot.handlers.accounts import drop_pending
from bot.keyboards.menus import main_menu, settings_kb
from bot.services.plans import lim
from bot.services.usage import remaining_today
from bot.utils.text import SEP, fmt_dt, tz_label
from bot.utils.ui import esc, render

router = Router(name="start")


async def menu_text(session, user: User, plan, expires) -> str:
    _, cap, used = await remaining_today(session, user.id, plan)
    if plan.key == "none":
        tariff = "⛔ Тариф: <b>не активен</b>\n💎 Оформите PRO или VIP в разделе «Подписка»"
    else:
        tariff = (f"{plan.emoji} Тариф: <b>{plan.title}</b> · до {fmt_dt(expires, user.tz_offset)}\n"
                  f"📊 Сегодня отправлено: <b>{used}</b> / {lim(cap)}")
    return f"🚀 <b>Sender Pro</b>\n{SEP}\n👤 {esc(user.full_name)}\n{tariff}\n{SEP}\nВыберите раздел 👇"


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(m: Message, state: FSMContext, session, user, plan, plan_expires, is_admin):
    await state.clear()
    sent = await m.answer(await menu_text(session, user, plan, plan_expires), reply_markup=main_menu(is_admin))
    await state.update_data(anchor=sent.message_id)


@router.callback_query(F.data == "menu:main")
async def cb_menu(call: CallbackQuery, state: FSMContext, session, user, plan, plan_expires, is_admin):
    await state.clear()
    await call.answer()
    await render(call, await menu_text(session, user, plan, plan_expires), main_menu(is_admin), state)


@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext, session, user, plan, plan_expires, is_admin):
    """Универсальная «Отмена» любого пошагового ввода."""
    await state.clear()
    await drop_pending(user.id)
    await call.answer("Отменено")
    await render(call, await menu_text(session, user, plan, plan_expires), main_menu(is_admin), state)


# ───────────── профиль и настройки ─────────────
async def _settings_text(session, user: User, plan, expires) -> str:
    accs = (await session.execute(select(func.count()).select_from(Account).where(Account.user_id == user.id))).scalar_one()
    camps = (await session.execute(select(func.count()).select_from(Mailing).where(Mailing.user_id == user.id))).scalar_one()
    until = fmt_dt(expires, user.tz_offset) if expires else "—"
    return (f"⚙️ <b>Настройки и профиль</b>\n{SEP}\n"
            f"🆔 ID: <code>{user.id}</code>\n"
            f"{plan.emoji} Тариф: <b>{plan.title}</b> · {until}\n"
            f"📱 Аккаунтов: <b>{accs}</b> · 🚀 Рассылок: <b>{camps}</b>\n"
            f"📅 С нами с {fmt_dt(user.created_at, user.tz_offset)[:10]}\n{SEP}\n"
            f"Часовой пояс: <b>{tz_label(user.tz_offset)}</b> (для расписания)")


@router.callback_query(F.data == "set:main")
async def cb_settings(call: CallbackQuery, session, user, plan, plan_expires):
    await call.answer()
    await render(call, await _settings_text(session, user, plan, plan_expires), settings_kb(user.tz_offset, user.notify))


@router.callback_query(F.data.startswith("set:"))
async def cb_settings_action(call: CallbackQuery, session, user, plan, plan_expires):
    action = call.data.split(":")[1]
    if action == "tz":
        user.tz_offset = max(-12, min(14, user.tz_offset + int(call.data.split(":")[2])))
        await call.answer(f"Часовой пояс: {tz_label(user.tz_offset)}")
    elif action == "notify":
        user.notify = not user.notify
        await call.answer("🔔 Уведомления включены" if user.notify else "🔕 Уведомления выключены")
    else:
        return await call.answer()
    await render(call, await _settings_text(session, user, plan, plan_expires), settings_kb(user.tz_offset, user.notify))
