from __future__ import annotations

import html

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


def esc(value: object) -> str:
    return html.escape(str(value))


_GREEN = ("✅", "🚀", "▶️", "➕", "🟢", "🎁", "👑", "⭐", "🔄")
_RED = ("⏹", "🗑", "✖️", "🚫", "⛔", "🔴")
_BLUE = ("🕒", "🔑", "📦", "🧦", "🎲", "🔁", "💎", "📢", "✍️", "🔍")
_PLAIN = ("• ", "➖", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "0️⃣")


def _style(text: str) -> str | None:
    """Цвет кнопки по смыслу: зелёный — действие, красный — отмена/удаление, синий — настройка, навигация без цвета."""
    if text.startswith(_PLAIN):
        return None
    for prefix, color in ((_RED, "danger"), (_GREEN, "success"), (_BLUE, "primary")):
        if text.startswith(prefix):
            return color
    return "primary"


def _btn(item: tuple) -> InlineKeyboardButton:
    """(текст, данные) — цвет по смыслу; (текст, данные, стиль) — явный: 'primary' | 'success' | 'danger' | ''."""
    text, data = item[0], item[1]
    style = (item[2] or None) if len(item) > 2 else _style(text)
    if data.startswith("http"):
        return InlineKeyboardButton(text=text, url=data, style=style)
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def ikb(rows: list[list[tuple]]) -> InlineKeyboardMarkup:
    """ikb([[("Текст", "callback")], [("Ссылка", "https://...")]])"""
    return InlineKeyboardMarkup(inline_keyboard=[[_btn(b) for b in row] for row in rows if row])


async def render(event: CallbackQuery | Message, text: str, kb: InlineKeyboardMarkup | None = None,
                 state: FSMContext | None = None) -> None:
    """Бесшовная смена экрана: callback -> edit_text; текстовый ввод -> правим «якорное» сообщение."""
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await event.message.answer(text, reply_markup=kb)
        if state:
            await state.update_data(anchor=event.message.message_id)
        return

    anchor = (await state.get_data()).get("anchor") if state else None
    try:
        await event.delete()  # убираем пользовательский ввод — чат остаётся чистым
    except TelegramBadRequest:
        pass
    if anchor:
        try:
            await event.bot.edit_message_text(text, chat_id=event.chat.id, message_id=anchor, reply_markup=kb)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
    sent = await event.answer(text, reply_markup=kb)
    if state:
        await state.update_data(anchor=sent.message_id)


PAGE_SIZE = 20  # строк на страницу: список остаётся далеко от лимита Telegram в ~100 кнопок и 4096 символов


def paginate(items: list, page: int, size: int = PAGE_SIZE) -> tuple[list, int, int]:
    """-> (элементы страницы, нормализованная страница, всего страниц)"""
    total = max(1, (len(items) + size - 1) // size)
    page = max(0, min(page, total - 1))
    return items[page * size:(page + 1) * size], page, total


def nav_row(page: int, total: int, prefix: str) -> list[tuple]:
    """◀️ 2/5 ▶️ ; callback = f'{prefix}:{номер страницы}'"""
    if total <= 1:
        return []
    row = []
    if page > 0:
        row.append(("◀️", f"{prefix}:{page - 1}", "primary"))
    row.append((f"{page + 1}/{total}", "noop", "primary"))
    if page < total - 1:
        row.append(("▶️", f"{prefix}:{page + 1}", "primary"))
    return row
