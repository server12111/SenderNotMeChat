from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from bot.database.models import Account, Mailing, MailingMessage, MailingTarget
from bot.utils.ui import ikb, nav_row, paginate

BACK = ("◀️ Назад", "ml:list:0")


def mailings_kb(items: list[Mailing], page: int = 0, nav_prefix: str = "ml:list", back: tuple = ("◀️ Главное меню", "menu:main"),
                can_create: bool = True) -> InlineKeyboardMarkup:
    chunk, page, total = paginate(items, page)
    rows = [[(f"{'🟢' if m.is_active else '🔴'} {m.name[:40]}", f"ml:v:{m.id}", "primary")] for m in chunk]
    rows.append(nav_row(page, total, nav_prefix))
    last = ([("➕ Создать рассылку", "ml:new")] if can_create else []) + [back]
    rows.append(last)
    return ikb(rows)


def mailing_card_kb(m: Mailing) -> InlineKeyboardMarkup:
    toggle = ("🔴 Остановить", f"ml:tg:{m.id}") if m.is_active else ("🟢 Запустить", f"ml:tg:{m.id}")
    return ikb([
        [toggle],
        [("📝 Сообщения", f"ml:msgs:{m.id}:0", "primary"), ("🎯 Целевые чаты", f"ml:tgts:{m.id}:0", "primary")],
        [("⏰ Время активности", f"ml:hrs:{m.id}", "primary"), ("🔃 Аккаунты", f"ml:acc:{m.id}", "primary")],
        [(f"🎲 Спинтакс: {'ВКЛ' if m.spintax else 'ВЫКЛ'}", f"ml:spin:{m.id}", "primary"), ("🔄 Обновить", f"ml:v:{m.id}", "primary")],
        [("❌ Удалить рассылку", f"ml:del:{m.id}"), ("◀️ Назад", "ml:list:0")],
    ])


def _preview(msg: MailingMessage) -> str:
    import html
    import re
    text = html.unescape(re.sub(r"<[^>]+>", "", msg.text or "")).replace("\n", " ").strip()
    text = text[:28] + "…" if len(text) > 28 else text
    prefix = "[Медиа] " if msg.media_path else ""
    return f"{prefix}{text}" if (prefix or text) else "[Медиа]"


def messages_kb(mid: int, msgs: list[MailingMessage], page: int, creating: bool) -> InlineKeyboardMarkup:
    chunk, page, total = paginate(msgs, page)
    rows = [[(f"🗑 {_preview(x)}", f"ml:mdel:{x.id}")] for x in chunk]
    rows.append(nav_row(page, total, f"ml:msgs:{mid}"))
    rows.append([("➕ Текст / фото / видео", f"ml:madd:{mid}", "primary")])
    if len(msgs) > 3:
        rows.append([("🗑 Удалить все тексты", f"ml:mdelall:{mid}")])
    if creating and msgs:
        rows.append([("✅ Готово", f"ml:cmdone:{mid}")])
    rows.append([("◀️ Назад", f"ml:v:{mid}")])
    return ikb(rows)


def targets_kb(mid: int, targets: list[MailingTarget], page: int, creating: bool) -> InlineKeyboardMarkup:
    chunk, page, total = paginate(targets, page)
    rows = []
    if not creating:
        rows.append([("КД между циклами", f"ml:iv:{mid}", "success"), ("КД между чатами", f"ml:cd:{mid}", "success")])
        rows.append([("📤 Выгрузить чаты", f"ml:export:{mid}", "primary")])
    for t in chunk:
        rows.append([(f"🗑 {t.target}{' ⚠️' if t.last_error else ''}", f"ml:tdel:{t.id}")])
    rows.append(nav_row(page, total, f"ml:tgts:{mid}"))
    rows.append([("➕ Добавить чат", f"ml:tadd:{mid}", "primary"), ("📁 Добавить папку", f"ml:tfold:{mid}", "primary")])
    rows.append([("📄 Загрузить .txt", f"ml:ttxt:{mid}", "primary")])
    last = []
    if creating and targets:
        last.append(("✅ Готово", f"ml:ctdone:{mid}"))
    last.append(("◀️ Назад", f"ml:v:{mid}"))
    rows.append(last)
    return ikb(rows)


def accounts_pick_kb(accs: list[Account], chosen: set[int], toggle_prefix: str, done: tuple | None, back: tuple | None) -> InlineKeyboardMarkup:
    rows = [[(f"{'✅' if a.id in chosen else '⬜️'} {a.display_name}", f"{toggle_prefix}:{a.id}", "primary")] for a in accs]
    last = [x for x in (back, done) if x]
    if last:
        rows.append(last)
    return ikb(rows)


def hours_kb(mid: int) -> InlineKeyboardMarkup:
    return ikb([[("⏭ Пропустить (24/7)", f"ml:hskip:{mid}", "primary"), ("⏰ Настроить", f"ml:hset:{mid}", "primary")],
                [("◀️ Назад", f"ml:v:{mid}")]])


def launch_kb(mid: int) -> InlineKeyboardMarkup:
    return ikb([[("🚀 Запустить рассылку", f"ml:launch:{mid}"), ("◀️ Назад", f"ml:v:{mid}")]])
