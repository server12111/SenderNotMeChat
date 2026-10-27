from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from bot.utils.ui import ikb

BACK_ADM = ("◀️ Админ-меню", "adm:main")


def admin_menu(pending: int = 0) -> InlineKeyboardMarkup:
    return ikb([
        [("🏷 Тарифы", "adm:plans", "primary"), ("🎁 Промокоды", "adm:promos", "primary")],
        [("👛 Реквизиты", "adm:methods", "primary"),
         (f"🧾 Заявки{f' ({pending})' if pending else ''}", "adm:orders", "success" if pending else "primary")],
        [("📊 Дашборд", "adm:dash"), ("👥 Пользователи", "adm:users")],
        [("📢 Сервисная рассылка", "adm:bc"), ("⚙️ Система", "adm:sys")],
        [("◀️ В меню", "menu:main")],
    ])


def user_card_kb(uid: int, banned: bool) -> InlineKeyboardMarkup:
    return ikb([
        [("⭐ PRO 30д", f"adm:g:{uid}:pro:30"), ("👑 VIP 30д", f"adm:g:{uid}:vip:30")],
        [("➕ +7 дней", f"adm:x:{uid}:7"), ("➕ +30 дней", f"adm:x:{uid}:30")],
        [("🚫 Аннулировать тариф", f"adm:rv:{uid}")],
        [("✅ Разблокировать" if banned else "⛔ Заблокировать", f"adm:ban:{uid}")],
        [("📱 Аккаунты", f"adm:acc:{uid}"), ("🚀 Задачи", f"adm:tasks:{uid}")],
        [("🔍 Другой поиск", "adm:users"), BACK_ADM],
    ])


def system_kb(maintenance: bool, g_delay: int, g_cap: int) -> InlineKeyboardMarkup:
    return ikb([
        [(f"🛠 Техобслуживание: {'🟢 ВКЛ' if maintenance else '🔴 выкл'}", "adm:sys:maint")],
        [(f"⏱ Мин. задержка: {g_delay}с" if g_delay else "⏱ Мин. задержка: выкл", "adm:sys:delay")],
        [(f"📉 Лимит/сутки на юзера: {g_cap}" if g_cap else "📉 Лимит/сутки на юзера: выкл", "adm:sys:cap")],
        [BACK_ADM],
    ])
