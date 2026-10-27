from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from bot.database.models import Account
from bot.utils.ui import ikb, nav_row, paginate

BACK_MAIN = [("◀️ Главное меню", "menu:main")]


def acc_emoji(a: Account) -> str:
    if not a.is_active or a.status == "dead":
        return "🔴"
    return "🟠" if a.status == "limited" else "🟢"


def main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [("📋 Мои рассылки", "ml:list:0"), ("👤 Аккаунты", "acc:list:0")],
        [("💳 Подписка", "sub:main"), ("🎁 Промокод", "promo")],
        [("⚙️ Настройки", "set:main")],
    ]
    if is_admin:
        rows.append([("🛡 Админ-панель", "adm:main")])
    return ikb(rows)


# ───────────── аккаунты ─────────────
def accounts_kb(accs: list[Account], page: int = 0) -> InlineKeyboardMarkup:
    items, page, total = paginate(accs, page)
    rows = [[(f"{acc_emoji(a)} {a.display_name}", f"acc:v:{a.id}", "primary")] for a in items]
    rows.append(nav_row(page, total, "acc:list"))
    rows.append([("➕ Добавить аккаунт", "acc:add"), ("◀️ Главное меню", "menu:main")])
    return ikb(rows)


def account_card_kb(a: Account) -> InlineKeyboardMarkup:
    sp = "🟢 Автоподписка: ВКЛ" if a.auto_subscribe else "🔴 Автоподписка: ВЫКЛ"
    return ikb([
        [("✉️ Рассылки аккаунта", f"acc:ml:{a.id}", "primary")],
        [("▶️ Запустить все", f"acc:startall:{a.id}"), ("⛔ Остановить все", f"acc:stopall:{a.id}")],
        [(sp, f"acc:sp:{a.id}", "primary"), ("🌐 Прокси", f"acc:px:{a.id}", "primary")],
        [("🔄 Проверить", f"acc:chk:{a.id}", "primary"), ("✏️ Переименовать", f"acc:rn:{a.id}", "primary")],
        [("❌ Удалить", f"acc:del:{a.id}")],
        [("◀️ Назад", "acc:list:0")],
    ])


def confirm_kb(yes_cb: str, back_cb: str, yes_text: str = "✅ Да, удалить") -> InlineKeyboardMarkup:
    return ikb([[(yes_text, yes_cb, "danger"), ("◀️ Назад", back_cb)]])


def cancel_kb(cb: str = "cancel") -> InlineKeyboardMarkup:
    return ikb([[("✖️ Отмена", cb)]])


def add_proxy_kb() -> InlineKeyboardMarkup:
    return ikb([[("🌐 Указать прокси", "acc:add:px", "primary"), ("⏭ Пропустить", "acc:add:skippx", "primary")],
                [("✖️ Отмена", "cancel")]])


def add_api_kb(can_skip: bool) -> InlineKeyboardMarkup:
    row = [("🔑 Свой API ID/Hash", "acc:add:api", "primary")]
    if can_skip:
        row.append(("⏭ Пропустить", "acc:add:skipapi", "primary"))
    return ikb([row, [("✖️ Отмена", "cancel")]])


def numpad_kb() -> InlineKeyboardMarkup:
    """Код входа вводится кнопками: код, отправленный сообщением в чат, Telegram считает утёкшим и аннулирует."""
    def d(n: int):
        return (f"{n}️⃣", f"code:d:{n}")
    return ikb([
        [d(1), d(2), d(3)], [d(4), d(5), d(6)], [d(7), d(8), d(9)],
        [("🗑", "code:clear", "danger"), d(0), ("⬅️", "code:back", "primary")],
        [("✖️ Отмена", "cancel"), ("✅ Подтвердить", "code:ok", "success")],
    ])


# ───────────── подписка / настройки ─────────────
def settings_kb(tz: int, notify: bool) -> InlineKeyboardMarkup:
    return ikb([
        [("➖", "set:tz:-1"), (f"🕒 Часовой пояс: UTC{tz:+d}" if tz else "🕒 Часовой пояс: UTC", "set:noop"), ("➕", "set:tz:1")],
        [(f"🔔 Уведомления: {'вкл' if notify else 'выкл'}", "set:notify")],
        BACK_MAIN,
    ])
