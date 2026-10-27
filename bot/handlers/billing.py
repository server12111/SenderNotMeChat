"""Покупка тарифа: тарифы → способ оплаты → заявка с реквизитами → «Я оплатил» → проверка админом."""
from __future__ import annotations

import html
import json
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.handlers.states import PromoFSM
from bot.keyboards import billing as K
from bot.database.models import Order, OrderStatus, fmt_usd
from bot.services import billing
from bot.services.plans import PLANS, lim
from bot.utils.text import fmt_dt
from bot.utils.ui import render

router = Router(name="billing")
log = logging.getLogger(__name__)

MAX_PENDING_PER_USER = 3

PERKS = {
    "pro": "до {a} аккаунтов · без лимита сообщений · спинтакс · автоповтор",
    "vip": "безлимит аккаунтов и сообщений · приоритетная очередь · SOCKS5-прокси · расширенная рандомизация",
}


def perks(tier: str) -> str:
    p = PLANS.get(tier)
    return PERKS.get(tier, "").format(a=lim(p.max_accounts) if p else "")


# ───────────────────────────── тарифы ─────────────────────────────
async def plans_text(session, user, plan, expires) -> str:
    plans = await billing.list_plans(session)
    cur = (f"{plan.emoji} Ваш тариф: <b>{plan.title}</b> · до {fmt_dt(expires, user.tz_offset)}" if expires
           else "⛔ Подписка не активна")
    if plans:
        lines = [f"🔥 <b>{html.escape(p.name)}</b> · {K.tier_title(p.tier)} — {p.duration_days} дн. · <b>{fmt_usd(p.price)}</b>"
                 for p in plans]
        body = f"<blockquote>{chr(10).join(lines)}</blockquote>"
    else:
        body = "Тарифы пока не добавлены."
    text = f"💳 <b>Тарифы</b>\n\n{cur}\n\n{body}"
    if user.discount:
        text += f"\n\n🏷 Ваша скидка: <b>{fmt_usd(user.discount)}</b>"
    return text + ("\n\nВыберите тариф:" if plans else "")


@router.callback_query(F.data.in_({"sub:main", "buy"}))
async def buy_list(call: CallbackQuery, state: FSMContext, session, user, plan, plan_expires) -> None:
    await state.clear()
    await call.answer()
    await render(call, await plans_text(session, user, plan, plan_expires), K.plans_list(await billing.list_plans(session)), state)


@router.callback_query(F.data.regexp(r"^buy:\d+$"))
async def buy_plan(call: CallbackQuery, state: FSMContext, session, user) -> None:
    from bot.database.models import Plan
    p = await session.get(Plan, int(call.data.split(":")[1]))
    if p is None:
        return await call.answer("Тариф не найден.", show_alert=True)
    price = p.price - min(p.price, user.discount)
    price_line = f"💰 Цена: <b>{fmt_usd(price)}</b>"
    if price != p.price:
        price_line += f" <s>{fmt_usd(p.price)}</s>"
    methods = await billing.list_methods(session, enabled_only=True)
    footer = "Выберите способ оплаты:" if methods else "⚠️ Оплата временно недоступна — напишите в поддержку."
    text = (f"💳 <b>{html.escape(p.name)}</b> · {K.tier_title(p.tier)}\n\n"
            f"<blockquote>📅 Срок: <b>{p.duration_days} дн.</b>\n♾ Возможности: {perks(p.tier)}\n{price_line}"
            f"</blockquote>\n\n{footer}")
    await call.answer()
    await render(call, text, K.plan_methods(p.id, methods), state)


# ───────────────────────────── заявка ─────────────────────────────
def order_details(order: Order) -> str:
    return (f"<blockquote>🔥 Тариф: <b>{html.escape(order.plan_name)}</b>\n"
            f"💰 К оплате: <b>{html.escape(order.amount_text)}</b>"
            f"{f' ({fmt_usd(order.amount_usd)})' if '$' not in order.amount_text else ''}\n"
            f"👛 Реквизиты: <b>{html.escape(order.address)}</b></blockquote>")


@router.callback_query(F.data.regexp(r"^pay:\d+:\w+$"))
async def pay(call: CallbackQuery, state: FSMContext, session, user) -> None:
    _, plan_id, method_key = call.data.split(":")
    order = await billing.create_order(session, user.id, int(plan_id), method_key)
    if order is None:
        return await call.answer("Этот способ оплаты сейчас недоступен.", show_alert=True)
    method = await billing.get_method(session, method_key)
    warn = (f"⚠️ <b>Переведите ровно {html.escape(order.amount_text)}</b> — до копейки, "
            "по этой сумме мы найдём ваш платёж. Комиссию банка / сети оплачивайте сверху.")
    text = (f"🧾 <b>Заявка #{order.id}</b> · {html.escape(method.title)}\n\n{order_details(order)}\n\n{warn}\n\n"
            "После перевода нажмите <b>«Я оплатил»</b> — администратор проверит платёж и активирует тариф.")
    await call.answer()
    await render(call, text, K.order_pay(order.id), state)


@router.callback_query(F.data.regexp(r"^order:cancel:\d+$"))
async def order_cancel(call: CallbackQuery, state: FSMContext, session, user, plan, plan_expires) -> None:
    await billing.cancel_order(session, int(call.data.rsplit(":", 1)[1]), user.id)
    await buy_list(call, state, session, user, plan, plan_expires)


@router.callback_query(F.data.regexp(r"^order:paid:\d+$"))
async def order_paid(call: CallbackQuery, state: FSMContext, bot: Bot, session, user) -> None:
    if await billing.count_pending_orders(session, user.id) >= MAX_PENDING_PER_USER:
        return await call.answer("У вас уже есть заявки на проверке — дождитесь решения.", show_alert=True)
    order = await billing.mark_order_paid(session, int(call.data.rsplit(":", 1)[1]), user.id)
    if order is None:
        return await call.answer("Заявка уже отправлена или отменена.", show_alert=True)
    await call.answer()
    await render(call, f"⏳ <b>Заявка #{order.id} отправлена на проверку</b>\n\n{order_details(order)}\n\n"
                       "Как только администратор подтвердит оплату, тариф активируется и придёт уведомление.",
                 K.to_menu(), state)
    await notify_admins(bot, session, order, call.from_user)


# ───────────────────────────── промокод ─────────────────────────────
@router.callback_query(F.data == "promo")
async def promo_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(PromoFSM.code)
    await render(call, "🎁 <b>Промокод</b>\n\nОтправьте промокод сообщением:", K.promo_input_back(), state)


@router.message(PromoFSM.code, F.text)
async def promo_code(m: Message, state: FSMContext, session, user, plan, plan_expires) -> None:
    ok, text = await billing.activate_promocode(session, user.id, m.text)
    await state.clear()
    await session.commit()
    plan, plan_expires = await _fresh_plan(session, user.id)
    icon = "✅" if ok else "⚠️"
    await render(m, f"{icon} {html.escape(text)}\n\n" + await plans_text(session, user, plan, plan_expires),
                 K.plans_list(await billing.list_plans(session)), state)


async def _fresh_plan(session, uid: int):
    from bot.services.plans import effective_plan
    return await effective_plan(session, uid)


# ───────────────────────────── уведомления админам ─────────────────────────────
def admin_order_text(order: Order, tg_user=None, user_tg_id: int | None = None) -> str:
    if tg_user is not None:
        who = f'<a href="tg://user?id={tg_user.id}">{html.escape(tg_user.full_name)}</a>'
        if tg_user.username:
            who += f" · @{html.escape(tg_user.username)}"
        who += f" · <code>{tg_user.id}</code>"
    else:
        who = f'<a href="tg://user?id={user_tg_id}">пользователь</a> · <code>{user_tg_id}</code>'
    status = {OrderStatus.PENDING: "⏳ ждёт проверки", OrderStatus.CONFIRMED: "✅ подтверждена",
              OrderStatus.REJECTED: "❌ отклонена"}.get(order.status, order.status)
    paid = f"{order.paid_at:%d.%m %H:%M} UTC" if order.paid_at else "—"
    return (f"🧾 <b>Заявка #{order.id}</b> — {status}\n\n"
            f"👤 {who}\n"
            f"🔥 {html.escape(order.plan_name)} · {K.tier_title(order.tier)} · {order.days} дн.\n"
            f"💰 <b>{html.escape(order.amount_text)}</b> ({fmt_usd(order.amount_usd)}) · {html.escape(order.method)}\n"
            f"👛 {html.escape(order.address)}\n"
            f"📅 Оплачено: {paid}")


async def notify_admins(bot: Bot, session, order: Order, tg_user) -> None:
    text = admin_order_text(order, tg_user)
    sent: list[list[int]] = []
    for admin_id in billing.admin_ids():
        try:
            msg = await bot.send_message(admin_id, text, reply_markup=K.order_review(order.id))
            sent.append([admin_id, msg.message_id])
        except (TelegramBadRequest, TelegramForbiddenError):
            pass  # админ не запускал бота / заблокировал его
    if not sent:
        log.error("Заявка #%s: ни одному админу не удалось отправить уведомление", order.id)
    await billing.set_order_admin_msgs(session, order.id, sent)


async def finish_admin_msgs(bot: Bot, order: Order, admin_name: str) -> None:
    """Убирает кнопки из уведомлений у всех админов и пишет, кто принял решение."""
    text = admin_order_text(order, user_tg_id=order.user_id) + f"\n🛠 {html.escape(admin_name)}"
    for chat_id, msg_id in json.loads(order.admin_msgs or "[]"):
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
