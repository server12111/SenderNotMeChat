"""Тарифы, заявки на оплату, промокоды, способы оплаты — логика из проекта invite.

Оплата по реквизитам: пользователь переводит точную сумму, нажимает «Я оплатил», админ подтверждает вручную.
Каждой заявке выдаётся уникальная сумма (на 1–99 мельчайших единиц меньше), по ней видно, от кого платёж.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.database.models import (
    Order, OrderStatus, PaymentMethod, Plan, Promocode, PromocodeActivation, PromoType, User, fmt_usd, utcnow,
)
from bot.services.plans import PLANS, effective_plan, grant


# ───────────────────────────── тарифы ─────────────────────────────
async def list_plans(s: AsyncSession) -> list[Plan]:
    return list((await s.execute(select(Plan).order_by(Plan.price))).scalars())


async def add_plan(s: AsyncSession, name: str, price: int, days: int, tier: str) -> Plan:
    plan = Plan(name=name, price=price, duration_days=days, tier=tier)
    s.add(plan)
    await s.flush()
    return plan


async def delete_plan(s: AsyncSession, plan_id: int) -> None:
    await s.execute(delete(Plan).where(Plan.id == plan_id))


# ───────────────────────────── промокоды ─────────────────────────────
async def list_promos(s: AsyncSession, limit: int = 20) -> list[Promocode]:
    return list((await s.execute(select(Promocode).order_by(Promocode.id.desc()).limit(limit))).scalars())


async def promo_exists(s: AsyncSession, code: str) -> bool:
    return (await s.execute(select(Promocode.id).where(Promocode.code == code.upper()))).first() is not None


async def create_promo(s: AsyncSession, code: str, type_: str, value: int, tier: str, max_activations: int) -> Promocode:
    promo = Promocode(code=code.upper(), type=type_, value=value, tier=tier, max_activations=max_activations)
    s.add(promo)
    await s.flush()
    return promo


async def delete_promo(s: AsyncSession, promo_id: int) -> None:
    await s.execute(delete(PromocodeActivation).where(PromocodeActivation.promo_id == promo_id))
    await s.execute(delete(Promocode).where(Promocode.id == promo_id))


async def activate_promocode(s: AsyncSession, user_id: int, code: str) -> tuple[bool, str]:
    promo = (await s.execute(select(Promocode).where(Promocode.code == code.strip().upper()))).scalar_one_or_none()
    if promo is None:
        return False, "Промокод не найден."
    if promo.type not in PromoType.ALL:
        return False, "Этот промокод больше не действует."
    if await s.get(PromocodeActivation, (user_id, promo.id)):
        return False, "Вы уже активировали этот промокод."
    # атомарно занимаем слот активации
    res = await s.execute(
        update(Promocode)
        .where(Promocode.id == promo.id,
               or_(Promocode.max_activations <= 0, Promocode.used_count < Promocode.max_activations))
        .values(used_count=Promocode.used_count + 1))
    if res.rowcount == 0:
        await s.rollback()
        return False, "Лимит активаций промокода исчерпан."

    user = await s.get(User, user_id)
    if promo.type == PromoType.ADD_DAYS:
        plan, _ = await effective_plan(s, user_id)
        tier = plan.key if plan.key in PLANS else promo.tier  # есть активный тариф — продлеваем его
        await grant(s, user_id, tier, promo.value)
        text = f"Начислено {promo.value} дн. подписки {PLANS[tier].title}."
    else:
        user.discount += promo.value
        text = f"Скидка {fmt_usd(promo.value)} будет применена к следующей покупке."
    s.add(PromocodeActivation(user_id=user_id, promo_id=promo.id))
    try:
        await s.flush()
    except IntegrityError:  # тот же код прислали дважды параллельно
        await s.rollback()
        return False, "Вы уже активировали этот промокод."
    return True, text


# ───────────────────────────── способы оплаты ─────────────────────────────
def format_amount(method: PaymentMethod, cents: int) -> str:
    if method.rate > 0:
        return _fmt(cents / 100 * method.rate, method)
    return f"{fmt_usd(cents)} в {method.currency}"


async def list_methods(s: AsyncSession, enabled_only: bool = False) -> list[PaymentMethod]:
    q = select(PaymentMethod).order_by(PaymentMethod.sort)
    if enabled_only:
        q = q.where(PaymentMethod.enabled.is_(True), PaymentMethod.address.is_not(None))
    return list((await s.execute(q)).scalars())


async def get_method(s: AsyncSession, key: str) -> PaymentMethod | None:
    return await s.get(PaymentMethod, key)


async def update_method(s: AsyncSession, key: str, **values) -> PaymentMethod | None:
    method = await s.get(PaymentMethod, key)
    if method is None:
        return None
    for k, v in values.items():
        setattr(method, k, v)
    if not method.address:
        method.enabled = False
    await s.flush()
    return method


# ───────────────────────────── заявки ─────────────────────────────
_order_lock = asyncio.Lock()  # выбор уникальной суммы и вставка — без гонок между пользователями


def _decimals(method: PaymentMethod) -> int:
    return 8 if method.currency == "BTC" else 2


def _fmt(value: float, method: PaymentMethod) -> str:
    d = _decimals(method)
    text = f"{value:.{d}f}"
    if d > 2:
        text = text.rstrip("0").rstrip(".")
    return f"{text} {method.currency}"


async def _unique_amount_text(s: AsyncSession, method: PaymentMethod, cents: int) -> str:
    """Сумма в валюте оплаты, сдвинутая на 1–99 мельчайших единиц (копеек / центов / сатоши),
    чтобы не совпасть ни с одной открытой заявкой: по точной сумме видно, от кого платёж."""
    taken = set((await s.execute(
        select(Order.amount_text).where(
            Order.method == method.key,
            or_(Order.status == OrderStatus.PENDING,
                # брошенные заявки (реквизиты показали, «Я оплатил» не нажали) держат сумму сутки
                (Order.status == OrderStatus.NEW) & (Order.created_at > utcnow() - timedelta(days=1)))))).scalars())
    d = _decimals(method)
    unit = 10 ** d
    base = round(cents / 100 * method.rate * unit)  # в мельчайших единицах
    sign = -1 if base > 100 else 1  # обычно чуть дешевле; у копеечных сумм — чуть дороже
    for off in range(1, 100):  # ближайшая свободная: 450.78, 450.77, …
        text = _fmt((base + sign * off) / unit, method)
        if text not in taken:
            return text
    return _fmt((base + sign * 99) / unit, method)


async def create_order(s: AsyncSession, user_id: int, plan_id: int, method_key: str) -> Order | None:
    """Создаёт заявку с зафиксированной ценой (со скидкой) и реквизитами."""
    async with _order_lock:
        # прошлые неоплаченные заявки пользователя больше не нужны — освобождаем их суммы
        await s.execute(update(Order).where(Order.user_id == user_id, Order.status == OrderStatus.NEW)
                        .values(status=OrderStatus.CANCELLED))
        user = await s.get(User, user_id)
        plan = await s.get(Plan, plan_id)
        method = await s.get(PaymentMethod, method_key)
        if not (user and plan and method and method.enabled and method.address):
            return None
        if method.rate <= 0:
            return None  # без курса точную сумму не посчитать
        discount = min(plan.price, user.discount)
        amount = plan.price - discount
        order = Order(user_id=user_id, plan_id=plan.id, plan_name=plan.name, tier=plan.tier, days=plan.duration_days,
                      discount=discount, amount_usd=amount, method=method.key,
                      amount_text=await _unique_amount_text(s, method, amount), address=method.address)
        s.add(order)
        await s.commit()  # сумма должна быть видна следующему заказу сразу
        return order


async def get_order(s: AsyncSession, order_id: int) -> Order | None:
    return await s.get(Order, order_id)


async def cancel_order(s: AsyncSession, order_id: int, user_id: int) -> bool:
    res = await s.execute(update(Order).where(Order.id == order_id, Order.user_id == user_id, Order.status == OrderStatus.NEW)
                          .values(status=OrderStatus.CANCELLED))
    return res.rowcount > 0


async def mark_order_paid(s: AsyncSession, order_id: int, user_id: int) -> Order | None:
    """new → pending. None — заявка не найдена или уже отправлена."""
    res = await s.execute(update(Order).where(Order.id == order_id, Order.user_id == user_id, Order.status == OrderStatus.NEW)
                          .values(status=OrderStatus.PENDING, paid_at=utcnow()))
    if res.rowcount == 0:
        return None
    await s.commit()
    return await s.get(Order, order_id)


async def set_order_admin_msgs(s: AsyncSession, order_id: int, msgs: list[list[int]]) -> None:
    import json
    await s.execute(update(Order).where(Order.id == order_id).values(admin_msgs=json.dumps(msgs)))


async def decide_order(s: AsyncSession, order_id: int, admin_tg_id: int, confirm: bool) -> tuple[Order, User] | None:
    """pending → confirmed/rejected; при подтверждении начисляет тариф. None — заявку уже обработал кто-то другой."""
    res = await s.execute(
        update(Order).where(Order.id == order_id, Order.status == OrderStatus.PENDING)
        .values(status=OrderStatus.CONFIRMED if confirm else OrderStatus.REJECTED, decided_by=admin_tg_id, decided_at=utcnow()))
    if res.rowcount == 0:
        await s.rollback()
        return None
    order = await s.get(Order, order_id)
    user = await s.get(User, order.user_id)
    if confirm:
        await grant(s, user.id, order.tier if order.tier in PLANS else "pro", order.days)
        user.discount = max(0, user.discount - order.discount)
    await s.flush()
    return order, user


async def count_pending_orders(s: AsyncSession, user_id: int) -> int:
    return (await s.execute(select(func.count()).select_from(Order)
                            .where(Order.user_id == user_id, Order.status == OrderStatus.PENDING))).scalar_one()


async def count_all_pending(s: AsyncSession) -> int:
    return (await s.execute(select(func.count()).select_from(Order).where(Order.status == OrderStatus.PENDING))).scalar_one()


async def list_pending_orders(s: AsyncSession, limit: int = 20) -> list[Order]:
    return list((await s.execute(select(Order).where(Order.status == OrderStatus.PENDING)
                                 .order_by(Order.paid_at).limit(limit))).scalars())


def admin_ids() -> set[int]:
    return set(config.admin_ids)
