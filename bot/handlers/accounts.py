"""Управление аккаунтами: список -> карточка -> действия, пошаговое добавление."""
from __future__ import annotations

import logging
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from telethon.errors import (
    ApiIdInvalidError, FloodWaitError, PasswordHashInvalidError, PhoneCodeExpiredError, PhoneCodeInvalidError,
    PhoneNumberBannedError, PhoneNumberInvalidError, SessionPasswordNeededError,
)

from bot.config import config
from bot.database.models import Account, Mailing, utcnow
from bot.handlers.states import AccProxy, AddAccount, RenameAccount
from bot.keyboards.menus import (
    BACK_MAIN, account_card_kb, accounts_kb, acc_emoji, add_api_kb, add_proxy_kb, cancel_kb, confirm_kb, numpad_kb,
)
from bot.services.accounts import (
    check_proxy, device_seed, make_client, parse_proxy, session_from_file, session_from_tdata, validate_session,
)
from bot.services.clients import ClientManager
from bot.services.plans import lim
from bot.services.sender import SenderService
from bot.services.usage import sent_today_account
from bot.utils.crypto import encrypt
from bot.utils.text import SEP
from bot.utils.ui import esc, ikb, paginate, render

log = logging.getLogger(__name__)
router = Router(name="accounts")

CODE_LEN = 5
_pending: dict[int, dict] = {}  # user_id -> {"client", "phone", "hash"} — клиент, ждущий код/2FA


async def drop_pending(uid: int) -> None:
    p = _pending.pop(uid, None)
    if p:
        try:
            await p["client"].disconnect()
        except Exception:
            pass


async def _owned(session, aid: int, uid: int) -> Account | None:
    a = await session.get(Account, aid)
    return a if a and a.user_id == uid else None


async def _mailings_of(session, uid: int, aid: int | None = None) -> list[Mailing]:
    # populate_existing: статус рассылок меняется из фоновых задач — читаем свежие значения, а не кэш сессии
    q = select(Mailing).where(Mailing.user_id == uid).order_by(Mailing.id).execution_options(populate_existing=True)
    ms = (await session.execute(q)).scalars().all()
    return [m for m in ms if aid is None or aid in (m.account_ids or [])]


# ───────────── список ─────────────
async def show_list(event, state, session, user, page: int = 0):
    accs = (await session.execute(select(Account).where(Account.user_id == user.id).order_by(Account.id))).scalars().all()
    chunk, page, total = paginate(accs, page)
    ms = await _mailings_of(session, user.id)
    text = f"👤 <b>Ваши аккаунты</b>\n{SEP}\n"
    if chunk:
        for a in chunk:
            n = sum(1 for m in ms if a.id in (m.account_ids or []))
            text += f"{acc_emoji(a)} {esc(a.display_name)}\n  └ {esc(a.phone)} · рассылок: {n}\n"
    else:
        text += "У вас пока нет добавленных аккаунтов.\n"
    if total > 1:
        text += f"\nСтраница {page + 1}/{total}."
    text += "\nВыберите аккаунт или добавьте новый:"
    await render(event, text, accounts_kb(accs, page), state)


@router.callback_query(F.data.startswith("acc:list:"))
async def cb_list(call: CallbackQuery, state: FSMContext, session, user):
    await state.clear()
    await drop_pending(user.id)
    await call.answer()
    await show_list(call, state, session, user, int(call.data.split(":")[2]))


# ───────────── карточка ─────────────
def _proxy_line(a: Account) -> str:
    if not a.proxy:
        return "🌐 Прокси: не задан"
    p = parse_proxy(a.proxy)
    return f"🌐 Прокси: <code>{esc(p['host'])}:{p['port']}</code>" if p else "🌐 Прокси: некорректный"


async def show_card(event, state, session, user, a: Account, prefix: str = ""):
    ms = await _mailings_of(session, user.id, a.id)
    status = {"ok": "🟢 Работает", "limited": "🟠 Ограничен Telegram", "dead": "🔴 Отключён"}.get(a.status, a.status)
    if not a.is_active:
        status = "⚪ Выключен вручную"
    text = (f"{prefix}📱 <b>Аккаунт: {esc(a.display_name)}</b>\n{SEP}\n"
            f"📞 Номер: {esc(a.phone)}\n"
            f"{status}\n"
            f"🤖 Автоподписка на спонсоров: {'✅ Включена' if a.auto_subscribe else '❌ Выключена'}\n"
            f"{_proxy_line(a)}\n"
            f"✉️ Рассылок: {len(ms)} (активных: {sum(1 for m in ms if m.is_active)})\n"
            f"📤 Сегодня отправлено: {await sent_today_account(session, a.id)}\n"
            f"📅 Добавлен: {a.created_at:%d.%m.%Y}")
    if a.cooldown_until and a.cooldown_until > utcnow():
        text += f"\n⏳ Отдых до {a.cooldown_until:%d.%m %H:%M} UTC"
    await render(event, text + "\n\nВыберите действие:", account_card_kb(a), state)


@router.callback_query(F.data.startswith("acc:v:"))
async def cb_view(call: CallbackQuery, state: FSMContext, session, user):
    await state.clear()
    a = await _owned(session, int(call.data.split(":")[2]), user.id)
    if not a:
        return await call.answer("Аккаунт не найден", show_alert=True)
    await call.answer()
    await show_card(call, state, session, user, a)


@router.callback_query(F.data.startswith("acc:ml:"))
async def cb_account_mailings(call: CallbackQuery, state: FSMContext, session, user):
    from bot.handlers.mailings import show_list as show_mailings
    parts = call.data.split(":")
    a = await _owned(session, int(parts[2]), user.id)
    if not a:
        return await call.answer("Аккаунт не найден", show_alert=True)
    await call.answer()
    await show_mailings(call, state, session, user, int(parts[3]) if len(parts) > 3 else 0, account=a)


@router.callback_query(F.data.startswith("acc:startall:") | F.data.startswith("acc:stopall:"))
async def cb_start_stop_all(call: CallbackQuery, state: FSMContext, session, user, plan, sender: SenderService):
    start = call.data.startswith("acc:startall:")
    a = await _owned(session, int(call.data.split(":")[2]), user.id)
    if not a:
        return await call.answer("Аккаунт не найден", show_alert=True)
    ms = await _mailings_of(session, user.id, a.id)
    await session.commit()
    if start:
        if plan.key == "none":
            return await call.answer("💎 Нужна активная подписка PRO или VIP", show_alert=True)
        active = sum(1 for m in await _mailings_of(session, user.id) if m.is_active)
        todo = [m for m in ms if not m.is_active]
        room = max(0, plan.max_campaigns - active)
        run = todo[:room]
        for m in run:
            await sender.activate(m.id)
        skipped = len(todo) - len(run)
        text = f"▶️ Запущено рассылок: {len(run)}" + (f"\n⚠️ Не запущено из-за лимита тарифа: {skipped}" if skipped else "")
    else:
        todo = [m for m in ms if m.is_active]
        for m in todo:
            await sender.deactivate(m.id)
        text = f"⛔ Остановлено рассылок: {len(todo)}"
    await call.answer(text, show_alert=True)
    await show_card(call, state, session, user, a)


@router.callback_query(F.data.startswith("acc:sp:"))
async def cb_toggle_sponsor(call: CallbackQuery, state: FSMContext, session, user, clients: ClientManager):
    a = await _owned(session, int(call.data.split(":")[2]), user.id)
    if not a:
        return await call.answer("Аккаунт не найден", show_alert=True)
    a.auto_subscribe = not a.auto_subscribe
    clients.set_auto_subscribe(a.id, a.auto_subscribe)
    await call.answer("🟢 Автоподписка включена" if a.auto_subscribe else "🔴 Автоподписка выключена")
    await show_card(call, state, session, user, a)


@router.callback_query(F.data.startswith("acc:chk:"))
async def cb_check(call: CallbackQuery, state: FSMContext, session, user, clients: ClientManager):
    a = await _owned(session, int(call.data.split(":")[2]), user.id)
    if not a:
        return await call.answer("Аккаунт не найден", show_alert=True)
    await call.answer("⏳ Проверяю…")
    try:
        await clients.check(a)
        if a.status == "dead":
            a.status = "ok"
        msg, alert = "🟢 Сессия активна", False
    except Exception as e:
        if getattr(e, "permanent", False):
            a.status = "dead"
        msg, alert = f"🔴 {e}" if str(e) else f"🔴 {type(e).__name__}", True
    await call.answer(msg, show_alert=alert)
    await show_card(call, state, session, user, a)


# ───────────── переименование ─────────────
@router.callback_query(F.data.startswith("acc:rn:"))
async def cb_rename(call: CallbackQuery, state: FSMContext, session, user):
    a = await _owned(session, int(call.data.split(":")[2]), user.id)
    if not a:
        return await call.answer("Аккаунт не найден", show_alert=True)
    await call.answer()
    await state.set_state(RenameAccount.title)
    await state.update_data(acc_id=a.id)
    await render(call, f"✏️ <b>Переименование</b>\n{SEP}\nТекущее имя: <b>{esc(a.display_name)}</b>\n\n"
                       f"Введите новое имя (до 40 символов) или <code>-</code>, чтобы сбросить.",
                 cancel_kb(f"acc:v:{a.id}"), state)


@router.message(RenameAccount.title, F.text)
async def on_rename(m: Message, state: FSMContext, session, user):
    a = await _owned(session, (await state.get_data()).get("acc_id", 0), user.id)
    if not a:
        await state.clear()
        return await render(m, "Аккаунт не найден.", ikb([BACK_MAIN]), state)
    a.title = None if m.text.strip() == "-" else m.text.strip()[:40]
    await state.clear()
    await show_card(m, state, session, user, a, prefix="✅ Имя сохранено\n\n")


# ───────────── прокси (VIP) ─────────────
@router.callback_query(F.data.startswith("acc:px:"))
async def cb_proxy(call: CallbackQuery, state: FSMContext, session, user, plan):
    a = await _owned(session, int(call.data.split(":")[2]), user.id)
    if not a:
        return await call.answer("Аккаунт не найден", show_alert=True)
    if not plan.proxy:
        return await call.answer("🔒 Индивидуальные SOCKS5-прокси доступны на тарифе VIP", show_alert=True)
    await call.answer()
    await state.set_state(AccProxy.value)
    await state.update_data(acc_id=a.id)
    await render(call, f"🌐 <b>Прокси для {esc(a.display_name)}</b>\n{SEP}\n"
                       f"Введите прокси в формате:\n<code>socks5://host:port</code>\nили с авторизацией:\n"
                       f"<code>socks5://user:pass@host:port</code>\n\n<code>-</code> — убрать прокси.\n"
                       f"<i>Перед сохранением проверю соединение. Применится при следующем подключении аккаунта.</i>",
                 cancel_kb(f"acc:v:{a.id}"), state)


@router.message(AccProxy.value, F.text)
async def on_proxy(m: Message, state: FSMContext, session, user, plan):
    a = await _owned(session, (await state.get_data()).get("acc_id", 0), user.id)
    raw = m.text.strip()
    if not a or not plan.proxy:
        await state.clear()
        return await render(m, "🔒 Недоступно", ikb([BACK_MAIN]), state)
    if raw == "-":
        a.proxy = None
    else:
        if not parse_proxy(raw):
            return await render(m, "❌ Неверный формат. Пример: <code>socks5://user:pass@1.2.3.4:1080</code>",
                                cancel_kb(f"acc:v:{a.id}"), state)
        await render(m, "⏳ Проверяю прокси…", None, state)
        if not await check_proxy(raw):
            return await render(m, "❌ Прокси не отвечает или не пропускает трафик до Telegram. Пришлите другой:",
                                cancel_kb(f"acc:v:{a.id}"), state)
        a.proxy = raw
    await state.clear()
    await show_card(m, state, session, user, a, prefix="✅ Прокси сохранён\n\n" if a.proxy else "✅ Прокси удалён\n\n")


# ───────────── удаление ─────────────
@router.callback_query(F.data.startswith("acc:del:"))
async def cb_delete(call: CallbackQuery, state: FSMContext, session, user, sender: SenderService):
    parts = call.data.split(":")
    a = await _owned(session, int(parts[2]), user.id)
    if not a:
        return await call.answer("Аккаунт не найден", show_alert=True)
    if len(parts) == 3:
        await call.answer()
        return await render(call, f"❌ <b>Удалить {esc(a.display_name)}?</b>\n{SEP}\nСессия будет стёрта из БД, "
                                  f"аккаунт уберётся из всех рассылок.", confirm_kb(f"acc:del:{a.id}:ok", f"acc:v:{a.id}"))
    for m in await _mailings_of(session, user.id, a.id):
        m.account_ids = [x for x in m.account_ids if x != a.id]
        if not m.account_ids and m.is_active:
            await session.commit()
            await sender.deactivate(m.id)
    await session.delete(a)
    await call.answer("❌ Аккаунт удалён", show_alert=True)
    await show_list(call, state, session, user)


# ───────────── добавление: шаги ─────────────
def _head(d: dict, step: int, title: str) -> str:
    return f"➕ <b>Добавление аккаунта</b>\n{SEP}\n<b>Шаг {step} из {d.get('total', 2)}</b> · {title}\n\n"


async def _setup_alive(call: CallbackQuery, state: FSMContext) -> bool:
    """Кнопки шагов добавления после перезапуска бота / отмены устаревают — не падаем, а просим начать заново."""
    if (await state.get_data()).get("setup"):
        return True
    await call.answer("⌛ Сессия добавления истекла — начните заново", show_alert=True)
    return False


@router.callback_query(F.data == "acc:add")
async def cb_add(call: CallbackQuery, state: FSMContext, session, user, plan):
    n = (await session.execute(select(func.count()).select_from(Account).where(Account.user_id == user.id))).scalar_one()
    if plan.key == "none":
        return await call.answer("💎 Для подключения аккаунтов нужна подписка PRO или VIP", show_alert=True)
    if n >= plan.max_accounts:
        return await call.answer(f"🚫 Тариф {plan.title}: максимум {lim(plan.max_accounts)} акк.\n💎 Расширьте тариф.", show_alert=True)
    await call.answer()
    await state.clear()
    await drop_pending(user.id)
    d = {"setup": True, "proxy": None, "api_id": None, "api_hash": None, "total": 3 if plan.proxy else 2}
    await state.update_data(**d)
    if plan.proxy:
        await step_proxy(call, state, d)
    else:
        await step_api(call, state, d, 1)


async def step_proxy(event, state, d):
    await render(event, _head(d, 1, "прокси") + "Хотите привязать к аккаунту отдельный SOCKS5-прокси?\n"
                 "Он используется для всех подключений аккаунта.", add_proxy_kb(), state)


async def step_api(event, state, d, step):
    must = not (config.api_id and config.api_hash)
    await render(event, _head(d, step, "API") + "Использовать собственный API ID и Hash?\n"
                 "Получить: https://my.telegram.org\n\n"
                 + ("<b>В настройках бота API по умолчанию не задан — укажите свой.</b>" if must
                    else "Если нет — используются стандартные настройки."),
                 add_api_kb(can_skip=not must), state)


async def step_phone(event, state, d):
    await state.set_state(AddAccount.phone)
    await render(event, _head(d, d.get("total", 2), "вход") + "Введите номер телефона в формате <code>+380991234567</code>\n\n"
                 "Или отправьте <b>файлом</b>: TData (.zip) / Telethon .session\n"
                 "Или строкой <b>StringSession</b>.", cancel_kb(), state)


@router.callback_query(F.data == "acc:add:px")
async def cb_add_px(call: CallbackQuery, state: FSMContext):
    if not await _setup_alive(call, state):
        return
    await call.answer()
    await state.set_state(AddAccount.proxy)
    d = await state.get_data()
    await render(call, _head(d, 1, "прокси") + "Введите прокси:\n<code>socks5://host:port</code>\nили\n"
                 "<code>socks5://user:pass@host:port</code>", cancel_kb(), state)


@router.callback_query(F.data == "acc:add:skippx")
async def cb_add_skippx(call: CallbackQuery, state: FSMContext):
    if not await _setup_alive(call, state):
        return
    await call.answer()
    await step_api(call, state, await state.get_data(), 2)


@router.message(AddAccount.proxy, F.text)
async def on_add_proxy(m: Message, state: FSMContext):
    raw = m.text.strip()
    d = await state.get_data()
    if not parse_proxy(raw):
        return await render(m, "❌ Неверный формат. Пример: <code>socks5://user:pass@1.2.3.4:1080</code>", cancel_kb(), state)
    await render(m, "⏳ Проверяю прокси…", None, state)
    if not await check_proxy(raw):
        return await render(m, "❌ Прокси не отвечает или не пропускает трафик до Telegram. Пришлите другой:", cancel_kb(), state)
    await state.update_data(proxy=raw)
    await state.set_state(None)
    await step_api(m, state, d, 2)


@router.callback_query(F.data == "acc:add:skipapi")
async def cb_add_skipapi(call: CallbackQuery, state: FSMContext):
    if not await _setup_alive(call, state):
        return
    await call.answer()
    await step_phone(call, state, await state.get_data())


@router.callback_query(F.data == "acc:add:api")
async def cb_add_api(call: CallbackQuery, state: FSMContext):
    if not await _setup_alive(call, state):
        return
    await call.answer()
    await state.set_state(AddAccount.api_id)
    await render(call, "🔑 Введите <b>API ID</b> (число):", cancel_kb(), state)


@router.message(AddAccount.api_id, F.text)
async def on_api_id(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        return await render(m, "❌ API ID — это число. Введите снова:", cancel_kb(), state)
    await state.update_data(api_id=int(m.text.strip()))
    await state.set_state(AddAccount.api_hash)
    await render(m, "🔑 Теперь введите <b>API Hash</b> (32 символа):", cancel_kb(), state)


@router.message(AddAccount.api_hash, F.text)
async def on_api_hash(m: Message, state: FSMContext):
    h = m.text.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", h):
        return await render(m, "❌ API Hash — 32 hex-символа. Введите снова:", cancel_kb(), state)
    await state.update_data(api_hash=h)
    await step_phone(m, state, await state.get_data())


# ───────────── вход по номеру ─────────────
async def _persist(event, state, session, user, plan, string: str, me, d: dict):
    n = (await session.execute(select(func.count()).select_from(Account).where(Account.user_id == user.id))).scalar_one()
    if n >= plan.max_accounts:
        return await render(event, "🚫 Лимит аккаунтов тарифа исчерпан.", ikb([BACK_MAIN]), state)
    dup = (await session.execute(select(Account).where(Account.user_id == user.id, Account.tg_id == me.id))).scalar_one_or_none()
    if dup:
        await state.clear()
        return await show_card(event, state, session, user, dup, prefix="⚠️ Этот аккаунт уже добавлен\n\n")
    acc = Account(
        user_id=user.id, tg_id=me.id, phone=f"+{me.phone}" if me.phone else f"@{me.username or me.id}",
        name=((me.first_name or "") + (f" {me.last_name}" if me.last_name else "")).strip(), session=encrypt(string),
        api_id=d.get("api_id"), api_hash=encrypt(d["api_hash"]) if d.get("api_hash") else None, proxy=d.get("proxy"))
    session.add(acc)
    await session.flush()
    await state.clear()
    await show_card(event, state, session, user, acc, prefix="✅ <b>Аккаунт добавлен</b>\n\n")


async def _validate_and_save(m: Message, state, session, user, plan, string: str):
    d = await state.get_data()
    await render(m, "⏳ Проверяю сессию…", None, state)
    try:
        me, normalized = await validate_session(string, d.get("proxy"), d.get("api_id"), d.get("api_hash"))
    except Exception as e:
        log.info("session validation failed: %r", e)
        return await render(m, f"❌ <b>Сессия не прошла проверку</b>\n<code>{esc(type(e).__name__)}: {esc(str(e)[:150])}</code>\n\n"
                               f"Отправьте другую или номер телефона:", cancel_kb(), state)
    await _persist(m, state, session, user, plan, normalized, me, d)


@router.message(AddAccount.phone, F.text)
async def on_phone(m: Message, state: FSMContext, session, user, plan):
    raw = m.text.strip()
    d = await state.get_data()
    if d.get("connecting"):
        return
    if not raw.startswith("+"):
        if len(raw) > 100 and " " not in raw:  # похоже на StringSession
            try:
                await m.delete()
            except Exception:
                pass
            return await _validate_and_save(m, state, session, user, plan, raw)
        return await render(m, "❌ Номер должен начинаться с <code>+</code>. Введите снова:", cancel_kb(), state)
    phone = re.sub(r"[^\d+]", "", raw)
    await state.update_data(phone=phone, connecting=True)
    try:
        await _send_code(m, state, d, phone, user)
    finally:
        await state.update_data(connecting=False)


async def _send_code(m: Message, state: FSMContext, d: dict, phone: str, user):
    await drop_pending(user.id)
    await render(m, "⏳ Отправляю код…", None, state)
    client = make_client("", d.get("proxy"), api_id=d.get("api_id"), api_hash=d.get("api_hash"), seed=device_seed(phone, 0))
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except (PhoneNumberInvalidError, PhoneNumberBannedError, ApiIdInvalidError, FloodWaitError) as e:
        await client.disconnect()
        text = {
            PhoneNumberInvalidError: "❌ Неверный номер телефона. Формат: <code>+380991234567</code>",
            PhoneNumberBannedError: "❌ Этот номер заблокирован Telegram.",
            ApiIdInvalidError: "❌ API ID/Hash недействительны.",
        }.get(type(e), f"⏳ Слишком много попыток. Подождите {getattr(e, 'seconds', '?')} сек.")
        return await render(m, text, cancel_kb(), state)
    except Exception as e:
        await client.disconnect()
        log.info("send_code failed: %r", e)
        return await render(m, f"❌ Не удалось отправить код: <code>{esc(type(e).__name__)}</code>", cancel_kb(), state)
    _pending[user.id] = {"client": client, "phone": phone, "hash": sent.phone_code_hash}
    await state.set_state(AddAccount.code)
    await state.update_data(code="")
    await show_code(m, state, phone)


def _code_text(phone: str, code: str) -> str:
    shown = " ".join(list(code) + ["_"] * (CODE_LEN - len(code)))
    return (f"📲 <b>Введите код</b>\n{SEP}\nTelegram отправил код на <b>{esc(phone)}</b> "
            f"(в приложение или SMS).\nВводите кнопками ниже — так код не аннулируется.\n\n<code>{shown}</code>")


async def show_code(event, state, phone: str):
    code = (await state.get_data()).get("code", "")
    await render(event, _code_text(phone, code), numpad_kb(), state)


@router.message(AddAccount.code)
async def on_code_text(m: Message):
    try:
        await m.delete()  # код, отправленный в чат, Telegram аннулирует — удаляем и просим кнопки
    except Exception:
        pass
    await m.answer("⚠️ Вводите код кнопками под сообщением.")


@router.callback_query(AddAccount.code, F.data.startswith("code:"))
async def cb_code(call: CallbackQuery, state: FSMContext, session, user, plan):
    action = call.data.split(":")[1]
    d = await state.get_data()
    code = d.get("code", "")
    if action == "d":
        if len(code) >= 6:
            return await call.answer()
        code += call.data.split(":")[2]
    elif action == "back":
        code = code[:-1]
    elif action == "clear":
        code = ""
    elif action == "ok":
        if len(code) < CODE_LEN:
            return await call.answer(f"Введите все {CODE_LEN} цифр", show_alert=True)
        return await _confirm_code(call, state, session, user, plan, code)
    await state.update_data(code=code)
    await call.answer()
    await show_code(call, state, d.get("phone", ""))


async def _confirm_code(call: CallbackQuery, state: FSMContext, session, user, plan, code: str):
    p = _pending.get(user.id)
    if not p:
        await state.clear()
        await call.answer("Сессия входа истекла — начните заново", show_alert=True)
        return await render(call, "⌛ Сессия входа истекла.", ikb([BACK_MAIN]), state)
    await call.answer("⏳ Проверяю…")
    try:
        await p["client"].sign_in(phone=p["phone"], code=code, phone_code_hash=p["hash"])
    except SessionPasswordNeededError:
        await state.set_state(AddAccount.password)
        return await render(call, f"🔐 <b>Двухфакторная защита</b>\n{SEP}\nВведите пароль от аккаунта.\n"
                                  f"<i>Сообщение с паролем будет удалено.</i>", cancel_kb(), state)
    except PhoneCodeInvalidError:
        await state.update_data(code="")
        await call.answer("❌ Неверный код", show_alert=True)
        return await show_code(call, state, p["phone"])
    except PhoneCodeExpiredError:
        await drop_pending(user.id)
        await state.set_state(AddAccount.phone)
        return await render(call, "⌛ Код устарел. Введите номер телефона ещё раз:", cancel_kb(), state)
    except FloodWaitError as e:
        return await call.answer(f"⏳ Подождите {e.seconds} сек.", show_alert=True)
    except Exception as e:
        await drop_pending(user.id)
        await state.clear()
        return await render(call, f"❌ Не удалось войти: <code>{esc(type(e).__name__)}</code>", ikb([BACK_MAIN]), state)
    await _finish_login(call, state, session, user, plan)


@router.message(AddAccount.password, F.text)
async def on_password(m: Message, state: FSMContext, session, user, plan):
    pw = m.text
    try:
        await m.delete()
    except Exception:
        pass
    p = _pending.get(user.id)
    if not p:
        await state.clear()
        return await render(m, "⌛ Сессия входа истекла.", ikb([BACK_MAIN]), state)
    try:
        await p["client"].sign_in(password=pw)
    except PasswordHashInvalidError:
        return await render(m, "❌ Неверный пароль. Введите снова:", cancel_kb(), state)
    except FloodWaitError as e:
        return await render(m, f"⏳ Слишком много попыток. Подождите {e.seconds} сек.", cancel_kb(), state)
    except Exception as e:
        await drop_pending(user.id)
        await state.clear()
        return await render(m, f"❌ Не удалось войти: <code>{esc(type(e).__name__)}</code>", ikb([BACK_MAIN]), state)
    await _finish_login(m, state, session, user, plan)


async def _finish_login(event, state: FSMContext, session, user, plan):
    p = _pending.pop(user.id, None)
    d = await state.get_data()
    client = p["client"]
    try:
        me = await client.get_me()
        string = client.session.save()
    finally:
        await client.disconnect()
    await _persist(event, state, session, user, plan, string, me, d)


# ───────────── импорт файлами ─────────────
@router.message(AddAccount.phone, F.document)
async def on_import_file(m: Message, state: FSMContext, bot: Bot, session, user, plan):
    doc = m.document
    name = (doc.file_name or "").lower()
    if doc.file_size > 20 * 1024 * 1024 or not name.endswith((".zip", ".session")):
        return await m.answer("Нужен файл .zip (tdata) или Telethon .session до 20 МБ.")
    tmp = Path(tempfile.mkdtemp(prefix="acc_import_"))
    try:
        await render(m, "⏳ Читаю файл…", None, state)
        path = tmp / "f.session" if name.endswith(".session") else tmp / "a.zip"
        await bot.download(doc, destination=path)
        if name.endswith(".session"):
            string = session_from_file(str(path.with_suffix("")))
        else:
            with zipfile.ZipFile(path) as z:
                z.extractall(tmp / "x")
            found = next((p for p in (tmp / "x").rglob("tdata") if p.is_dir()), None)
            if not found:
                raise ValueError("в архиве нет папки tdata")
            string = await session_from_tdata(str(found))
    except ImportError:
        return await render(m, "❌ Для tdata нужен пакет <code>opentele</code>: <code>pip install opentele</code>", cancel_kb(), state)
    except Exception as e:
        return await render(m, f"❌ Не удалось прочитать файл: <code>{esc(str(e)[:150] or type(e).__name__)}</code>", cancel_kb(), state)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    await _validate_and_save(m, state, session, user, plan, string)
