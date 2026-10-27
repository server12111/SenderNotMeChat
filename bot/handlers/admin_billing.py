"""Админка: тарифы, промокоды, реквизиты, заявки на оплату (экраны и логика из проекта invite)."""
from __future__ import annotations

import html
import json
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import config
from bot.database.models import OrderStatus, PromoType, fmt_usd
from bot.handlers import billing as user_billing
from bot.handlers.states import AddPlanFSM, AddPromoFSM, MethodFSM
from bot.keyboards import billing as K
from bot.services import billing, rates as rates_mod
from bot.utils.ui import render

router = Router(name="admin_billing")
# доступ строго по ADMIN_IDS
router.message.filter(F.from_user.id.in_(config.admin_ids))
router.callback_query.filter(F.from_user.id.in_(config.admin_ids))

_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")


def _h(icon: str, title: str, body: str = "") -> str:
    head = f"{icon} <b>{title}</b>"
    return f"{head}\n\n{body}" if body else head


async def _int(m: Message, state: FSMContext, screen: str, back_cb: str, minimum: int = 0) -> int | None:
    """Читает целое из сообщения; при ошибке правит экран и возвращает None."""
    try:
        value = int((m.text or "").strip())
        if not minimum <= value <= 10**9:
            raise ValueError
        return value
    except ValueError:
        await render(m, f"{screen}\n\n⚠️ Нужно целое число ≥ {minimum}. Попробуйте ещё раз:", K.admin_cancel(back_cb), state)
        return None


async def _money(m: Message, state: FSMContext, screen: str, back_cb: str) -> int | None:
    """Читает сумму в $ («10», «9.99», «9,99») и возвращает центы."""
    raw = (m.text or "").strip().lstrip("$").replace(",", ".")
    if re.fullmatch(r"\d{1,7}(\.\d{1,2})?", raw):
        cents = round(float(raw) * 100)
        if cents > 0:
            return cents
    await render(m, f"{screen}\n\n⚠️ Введите сумму в долларах, например <code>9.99</code>:", K.admin_cancel(back_cb), state)
    return None


async def _clear(state: FSMContext) -> None:
    """Сброс FSM с сохранением «якорного» сообщения меню."""
    anchor = (await state.get_data()).get("anchor")
    await state.clear()
    if anchor:
        await state.update_data(anchor=anchor)


# ═════════════════════════════ тарифы ═════════════════════════════
PLAN_HEAD = "🏷 <b>Новый тариф</b>"


def _plan_line(p) -> str:
    return f"🔥 <b>{html.escape(p.name)}</b> · {K.tier_title(p.tier)} — {fmt_usd(p.price)} · {p.duration_days} дн."


async def _plans_screen(event, state, session, prefix: str = "") -> None:
    await _clear(state)
    plans = await billing.list_plans(session)
    if plans:
        body = f"<blockquote>{chr(10).join(_plan_line(p) for p in plans)}</blockquote>\n\nНажмите на тариф, чтобы удалить его."
    else:
        body = "Тарифов пока нет — добавьте первый."
    text = _h("🏷", "Тарифы", body)
    await render(event, f"{prefix}\n\n{text}" if prefix else text, K.admin_plans(plans), state)


@router.callback_query(F.data == "adm:plans")
async def plans(call: CallbackQuery, state: FSMContext, session) -> None:
    await call.answer()
    await _plans_screen(call, state, session)


@router.callback_query(F.data.regexp(r"^adm:plan_del:\d+$"))
async def plan_del_ask(call: CallbackQuery, state: FSMContext, session) -> None:
    from bot.database.models import Plan
    plan = await session.get(Plan, int(call.data.rsplit(":", 1)[1]))
    await call.answer()
    if plan is None:
        return await _plans_screen(call, state, session)
    await render(call, _h("🗑", "Удалить тариф?", _plan_line(plan)), K.confirm_delete(f"adm:plan_delok:{plan.id}", "adm:plans"), state)


@router.callback_query(F.data.regexp(r"^adm:plan_delok:\d+$"))
async def plan_del(call: CallbackQuery, state: FSMContext, session) -> None:
    await billing.delete_plan(session, int(call.data.rsplit(":", 1)[1]))
    await call.answer()
    await _plans_screen(call, state, session, "🗑 Тариф удалён.")


@router.callback_query(F.data == "adm:plan_add")
async def plan_add(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AddPlanFSM.name)
    await render(call, f"{PLAN_HEAD} · 1/4\n\nВведите <b>название</b>:", K.admin_cancel("adm:plans"), state)


@router.message(AddPlanFSM.name, F.text)
async def plan_name(m: Message, state: FSMContext) -> None:
    await state.update_data(name=m.text.strip()[:64])
    await state.set_state(AddPlanFSM.price)
    await render(m, f"{PLAN_HEAD} · 2/4\n\nЦена в <b>$</b> (например <code>9.99</code>):", K.admin_cancel("adm:plans"), state)


@router.message(AddPlanFSM.price, F.text)
async def plan_price(m: Message, state: FSMContext) -> None:
    if (v := await _money(m, state, f"{PLAN_HEAD} · 2/4", "adm:plans")) is None:
        return
    await state.update_data(price=v)
    await state.set_state(AddPlanFSM.days)
    await render(m, f"{PLAN_HEAD} · 3/4\n\nСрок подписки в <b>днях</b>:", K.admin_cancel("adm:plans"), state)


@router.message(AddPlanFSM.days, F.text)
async def plan_days(m: Message, state: FSMContext) -> None:
    if (v := await _int(m, state, f"{PLAN_HEAD} · 3/4", "adm:plans", 1)) is None:
        return
    await state.update_data(days=v)
    await state.set_state(AddPlanFSM.tier)
    await render(m, f"{PLAN_HEAD} · 4/4\n\nКакие <b>возможности</b> даёт тариф?\n\n"
                    f"{K.tier_title('pro')}: {user_billing.perks('pro')}\n{K.tier_title('vip')}: {user_billing.perks('vip')}",
                 K.plan_tiers(), state)


@router.callback_query(AddPlanFSM.tier, F.data.regexp(r"^adm:ptier:(pro|vip)$"))
async def plan_tier(call: CallbackQuery, state: FSMContext, session) -> None:
    d = await state.get_data()
    if d.get("plan_saved"):
        return await call.answer()
    await state.update_data(plan_saved=True)
    await billing.add_plan(session, d["name"], d["price"], d["days"], call.data.rsplit(":", 1)[1])
    await call.answer()
    await _plans_screen(call, state, session, f"✅ Тариф «{html.escape(d['name'])}» добавлен.")


# ═════════════════════════════ промокоды ═════════════════════════════
PROMO_HEAD = "🎁 <b>Новый промокод</b>"


def _promo_value(p) -> str:
    return fmt_usd(p.value) if p.type == PromoType.DISCOUNT else str(p.value)


def _promos_text(promos) -> str:
    if not promos:
        return _h("🎁", "Промокоды", "Промокодов пока нет.")
    lines = [f"<code>{p.code}</code> — {PromoType.TITLES.get(p.type, p.type)}: <b>{_promo_value(p)}</b>"
             + (f" ({K.tier_title(p.tier)})" if p.type == PromoType.ADD_DAYS else "")
             + f" · {p.used_count}/{p.max_activations or '∞'}" for p in promos]
    return _h("🎁", "Промокоды", f"<blockquote>{chr(10).join(lines)}</blockquote>\n\nНажмите на код, чтобы удалить его.")


async def _promos_screen(event, state, session, prefix: str = "") -> None:
    await _clear(state)
    promos = await billing.list_promos(session)
    text = _promos_text(promos)
    await render(event, f"{prefix}\n\n{text}" if prefix else text, K.admin_promos(promos), state)


@router.callback_query(F.data == "adm:promos")
async def promos(call: CallbackQuery, state: FSMContext, session) -> None:
    await call.answer()
    await _promos_screen(call, state, session)


@router.callback_query(F.data.regexp(r"^adm:promo_del:\d+$"))
async def promo_del_ask(call: CallbackQuery, state: FSMContext) -> None:
    pid = int(call.data.rsplit(":", 1)[1])
    await call.answer()
    await render(call, _h("🗑", "Удалить промокод?", "Активации по нему тоже удалятся."),
                 K.confirm_delete(f"adm:promo_delok:{pid}", "adm:promos"), state)


@router.callback_query(F.data.regexp(r"^adm:promo_delok:\d+$"))
async def promo_del(call: CallbackQuery, state: FSMContext, session) -> None:
    await billing.delete_promo(session, int(call.data.rsplit(":", 1)[1]))
    await call.answer()
    await _promos_screen(call, state, session, "🗑 Промокод удалён.")


@router.callback_query(F.data == "adm:promo_add")
async def promo_add(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AddPromoFSM.code)
    await render(call, f"{PROMO_HEAD} · 1/4\n\nВведите <b>код</b> (латиница, цифры, 3–32 символа):", K.admin_cancel("adm:promos"), state)


@router.message(AddPromoFSM.code, F.text)
async def promo_code(m: Message, state: FSMContext, session) -> None:
    code = m.text.strip()
    error = None
    if not _CODE_RE.match(code):
        error = "Допустимы A–Z, 0–9, _ и -, длина 3–32."
    elif await billing.promo_exists(session, code):
        error = "Такой код уже существует."
    if error:
        return await render(m, f"{PROMO_HEAD} · 1/4\n\n⚠️ {error} Введите другой код:", K.admin_cancel("adm:promos"), state)
    await state.update_data(code=code.upper())
    await state.set_state(AddPromoFSM.type)
    await render(m, f"{PROMO_HEAD} · 2/4\n\nКод: <code>{code.upper()}</code>\nВыберите <b>тип бонуса</b>:", K.promo_types(), state)


@router.callback_query(AddPromoFSM.type, F.data.regexp(r"^adm:ptype:\w+$"))
async def promo_type(call: CallbackQuery, state: FSMContext) -> None:
    type_ = call.data.rsplit(":", 1)[1]
    if type_ not in PromoType.ALL:
        return await call.answer()
    await call.answer()
    await state.update_data(type=type_)
    await state.set_state(AddPromoFSM.value)
    hint = {PromoType.DISCOUNT: "сумму скидки в $ (например 2.50)", PromoType.ADD_DAYS: "количество дней"}[type_]
    await render(call, f"{PROMO_HEAD} · 3/4\n\nВведите <b>{hint}</b>:", K.admin_cancel("adm:promos"), state)


async def _promo_limit_screen(event, state) -> None:
    await state.set_state(AddPromoFSM.limit)
    await render(event, f"{PROMO_HEAD} · 4/4\n\n<b>Лимит активаций</b> (0 — без лимита):", K.admin_cancel("adm:promos"), state)


@router.message(AddPromoFSM.value, F.text)
async def promo_value(m: Message, state: FSMContext) -> None:
    is_discount = (await state.get_data()).get("type") == PromoType.DISCOUNT
    v = await (_money(m, state, f"{PROMO_HEAD} · 3/4", "adm:promos") if is_discount
               else _int(m, state, f"{PROMO_HEAD} · 3/4", "adm:promos", 1))
    if v is None:
        return
    await state.update_data(value=v)
    if is_discount:
        return await _promo_limit_screen(m, state)
    await state.set_state(AddPromoFSM.tier)  # дни нужно привязать к тарифу — на случай, если у пользователя его нет
    await render(m, f"{PROMO_HEAD} · 3/4\n\nКакой <b>тариф</b> выдавать, если у пользователя нет активного?\n"
                    f"(Есть активный — дни добавятся к нему.)", K.promo_tiers(), state)


@router.callback_query(AddPromoFSM.tier, F.data.regexp(r"^adm:prtier:(pro|vip)$"))
async def promo_tier(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.update_data(tier=call.data.rsplit(":", 1)[1])
    await _promo_limit_screen(call, state)


@router.message(AddPromoFSM.limit, F.text)
async def promo_limit(m: Message, state: FSMContext, session) -> None:
    if (v := await _int(m, state, f"{PROMO_HEAD} · 4/4", "adm:promos")) is None:
        return
    d = await state.get_data()
    await billing.create_promo(session, d["code"], d["type"], d["value"], d.get("tier", "pro"), v)
    await _promos_screen(m, state, session, f"✅ Промокод <code>{d['code']}</code> создан.")


# ═════════════════════════════ реквизиты ═════════════════════════════
_FIELD_PROMPTS = {
    "address": "Отправьте <b>адрес кошелька</b> или <b>номер карты</b> для этого способа:",
    "rate": "Сколько <b>{currency}</b> брать за <b>$1</b>? Например <code>41.5</code>.",
}


def _method_kb(method):
    return K.admin_method(method, method.key in rates_mod.AUTO_SOURCES, rates_mod.is_auto(method))


def _method_card(method, prefix: str = "") -> str:
    status = "✅ включён" if method.enabled else "🛑 выключен"
    rate = f"{method.rate:.8g} {method.currency} за $1" if method.rate > 0 else "сумма в $ (эквивалент)"
    if rates_mod.is_auto(method):
        when = f", {method.rate_updated_at:%d.%m %H:%M} UTC" if method.rate_updated_at else ", ещё не загружен"
        rate += f" · авто ({rates_mod.SOURCE_NAMES[method.key]}{when})"
    elif method.key in rates_mod.AUTO_SOURCES:
        rate += " · вручную"
    example = billing.format_amount(method, 1000) + " (минус 1–99 мельчайших единиц)"
    body = (f"<blockquote>Статус: <b>{status}</b>\n"
            f"👛 Адрес: <code>{html.escape(method.address or '—')}</code>\n"
            f"🏷 Опознание платежа: <b>уникальная сумма</b>\n"
            f"💰 Курс: <b>{rate}</b>\n"
            f"🔥 Тариф за $10 → <b>{html.escape(example)}</b></blockquote>")
    if not method.address:
        body += "\n\n⚠️ Укажите адрес — без него способ не показывается клиентам."
    if method.rate <= 0:
        body += f"\n\n⚠️ Укажите курс: платёж опознаётся по точной сумме в {method.currency}."
    text = _h("👛", method.title, body)
    return f"{prefix}\n\n{text}" if prefix else text


async def _methods_screen(event, state, session, prefix: str = "") -> None:
    await _clear(state)
    methods = await billing.list_methods(session)
    on = sum(1 for x in methods if x.enabled)
    text = _h("👛", "Реквизиты для оплаты",
              f"Включено способов: <b>{on}/{len(methods)}</b>\n\nНажмите на способ, чтобы указать адрес и курс.\n\n"
              "💰 Бот выставляет каждой заявке уникальную сумму — на несколько копеек / центов меньше (450.78, 450.77…). "
              "По точной сумме видно, от кого платёж.")
    await render(event, f"{prefix}\n\n{text}" if prefix else text, K.admin_methods(methods), state)


@router.callback_query(F.data == "adm:methods")
async def methods(call: CallbackQuery, state: FSMContext, session) -> None:
    await call.answer()
    await _methods_screen(call, state, session)


@router.callback_query(F.data.regexp(r"^adm:method:\w+$"))
async def method_card(call: CallbackQuery, state: FSMContext, session) -> None:
    method = await billing.get_method(session, call.data.rsplit(":", 1)[1])
    await call.answer()
    if method is None:
        return await _methods_screen(call, state, session)
    await _clear(state)
    await render(call, _method_card(method), _method_kb(method), state)


@router.callback_query(F.data.regexp(r"^adm:mtoggle:\w+$"))
async def method_toggle(call: CallbackQuery, state: FSMContext, session) -> None:
    method = await billing.get_method(session, call.data.rsplit(":", 1)[1])
    if method is None:
        await call.answer()
        return await _methods_screen(call, state, session)
    if not method.enabled and not method.address:
        return await call.answer("Сначала укажите адрес.", show_alert=True)
    if not method.enabled and method.rate <= 0:
        return await call.answer(f"Сначала укажите курс: сколько {method.currency} за $1. "
                                 "В этой сети платёж опознаётся по точной сумме.", show_alert=True)
    method = await billing.update_method(session, method.key, enabled=not method.enabled)
    await call.answer()
    await render(call, _method_card(method), _method_kb(method), state)


@router.callback_query(F.data.regexp(r"^adm:mauto:\w+$"))
async def method_auto(call: CallbackQuery, state: FSMContext, session) -> None:
    method = await billing.get_method(session, call.data.rsplit(":", 1)[1])
    if method is None or method.key not in rates_mod.AUTO_SOURCES:
        await call.answer()
        return await _methods_screen(call, state, session)
    if rates_mod.is_auto(method):
        return await call.answer("Авто-курс уже включён. Чтобы задать свой — нажмите «Свой курс».", show_alert=True)
    await billing.update_method(session, method.key, auto_rate=True)
    await session.commit()
    got = await rates_mod.update_rates(only=method.key)
    await session.refresh(method)  # курс записан отдельной сессией
    prefix = ("✅ Авто-курс включён." if got
              else "⚠️ Авто-курс включён, но источник сейчас недоступен — повторю через 15 минут.")
    await call.answer()
    await render(call, _method_card(method, prefix), _method_kb(method), state)


@router.callback_query(F.data.regexp(r"^adm:mset:\w+:(address|rate)$"))
async def method_set(call: CallbackQuery, state: FSMContext, session) -> None:
    _, _, key, field = call.data.split(":")
    method = await billing.get_method(session, key)
    await call.answer()
    if method is None:
        return await _methods_screen(call, state, session)
    await state.set_state(MethodFSM.value)
    await state.update_data(method_key=key, method_field=field)
    prompt = _FIELD_PROMPTS[field].format(currency=method.currency)
    if field == "rate" and method.currency == "BTC":
        prompt = ("Сколько <b>BTC</b> брать за <b>$1</b>? Например <code>0.0000105</code> "
                  "(при курсе $95 000 за 1 BTC). Обновляйте курс время от времени.")
    await render(call, _h("👛", method.title, prompt), K.admin_cancel(f"adm:method:{key}"), state)


@router.message(MethodFSM.value, F.text)
async def method_value(m: Message, state: FSMContext, session) -> None:
    d = await state.get_data()
    key, field = d.get("method_key"), d.get("method_field")
    method = await billing.get_method(session, key or "")
    if method is None or field not in _FIELD_PROMPTS:
        await _clear(state)
        return await render(m, _h("👛", "Реквизиты"), K.admin_back(), state)
    back = K.admin_cancel(f"adm:method:{key}")
    raw = m.text.strip()
    if field == "rate":
        try:
            value = float(raw.replace(",", "."))
            if not 0 <= value <= 10**7:
                raise ValueError
        except ValueError:
            return await render(m, _h("👛", method.title, "⚠️ Нужно число, например <code>41.5</code>"), back, state)
        if value == 0:
            return await render(m, _h("👛", method.title, f"⚠️ Нужен точный курс, иначе платёж не опознать по сумме. "
                                                         f"Сколько {method.currency} за $1?"), back, state)
    else:
        if len(raw) > 255:
            return await render(m, _h("👛", method.title, "⚠️ Слишком длинно (макс. 255 символов)"), back, state)
        value = raw
    extra = {"auto_rate": False} if field == "rate" else {}  # свой курс отключает автообновление
    method = await billing.update_method(session, key, **{field: value}, **extra)
    await _clear(state)
    await render(m, _method_card(method, "✅ Сохранено."), _method_kb(method), state)


# ═════════════════════════════ заявки на оплату ═════════════════════════════
async def _orders_screen(event, state, session, prefix: str = "") -> None:
    await _clear(state)
    orders = await billing.list_pending_orders(session)
    if orders:
        lines = [f"#{o.id} · <b>{html.escape(o.amount_text)}</b> · {html.escape(o.plan_name)} · {o.paid_at:%d.%m %H:%M}" for o in orders]
        body = f"<blockquote>{chr(10).join(lines)}</blockquote>\n\nНажмите на заявку, чтобы проверить её."
    else:
        body = "Новых заявок нет."
    text = _h("🧾", "Заявки на оплату", body)
    await render(event, f"{prefix}\n\n{text}" if prefix else text, K.admin_orders(orders), state)


@router.callback_query(F.data == "adm:orders")
async def orders(call: CallbackQuery, state: FSMContext, session) -> None:
    await call.answer()
    await _orders_screen(call, state, session)


@router.callback_query(F.data.regexp(r"^adm:order:\d+$"))
async def order_card(call: CallbackQuery, state: FSMContext, session) -> None:
    order = await billing.get_order(session, int(call.data.rsplit(":", 1)[1]))
    await call.answer()
    if order is None or order.status != OrderStatus.PENDING:
        return await _orders_screen(call, state, session, "⚠️ Заявка уже обработана.")
    markup = K.order_review(order.id)
    markup.inline_keyboard.append(K.admin_back("adm:orders").inline_keyboard[0])
    await render(call, user_billing.admin_order_text(order, user_tg_id=order.user_id), markup, state)


@router.callback_query(F.data.regexp(r"^adm:order_(ok|no):\d+$"))
async def order_decide(call: CallbackQuery, state: FSMContext, bot: Bot, session) -> None:
    confirm = call.data.startswith("adm:order_ok")
    res = await billing.decide_order(session, int(call.data.rsplit(":", 1)[1]), call.from_user.id, confirm)
    if res is None:
        await call.answer("Заявка уже обработана другим админом.", show_alert=True)
        return await _orders_screen(call, state, session)
    order, user = res
    await session.commit()
    admin_name = f"@{call.from_user.username}" if call.from_user.username else call.from_user.full_name
    await user_billing.finish_admin_msgs(bot, order, admin_name)

    if confirm:
        note = (f"✅ <b>Оплата подтверждена!</b>\n\nЗаявка #{order.id}: тариф «{html.escape(order.plan_name)}» "
                f"активирован — +{order.days} дн. подписки.")
    else:
        note = (f"❌ <b>Заявка #{order.id} отклонена.</b>\n\nПлатёж не найден. "
                "Если вы оплатили — напишите в поддержку и приложите чек.")
    try:
        await bot.send_message(user.id, note, reply_markup=K.to_menu())
    except (TelegramBadRequest, TelegramForbiddenError):
        pass

    # нажали в уведомлении — оно уже обновлено; нажали из списка заявок — возвращаемся к списку
    notified = {tuple(x) for x in json.loads(order.admin_msgs or "[]")}
    if (call.message.chat.id, call.message.message_id) in notified:
        await call.answer("Подтверждено" if confirm else "Отклонено")
    else:
        await call.answer()
        await _orders_screen(call, state, session, f"✅ Заявка #{order.id} {'подтверждена' if confirm else 'отклонена'}.")
