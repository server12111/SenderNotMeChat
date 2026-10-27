from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from bot.database.models import Order, PaymentMethod, Plan, Promocode, PromoType, fmt_usd
from bot.services.plans import PLANS
from bot.utils.ui import ikb

P, S, D = "primary", "success", "danger"


def _chunk(buttons: list, size: int = 2) -> list[list]:
    return [buttons[i:i + size] for i in range(0, len(buttons), size)]


def _method_icon(m: PaymentMethod) -> str:
    return "🏦" if m.key == "uah" else "🪙"


def tier_title(tier: str) -> str:
    p = PLANS.get(tier)
    return f"{p.emoji} {p.title}" if p else tier


# ───────────── пользователь ─────────────
def plans_list(plans: list[Plan]) -> InlineKeyboardMarkup:
    rows = [[(f"🔥 {p.name} · {fmt_usd(p.price)}", f"buy:{p.id}", P)] for p in plans]
    rows.append([("🎁 Промокод", "promo", P), ("⬅️ Назад", "menu:main", P)])
    return ikb(rows)


def plan_methods(plan_id: int, methods: list[PaymentMethod]) -> InlineKeyboardMarkup:
    buttons = [(f"{_method_icon(m)} {m.title}", f"pay:{plan_id}:{m.key}", P) for m in methods]
    return ikb(_chunk(buttons) + [[("⬅️ К тарифам", "sub:main", P)]])


def order_pay(order_id: int) -> InlineKeyboardMarkup:
    return ikb([[("✅ Я оплатил", f"order:paid:{order_id}", S), ("❌ Отмена", f"order:cancel:{order_id}", D)]])


def to_menu() -> InlineKeyboardMarkup:
    return ikb([[("🏠 В главное меню", "menu:main", P)]])


def promo_input_back() -> InlineKeyboardMarkup:
    return ikb([[("❌ Отмена", "sub:main", D)]])


# ───────────── админ ─────────────
def order_review(order_id: int) -> InlineKeyboardMarkup:
    return ikb([[("✅ Подтвердить", f"adm:order_ok:{order_id}", S), ("❌ Отклонить", f"adm:order_no:{order_id}", D)]])


def admin_back(cb: str = "adm:main") -> InlineKeyboardMarkup:
    return ikb([[("⬅️ Назад", cb, P)]])


def admin_cancel(cb: str = "adm:main") -> InlineKeyboardMarkup:
    return ikb([[("❌ Отмена", cb, D)]])


def confirm_delete(yes_cb: str, back_cb: str) -> InlineKeyboardMarkup:
    return ikb([[("🗑 Да, удалить", yes_cb, D), ("⬅️ Нет", back_cb, P)]])


def admin_plans(plans: list[Plan]) -> InlineKeyboardMarkup:
    dels = [(f"🗑 {p.name}", f"adm:plan_del:{p.id}", D) for p in plans]
    return ikb(_chunk(dels) + [[("➕ Добавить тариф", "adm:plan_add", S)], [("⬅️ Назад", "adm:main", P)]])


def plan_tiers() -> InlineKeyboardMarkup:
    return ikb([[(tier_title("pro"), "adm:ptier:pro", P), (tier_title("vip"), "adm:ptier:vip", P)],
                [("❌ Отмена", "adm:plans", D)]])


def admin_promos(promos: list[Promocode]) -> InlineKeyboardMarkup:
    dels = [(f"🗑 {p.code}", f"adm:promo_del:{p.id}", D) for p in promos]
    return ikb(_chunk(dels) + [[("➕ Создать промокод", "adm:promo_add", S)], [("⬅️ Назад", "adm:main", P)]])


def promo_types() -> InlineKeyboardMarkup:
    return ikb([[(f"📅 {PromoType.TITLES[PromoType.ADD_DAYS]}", f"adm:ptype:{PromoType.ADD_DAYS}", P),
                 (f"💰 {PromoType.TITLES[PromoType.DISCOUNT]}", f"adm:ptype:{PromoType.DISCOUNT}", P)],
                [("❌ Отмена", "adm:promos", D)]])


def promo_tiers() -> InlineKeyboardMarkup:
    return ikb([[(tier_title("pro"), "adm:prtier:pro", P), (tier_title("vip"), "adm:prtier:vip", P)],
                [("❌ Отмена", "adm:promos", D)]])


def admin_methods(methods: list[PaymentMethod]) -> InlineKeyboardMarkup:
    buttons = [(f"{_method_icon(m)} {m.title}{' ✓' if m.enabled else ''}", f"adm:method:{m.key}", S if m.enabled else P)
               for m in methods]
    return ikb(_chunk(buttons) + [[("⬅️ Назад", "adm:main", P)]])


def admin_method(m: PaymentMethod, auto_supported: bool = False, auto_on: bool = False) -> InlineKeyboardMarkup:
    toggle = (("🛑 Выключить", f"adm:mtoggle:{m.key}", D) if m.enabled else ("✅ Включить", f"adm:mtoggle:{m.key}", S))
    rows = [[("👛 Адрес / карта", f"adm:mset:{m.key}:address", P),
             ("💰 Свой курс" if auto_on else "💰 Курс к $", f"adm:mset:{m.key}:rate", P)]]
    if auto_supported:
        rows.append([("♻️ Авто-курс: вкл" if auto_on else "♻️ Включить авто-курс", f"adm:mauto:{m.key}", S if auto_on else P)])
    rows += [[toggle], [("⬅️ Назад", "adm:methods", P)]]
    return ikb(rows)


def admin_orders(orders: list[Order]) -> InlineKeyboardMarkup:
    buttons = [(f"🧾 #{o.id} · {fmt_usd(o.amount_usd)}", f"adm:order:{o.id}", P) for o in orders]
    return ikb(_chunk(buttons) + [[("⬅️ Назад", "adm:main", P)]])
