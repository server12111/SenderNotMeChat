"""Рассылки: список -> карточка -> сообщения / чаты / время / аккаунты; пошаговое создание."""
from __future__ import annotations

import asyncio
import html
import logging
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import delete, func, select

from bot.config import MEDIA_DIR
from bot.database.db import Session
from bot.database.models import Account, Mailing, MailingMessage, MailingTarget, User, utcnow
from bot.handlers.states import MailingFlow
from bot.keyboards import mailings as K
from bot.keyboards.menus import BACK_MAIN, cancel_kb, confirm_kb
from bot.middlewares.guard import PlanGuardMiddleware
from bot.services.clients import ClientError, ClientManager
from bot.services.joiner import ADDLIST_RE, inspect_folder
from bot.services.plans import lim
from bot.services.sender import SenderService
from bot.utils.text import (
    SEP, fmt_dt, fmt_duration, fmt_hours, hours_wait, parse_delay, parse_duration, parse_hours, parse_targets,
    progress_bar,
)
from bot.utils.ui import PAGE_SIZE, esc, ikb, paginate, render

log = logging.getLogger(__name__)
router = Router(name="mailings")
router.callback_query.middleware(PlanGuardMiddleware())

MIN_CYCLE = 30  # сек, минимальный «КД между циклами»
MAX_TARGETS = 20000
STEP = "➕ <b>Создание рассылки</b>\n" + SEP + "\n<b>Шаг {n}/7</b> · "


# ═════════════════════════════ список ═════════════════════════════
async def _mailings(session, uid: int, account: Account | None = None) -> list[Mailing]:
    ms = (await session.execute(select(Mailing).where(Mailing.user_id == uid).order_by(Mailing.id.desc()))).scalars().all()
    return [m for m in ms if account is None or account.id in (m.account_ids or [])]


async def show_list(event, state, session, user, page: int = 0, account: Account | None = None):
    ms = await _mailings(session, user.id, account)
    chunk, page, total = paginate(ms, page)
    head = f"✉️ <b>Рассылки аккаунта {esc(account.display_name)}</b>" if account else "📋 <b>Ваши рассылки</b>"
    text = f"{head}\n{SEP}\n"
    if chunk:
        for m in chunk:
            name = m.name if len(m.name) <= 60 else m.name[:57] + "…"
            text += f"• {esc(name)} — {'🟢 Активна' if m.is_active else '🔴 Остановлена'}\n"
    else:
        text += "Рассылок пока нет.\n"
    if total > 1:
        text += f"\nСтраница {page + 1}/{total}."
    text += "\nВыберите рассылку или создайте новую:"
    kb = (K.mailings_kb(ms, page, f"acc:ml:{account.id}", ("◀️ Назад", f"acc:v:{account.id}")) if account
          else K.mailings_kb(ms, page))
    await render(event, text, kb, state)


@router.callback_query(F.data.startswith("ml:list:"))
async def cb_list(call: CallbackQuery, state: FSMContext, session, user):
    await state.clear()
    await call.answer()
    await show_list(call, state, session, user, int(call.data.split(":")[2]))


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


# ═════════════════════════════ карточка ═════════════════════════════
async def build_card(mid: int) -> tuple[str, object] | None:
    """Собирается из своей сессии — так же вызывается из автообновления."""
    async with Session() as s:
        m = await s.get(Mailing, mid)
        if not m:
            return None
        u = await s.get(User, m.user_id)
        tz = u.tz_offset if u else 0
        accs = (await s.execute(select(Account).where(Account.id.in_(m.account_ids or [])))).scalars().all()
        n_msg = (await s.execute(select(func.count()).select_from(MailingMessage).where(MailingMessage.mailing_id == mid))).scalar_one()
        total = (await s.execute(select(func.count()).select_from(MailingTarget).where(MailingTarget.mailing_id == mid))).scalar_one()
        pending = (await s.execute(select(func.count()).select_from(MailingTarget).where(
            MailingTarget.mailing_id == mid, MailingTarget.done_cycle < m.cycle))).scalar_one()
    now = utcnow()
    if not m.is_active:
        status = "🔴 Остановлена"
    elif m.next_cycle_at and m.next_cycle_at > now:
        status = "🟢 Активна · ⏳ ждёт следующий цикл"
    elif hours_wait(m.hours, now) > 0:
        status = "🟢 Активна · ⏰ вне времени активности"
    else:
        status = "🟢 Активна · идёт отправка"
    done = total - pending
    text = (f"📋 <b>Рассылка: {esc(m.name)}</b>\n{SEP}\n"
            f"Статус: {status}\n"
            f"Аккаунты: {esc(', '.join(a.display_name for a in accs)) or 'не выбраны'}\n"
            f"КД между циклами: {fmt_duration(m.interval_seconds)}\n"
            f"КД между чатами: {m.delay_min}–{m.delay_max} сек\n"
            f"Время активности: {fmt_hours(m.hours, tz)}\n"
            f"🎲 Спинтакс: {'ВКЛ' if m.spintax else 'ВЫКЛ'}\n"
            f"Сообщений: {n_msg} · Целевых чатов: {total}\n{SEP}\n"
            f"Цикл №{m.cycle + 1}\n<code>{progress_bar(done, total)}</code>\n"
            f"✅ Доставлено: <b>{m.cycle_sent}</b> · ❌ Ошибки: <b>{m.cycle_failed}</b> · ⏳ В очереди: <b>{pending}</b>\n"
            f"Последняя отправка: {fmt_dt(m.last_sent_at, tz) if m.last_sent_at else 'никогда'}")
    if m.is_active and m.next_cycle_at and m.next_cycle_at > now:
        text += f"\nСледующий цикл: <b>{fmt_dt(m.next_cycle_at, tz)}</b>"
    return text + "\n\nВыберите действие:", K.mailing_card_kb(m)


_live: dict[tuple[int, int], asyncio.Task] = {}
LIVE_STEPS, LIVE_EVERY = 40, 5  # автообновление карточки ≈ 3,5 минуты


async def show_card(event, state, mid: int, prefix: str = ""):
    card = await build_card(mid)
    if card is None:
        if isinstance(event, CallbackQuery):
            await event.answer("Рассылка не найдена", show_alert=True)
        return
    text, kb = card
    await render(event, prefix + text, kb, state)
    if isinstance(event, CallbackQuery):
        msg = event.message
        key = (msg.chat.id, msg.message_id)
        old = _live.pop(key, None)
        if old:
            old.cancel()
        _live[key] = asyncio.create_task(_live_loop(event.bot, key, mid))


async def _live_loop(bot: Bot, key: tuple[int, int], mid: int) -> None:
    last = None
    try:
        for _ in range(LIVE_STEPS):
            await asyncio.sleep(LIVE_EVERY)
            card = await build_card(mid)
            if card is None:
                return
            text, kb = card
            if text != last:
                try:
                    await bot.edit_message_text(text, chat_id=key[0], message_id=key[1], reply_markup=kb)
                except TelegramBadRequest as e:
                    if "not modified" not in str(e):
                        return  # сообщение удалено или пользователь ушёл на другой экран
                last = text
            async with Session() as s:
                active = (await s.execute(select(Mailing.is_active).where(Mailing.id == mid))).scalar_one_or_none()
            if not active:
                return
    finally:
        if _live.get(key) is asyncio.current_task():
            _live.pop(key, None)


async def _owned(session, mid: int, uid: int) -> Mailing | None:
    m = await session.get(Mailing, mid)
    return m if m and m.user_id == uid else None


async def _need(call: CallbackQuery, session, uid: int, mid: int) -> Mailing | None:
    m = await _owned(session, mid, uid)
    if not m:
        await call.answer("Рассылка не найдена", show_alert=True)
    return m


@router.callback_query(F.data.startswith("ml:v:"))
async def cb_view(call: CallbackQuery, state: FSMContext, session, user):
    await state.clear()
    mid = int(call.data.split(":")[2])
    if not await _need(call, session, user.id, mid):
        return
    await call.answer()
    await show_card(call, state, mid)


async def _validate_ready(session, m: Mailing) -> str | None:
    n_msg = (await session.execute(select(func.count()).select_from(MailingMessage).where(MailingMessage.mailing_id == m.id))).scalar_one()
    n_tg = (await session.execute(select(func.count()).select_from(MailingTarget).where(MailingTarget.mailing_id == m.id))).scalar_one()
    n_acc = (await session.execute(select(func.count()).select_from(Account).where(
        Account.id.in_(m.account_ids or []), Account.user_id == m.user_id, Account.is_active.is_(True)))).scalar_one()
    if not n_acc:
        return "📱 Выберите рабочий аккаунт (кнопка «Аккаунты»)"
    if not n_msg:
        return "📝 Добавьте хотя бы одно сообщение"
    if not n_tg:
        return "🎯 Добавьте хотя бы один целевой чат"
    return None


@router.callback_query(F.data.startswith("ml:tg:"))
async def cb_toggle(call: CallbackQuery, state: FSMContext, session, user, sender: SenderService):
    m = await _need(call, session, user.id, int(call.data.split(":")[2]))
    if not m:
        return
    mid = m.id
    if m.is_active:
        await session.commit()
        await sender.deactivate(mid)
        await call.answer("⏹ Рассылка остановлена")
    else:
        err = await _validate_ready(session, m)
        if err:
            return await call.answer(err, show_alert=True)
        m.next_cycle_at = None  # ручной запуск — без ожидания КД
        await session.commit()
        await sender.activate(mid)
        await call.answer("🚀 Рассылка запущена")
    await show_card(call, state, mid)


@router.callback_query(F.data.startswith("ml:launch:"))
async def cb_launch(call: CallbackQuery, state: FSMContext, session, user, sender: SenderService):
    m = await _need(call, session, user.id, int(call.data.split(":")[2]))
    if not m:
        return
    err = await _validate_ready(session, m)
    if err:
        return await call.answer(err, show_alert=True)
    mid = m.id
    m.next_cycle_at = None
    await session.commit()
    await sender.activate(mid)
    await state.clear()
    await call.answer("🚀 Рассылка запущена!")
    await show_card(call, state, mid, prefix="🚀 <b>Рассылка запущена!</b>\n\n")


@router.callback_query(F.data.startswith("ml:spin:"))
async def cb_spin(call: CallbackQuery, state: FSMContext, session, user, plan):
    m = await _need(call, session, user.id, int(call.data.split(":")[2]))
    if not m:
        return
    if not plan.spintax:
        return await call.answer("💎 Спинтакс доступен с тарифа PRO", show_alert=True)
    m.spintax = not m.spintax
    await session.commit()
    await call.answer("🎲 Спинтакс включён: {а|б|в} в тексте выбираются случайно" if m.spintax else "🎲 Спинтакс выключен",
                      show_alert=m.spintax)
    await show_card(call, state, m.id)


# ═════════════════════════════ удаление рассылки ═════════════════════════════
@router.callback_query(F.data.startswith("ml:del:"))
async def cb_delete(call: CallbackQuery, state: FSMContext, session, user, sender: SenderService):
    parts = call.data.split(":")
    m = await _need(call, session, user.id, int(parts[2]))
    if not m:
        return
    if len(parts) == 3:
        await call.answer()
        return await render(call, f"❌ <b>Удалить рассылку «{esc(m.name)}»?</b>\n{SEP}\nСообщения и список чатов будут удалены.",
                            confirm_kb(f"ml:del:{m.id}:ok", f"ml:v:{m.id}"))
    mid = m.id
    await session.commit()
    await sender.deactivate(mid)
    msgs = (await session.execute(select(MailingMessage).where(MailingMessage.mailing_id == mid))).scalars().all()
    for x in msgs:
        if x.media_path:
            Path(x.media_path).unlink(missing_ok=True)
    await session.execute(delete(MailingMessage).where(MailingMessage.mailing_id == mid))
    await session.execute(delete(MailingTarget).where(MailingTarget.mailing_id == mid))
    await session.execute(delete(Mailing).where(Mailing.id == mid))
    await call.answer("❌ Рассылка удалена")
    await show_list(call, state, session, user)


# ═════════════════════════════ создание ═════════════════════════════
@router.callback_query(F.data == "ml:new")
async def cb_new(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await state.set_state(MailingFlow.name)
    await state.update_data(draft=True)
    await render(call, STEP.format(n=1) + "название\n\nВведите название рассылки:", cancel_kb(), state)


@router.message(MailingFlow.name, F.text)
async def on_name(m: Message, state: FSMContext, session, user, plan):
    accs = (await session.execute(select(Account).where(
        Account.user_id == user.id, Account.is_active.is_(True), Account.status != "dead").order_by(Account.id))).scalars().all()
    if not accs:
        await state.clear()
        return await render(m, "❌ У вас нет рабочих аккаунтов.\nСначала добавьте аккаунт в разделе «Аккаунты».", ikb([BACK_MAIN]), state)
    await state.update_data(name=m.text.strip()[:60], accounts=[accs[0].id])
    await state.set_state(None)
    await step_accounts(m, state, session, user, plan)


async def step_accounts(event, state, session, user, plan):
    accs = (await session.execute(select(Account).where(
        Account.user_id == user.id, Account.is_active.is_(True), Account.status != "dead").order_by(Account.id))).scalars().all()
    d = await state.get_data()
    chosen = set(d.get("accounts") or [])
    await render(event, STEP.format(n=2) + f"аккаунты\n\nОтметьте один или несколько — рассылка пойдёт по кругу (ротация).\n"
                 f"Выбрано: <b>{len(chosen)}</b> из {lim(plan.max_accounts)}",
                 K.accounts_pick_kb(accs, chosen, "ml:cacc", ("Далее ▶️", "ml:cnext"), ("✖️ Отмена", "cancel")), state)


@router.callback_query(F.data.startswith("ml:cacc:"))
async def cb_cacc(call: CallbackQuery, state: FSMContext, session, user, plan):
    aid = int(call.data.split(":")[2])
    chosen = list((await state.get_data()).get("accounts") or [])
    if aid in chosen:
        chosen.remove(aid)
    elif len(chosen) >= plan.max_accounts:
        return await call.answer(f"Тариф {plan.title}: до {lim(plan.max_accounts)} акк. в рассылке", show_alert=True)
    else:
        chosen.append(aid)
    await state.update_data(accounts=chosen)
    await call.answer()
    await step_accounts(call, state, session, user, plan)


@router.callback_query(F.data == "ml:cnext")
async def cb_cnext(call: CallbackQuery, state: FSMContext):
    if not (await state.get_data()).get("accounts"):
        return await call.answer("📱 Выберите хотя бы один аккаунт", show_alert=True)
    await call.answer()
    await state.set_state(MailingFlow.interval)
    await render(call, STEP.format(n=3) + "КД между циклами\n\nЧерез сколько снова писать в тот же чат (после полного круга).\n"
                 "Например: <code>300</code> (секунды), <code>5м</code>, <code>2ч</code>", cancel_kb(), state)


async def _parse_interval(m: Message, state) -> int | None:
    sec = parse_duration(m.text or "")
    if sec is None or sec < MIN_CYCLE or sec > 30 * 86400:
        await render(m, f"❌ Введите время от {MIN_CYCLE} сек до 30 дней: <code>300</code>, <code>5м</code>, <code>2ч</code>",
                     cancel_kb(), state)
        return None
    return sec


async def _parse_chat_delay(m: Message, state, plan) -> tuple[int, int] | None:
    raw = (m.text or "").strip()
    r = parse_delay(raw) or ((int(raw), int(raw)) if raw.isdigit() else None)
    if not r:
        await render(m, "❌ Введите число секунд или диапазон: <code>20</code> или <code>15-40</code>", cancel_kb(), state)
        return None
    if r[0] < plan.min_delay:
        await render(m, f"❌ Минимум для вашего тарифа ({plan.title}) — <b>{plan.min_delay} сек</b>. Введите снова:", cancel_kb(), state)
        return None
    return r[0], min(r[1], 3600)


@router.message(MailingFlow.interval, F.text)
async def on_interval(m: Message, state: FSMContext, session, user, plan):
    sec = await _parse_interval(m, state)
    if sec is None:
        return
    d = await state.get_data()
    if d.get("draft"):
        await state.update_data(interval=sec)
        await state.set_state(MailingFlow.chat_delay)
        return await render(m, STEP.format(n=4) + f"КД между чатами\n\nПауза между отправками в разные чаты внутри одного круга.\n"
                            f"Число или диапазон секунд: <code>20</code> / <code>15-40</code>\nМинимум для вашего тарифа: {plan.min_delay} сек.",
                            cancel_kb(), state)
    mid = d["mid"]
    mm = await _owned(session, mid, user.id)
    if mm:
        mm.interval_seconds = sec
    await state.set_state(None)
    await show_targets(m, state, session, user, mid, 0, prefix=f"✅ КД между циклами: {fmt_duration(sec)}\n\n")


@router.message(MailingFlow.chat_delay, F.text)
async def on_chat_delay(m: Message, state: FSMContext, session, user, plan):
    r = await _parse_chat_delay(m, state, plan)
    if r is None:
        return
    d = await state.get_data()
    if d.get("draft"):
        mailing = Mailing(user_id=user.id, name=d["name"], account_ids=d["accounts"], interval_seconds=d["interval"],
                          delay_min=r[0], delay_max=r[1])
        session.add(mailing)
        await session.flush()
        await state.update_data(draft=False, creating=mailing.id)
        await state.set_state(None)
        return await show_messages(m, state, session, user, mailing.id, 0)
    mm = await _owned(session, d["mid"], user.id)
    if mm:
        mm.delay_min, mm.delay_max = r
    await state.set_state(None)
    await show_targets(m, state, session, user, d["mid"], 0, prefix=f"✅ КД между чатами: {r[0]}–{r[1]} сек\n\n")


@router.callback_query(F.data.startswith("ml:iv:"))
async def cb_iv(call: CallbackQuery, state: FSMContext, session, user):
    m = await _need(call, session, user.id, int(call.data.split(":")[2]))
    if not m:
        return
    await call.answer()
    await state.set_state(MailingFlow.interval)
    await state.update_data(mid=m.id, draft=False)
    await render(call, f"⏱ <b>КД между циклами</b>\n{SEP}\nСейчас: <b>{fmt_duration(m.interval_seconds)}</b>\n\n"
                 f"Через сколько снова писать в тот же чат (после полного круга).\n<code>300</code>, <code>5м</code>, <code>2ч</code>",
                 cancel_kb(f"ml:tgts:{m.id}:0"), state)


@router.callback_query(F.data.startswith("ml:cd:"))
async def cb_cd(call: CallbackQuery, state: FSMContext, session, user, plan):
    m = await _need(call, session, user.id, int(call.data.split(":")[2]))
    if not m:
        return
    await call.answer()
    await state.set_state(MailingFlow.chat_delay)
    await state.update_data(mid=m.id, draft=False)
    await render(call, f"⏱ <b>КД между чатами</b>\n{SEP}\nСейчас: <b>{m.delay_min}–{m.delay_max} сек</b>\n\n"
                 f"Число или диапазон секунд: <code>20</code> / <code>15-40</code>\nМинимум для вашего тарифа: {plan.min_delay} сек.",
                 cancel_kb(f"ml:tgts:{m.id}:0"), state)


@router.callback_query(F.data.startswith("ml:cmdone:"))
async def cb_cmdone(call: CallbackQuery, state: FSMContext, session, user):
    mid = int(call.data.split(":")[2])
    if not await _need(call, session, user.id, mid):
        return
    await call.answer()
    await show_targets(call, state, session, user, mid, 0)


@router.callback_query(F.data.startswith("ml:ctdone:"))
async def cb_ctdone(call: CallbackQuery, state: FSMContext, session, user):
    mid = int(call.data.split(":")[2])
    if not await _need(call, session, user.id, mid):
        return
    await call.answer()
    await render(call, STEP.format(n=7) + "время активности\n\nНастроить часы работы рассылки или работать 24/7?", K.hours_kb(mid), state)


# ═════════════════════════════ сообщения ═════════════════════════════
def _creating(d: dict, mid: int) -> bool:
    return d.get("creating") == mid


def _preview_text(msg: MailingMessage) -> str:
    import re
    t = html.unescape(re.sub(r"<[^>]+>", "", msg.text or "")).replace("\n", " ").strip()
    t = t[:60] + "…" if len(t) > 60 else t
    return ("[медиа] " if msg.media_path else "") + (t or "—")


async def show_messages(event, state, session, user, mid: int, page: int, prefix: str = ""):
    msgs = (await session.execute(select(MailingMessage).where(MailingMessage.mailing_id == mid).order_by(MailingMessage.id))).scalars().all()
    chunk, page, total = paginate(msgs, page)
    d = await state.get_data()
    creating = _creating(d, mid)
    head = STEP.format(n=5) + "сообщения\n\n" if creating else ""
    text = f"{prefix}{head}📝 <b>Сообщения рассылки</b> ({len(msgs)} шт.)\n{SEP}\n"
    if creating:
        text += "Можно добавить текст, фото или видео. Несколько сообщений — для рандомизации (каждый раз выбирается случайное).\n\n"
    if chunk:
        for i, x in enumerate(chunk, page * PAGE_SIZE + 1):
            text += f"{i}. {esc(_preview_text(x))}\n"
        text += "\nНажмите на сообщение, чтобы удалить его:"
    else:
        text += "Сообщений пока нет."
    await render(event, text, K.messages_kb(mid, msgs, page, creating), state)


@router.callback_query(F.data.startswith("ml:msgs:"))
async def cb_msgs(call: CallbackQuery, state: FSMContext, session, user):
    _, _, mid, page = call.data.split(":")
    if not await _need(call, session, user.id, int(mid)):
        return
    await call.answer()
    await show_messages(call, state, session, user, int(mid), int(page))


@router.callback_query(F.data.startswith("ml:madd:"))
async def cb_madd(call: CallbackQuery, state: FSMContext, session, user):
    mid = int(call.data.split(":")[2])
    if not await _need(call, session, user.id, mid):
        return
    await call.answer()
    await state.set_state(MailingFlow.message)
    await state.update_data(mid=mid)
    await render(call, f"📝 <b>Новое сообщение</b>\n{SEP}\nОтправьте текст, фото или видео (с подписью).\n"
                 f"Форматирование (жирный, ссылки, премиум-эмодзи) сохраняется.\n\n"
                 f"<i>Спинтакс (PRO): {{Привет|Здравствуйте}}</i>", cancel_kb(f"ml:msgs:{mid}:0"), state)


@router.message(MailingFlow.message)
async def on_message_variant(m: Message, state: FSMContext, bot: Bot, session, user):
    mid = (await state.get_data()).get("mid")
    if not mid or not await _owned(session, mid, user.id):
        await state.clear()
        return await render(m, "Сессия устарела.", ikb([BACK_MAIN]), state)
    text = m.html_text if (m.text or m.caption) else ""
    fid, ext = None, ""
    if m.photo:
        fid, ext = m.photo[-1].file_id, ".jpg"
    elif m.video:
        fid, ext = m.video.file_id, ".mp4"
    elif m.animation:
        fid, ext = m.animation.file_id, ".mp4"
    elif m.document:
        fid, ext = m.document.file_id, Path(m.document.file_name or "").suffix or ".bin"
    elif not m.text:
        return await m.answer("Поддерживаются текст, фото, видео и файлы.")
    limit = 1024 if fid else 4096
    if len(m.text or m.caption or "") > limit:
        return await m.answer(f"Слишком длинно: максимум {limit} символов.")
    path = None
    if fid:
        path = str(MEDIA_DIR / f"{user.id}_{mid}_{m.message_id}{ext}")
        try:
            await bot.download(fid, destination=path)
        except TelegramBadRequest:
            return await m.answer("Файл слишком большой (Bot API отдаёт до 20 МБ).")
    session.add(MailingMessage(mailing_id=mid, text=text, media_path=path))
    await session.flush()
    await state.set_state(None)
    await show_messages(m, state, session, user, mid, 10**6, prefix="✅ Сообщение добавлено\n\n")


async def _msg_owned(call: CallbackQuery, session, uid: int, msg_id: int):
    x = await session.get(MailingMessage, msg_id)
    if not x or not await _owned(session, x.mailing_id, uid):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return None
    return x


@router.callback_query(F.data.startswith("ml:mdel:"))
async def cb_mdel(call: CallbackQuery, state: FSMContext, session, user):
    parts = call.data.split(":")
    x = await _msg_owned(call, session, user.id, int(parts[2]))
    if not x:
        return
    mid = x.mailing_id
    if len(parts) == 3:
        await call.answer()
        return await render(call, f"🗑 <b>Удалить сообщение?</b>\n{SEP}\n{esc(_preview_text(x))}",
                            confirm_kb(f"ml:mdel:{x.id}:ok", f"ml:msgs:{mid}:0"))
    if x.media_path:
        Path(x.media_path).unlink(missing_ok=True)
    await session.delete(x)
    await session.flush()
    await call.answer("🗑 Удалено")
    await show_messages(call, state, session, user, mid, 0)


@router.callback_query(F.data.startswith("ml:mdelall:"))
async def cb_mdelall(call: CallbackQuery, state: FSMContext, session, user):
    parts = call.data.split(":")
    mid = int(parts[2])
    if not await _need(call, session, user.id, mid):
        return
    if len(parts) == 3:
        await call.answer()
        return await render(call, f"🗑 <b>Удалить все сообщения рассылки?</b>\n{SEP}\nБудут удалены все варианты текстов.",
                            confirm_kb(f"ml:mdelall:{mid}:ok", f"ml:msgs:{mid}:0", "✅ Да, удалить все"))
    msgs = (await session.execute(select(MailingMessage).where(MailingMessage.mailing_id == mid))).scalars().all()
    for x in msgs:
        if x.media_path:
            Path(x.media_path).unlink(missing_ok=True)
    await session.execute(delete(MailingMessage).where(MailingMessage.mailing_id == mid))
    await call.answer("🗑 Все сообщения удалены")
    await show_messages(call, state, session, user, mid, 0)


# ═════════════════════════════ целевые чаты ═════════════════════════════
async def show_targets(event, state, session, user, mid: int, page: int, prefix: str = ""):
    tg = (await session.execute(select(MailingTarget).where(MailingTarget.mailing_id == mid).order_by(MailingTarget.id))).scalars().all()
    chunk, page, total = paginate(tg, page)
    d = await state.get_data()
    creating = _creating(d, mid)
    head = STEP.format(n=6) + "целевые чаты\n\n" if creating else ""
    text = f"{prefix}{head}🎯 <b>Целевые чаты</b> ({len(tg)} шт.)\n{SEP}\n"
    if creating:
        text += ("Добавьте чаты: ссылки, @username, ID, инвайты <code>t.me/+…</code>, <b>папку</b> <code>t.me/addlist/…</code> "
                 "или файл .txt.\n<i>В чаты из ссылок аккаунт вступает сам — не сразу, с паузой.</i>\n\n")
    if chunk:
        for i, t in enumerate(chunk, page * PAGE_SIZE + 1):
            text += f"{i}. {esc(t.target)}{' ⚠️ ' + esc(t.last_error) if t.last_error else ''}\n"
        text += "\nНажмите на чат, чтобы удалить его:"
    else:
        text += "Целевых чатов пока нет."
    await render(event, text, K.targets_kb(mid, tg, page, creating), state)


@router.callback_query(F.data.startswith("ml:tgts:"))
async def cb_tgts(call: CallbackQuery, state: FSMContext, session, user):
    _, _, mid, page = call.data.split(":")
    if not await _need(call, session, user.id, int(mid)):
        return
    await state.set_state(None)
    await call.answer()
    await show_targets(call, state, session, user, int(mid), int(page))


def _ask(kind: str, mid: int) -> str:
    return {
        "tadd": "🎯 Отправьте чаты: ссылки <code>t.me/name</code>, <code>@username</code>, ID, инвайты <code>t.me/+…</code>\n"
                "Можно списком — по одному на строку. Дубликаты уберу сам.",
        "tfold": "📁 Отправьте ссылку на папку <code>t.me/addlist/…</code> (можно несколько).\n"
                 "Чаты папки добавятся в рассылку, а сама папка — на каждый аккаунт, если её там нет.",
        "ttxt": "📄 Отправьте файл <b>.txt</b> со списком чатов (по одному в строке).",
    }[kind]


@router.callback_query(F.data.regexp(r"^ml:(tadd|tfold|ttxt):\d+$"))
async def cb_add_targets(call: CallbackQuery, state: FSMContext, session, user):
    kind, mid = call.data.split(":")[1], int(call.data.split(":")[2])
    if not await _need(call, session, user.id, mid):
        return
    await call.answer()
    await state.set_state({"tadd": MailingFlow.target, "tfold": MailingFlow.folder, "ttxt": MailingFlow.txt}[kind])
    await state.update_data(mid=mid)
    await render(call, _ask(kind, mid), cancel_kb(f"ml:tgts:{mid}:0"), state)


async def _ingest(m: Message, state: FSMContext, bot: Bot, session, user, clients: ClientManager, raw: str) -> None:
    """Общий разбор текста/файла: чаты + ссылки на папки."""
    d = await state.get_data()
    mid = d.get("mid")
    mailing = await _owned(session, mid, user.id) if mid else None
    if not mailing:
        await state.clear()
        return await render(m, "Сессия устарела.", ikb([BACK_MAIN]), state)
    existing = set((await session.execute(select(MailingTarget.target).where(MailingTarget.mailing_id == mid))).scalars().all())
    new: dict[str, None] = {}
    notes: list[str] = []

    slugs = list(dict.fromkeys(ADDLIST_RE.findall(raw)))
    if slugs:
        accs = (await session.execute(select(Account).where(
            Account.id.in_(mailing.account_ids or []), Account.user_id == user.id, Account.is_active.is_(True)))).scalars().all()
        await render(m, "⏳ Читаю папку…", None, state)
        r, acc = None, None
        for acc in accs:
            try:
                r = await clients.acquire(acc)
                break
            except ClientError as e:
                notes.append(f"⚠️ {esc(acc.display_name)}: {esc(str(e))}")
        if r is None:
            notes.append("❌ Нет доступного аккаунта, чтобы прочитать папку")
        else:
            try:
                folders = list(mailing.folders or [])
                for slug in slugs:
                    try:
                        title, found, skipped = await inspect_folder(r.client, slug)
                    except Exception as e:
                        notes.append(f"❌ Папка {esc(slug)}: <code>{esc(type(e).__name__)}</code>")
                        continue
                    if slug not in folders:
                        folders.append(slug)
                    fresh = [t for t in found if t not in existing and t not in new]
                    new.update(dict.fromkeys(fresh))
                    notes.append(f"📂 «{esc(title)}»: +{len(fresh)} чатов" + (f" (каналов без права писать пропущено: {skipped})" if skipped else ""))
                mailing.folders = folders
            finally:
                await clients.release(acc.id)
        raw = ADDLIST_RE.sub(" ", raw)

    items, dups, bad = parse_targets(raw)
    fresh = [t for t in items if t not in existing and t not in new]
    new.update(dict.fromkeys(fresh))
    if items or bad:
        notes.append(f"➕ Добавлено чатов: {len(fresh)} · дубликатов: {dups + len(items) - len(fresh)} · невалидных: {bad}")
    room = MAX_TARGETS - len(existing)
    for t in list(new)[:max(0, room)]:
        session.add(MailingTarget(mailing_id=mid, target=t))
    await session.flush()
    await state.set_state(None)
    await show_targets(m, state, session, user, mid, 10**6, prefix=("\n".join(notes) + "\n\n") if notes else "")


@router.message(MailingFlow.target, F.text)
@router.message(MailingFlow.folder, F.text)
async def on_targets_text(m: Message, state: FSMContext, bot: Bot, session, user, clients: ClientManager):
    await _ingest(m, state, bot, session, user, clients, m.text)


@router.message(MailingFlow.txt, F.document)
async def on_targets_txt(m: Message, state: FSMContext, bot: Bot, session, user, clients: ClientManager):
    if not (m.document.file_name or "").lower().endswith(".txt") or m.document.file_size > 2 * 1024 * 1024:
        return await m.answer("Нужен файл .txt до 2 МБ.")
    raw = (await bot.download(m.document)).read().decode("utf-8", errors="ignore")
    await _ingest(m, state, bot, session, user, clients, raw)


@router.callback_query(F.data.startswith("ml:tdel:"))
async def cb_tdel(call: CallbackQuery, state: FSMContext, session, user):
    parts = call.data.split(":")
    t = await session.get(MailingTarget, int(parts[2]))
    if not t or not await _owned(session, t.mailing_id, user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    mid = t.mailing_id
    if len(parts) == 3:
        await call.answer()
        return await render(call, f"🗑 <b>Удалить чат из рассылки?</b>\n{SEP}\n{esc(t.target)}", confirm_kb(f"ml:tdel:{t.id}:ok", f"ml:tgts:{mid}:0"))
    await session.delete(t)
    await session.flush()
    await call.answer("🗑 Чат удалён")
    await show_targets(call, state, session, user, mid, 0)


@router.callback_query(F.data.startswith("ml:export:"))
async def cb_export(call: CallbackQuery, session, user):
    mid = int(call.data.split(":")[2])
    if not await _need(call, session, user.id, mid):
        return
    tg = (await session.execute(select(MailingTarget.target).where(MailingTarget.mailing_id == mid).order_by(MailingTarget.id))).scalars().all()
    if not tg:
        return await call.answer("Список чатов пуст", show_alert=True)
    await call.answer()
    await call.message.answer_document(BufferedInputFile("\n".join(tg).encode(), f"chats_{mid}.txt"), caption=f"📤 Чатов: {len(tg)}")


# ═════════════════════════════ время активности ═════════════════════════════
@router.callback_query(F.data.startswith("ml:hrs:") | F.data.startswith("ml:hset:"))
async def cb_hours(call: CallbackQuery, state: FSMContext, session, user):
    mid = int(call.data.split(":")[2])
    m = await _need(call, session, user.id, mid)
    if not m:
        return
    await call.answer()
    await state.set_state(MailingFlow.hours)
    await state.update_data(mid=mid)
    back = f"ml:v:{mid}" if not _creating(await state.get_data(), mid) else "cancel"
    await render(call, f"⏰ <b>Время активности</b>\n{SEP}\nСейчас: <b>{fmt_hours(m.hours, user.tz_offset)}</b>\n\n"
                 f"Время — в вашем часовом поясе (UTC{user.tz_offset:+d}).\nВведите диапазон:\n<code>10:00-13:00</code>\n\n"
                 f"Несколько диапазонов через запятую:\n<code>10:00-13:00, 18:00-22:00</code>\n\n"
                 f"Или отправьте <code>сброс</code> для работы 24/7", cancel_kb(back), state)


@router.message(MailingFlow.hours, F.text)
async def on_hours(m: Message, state: FSMContext, session, user):
    d = await state.get_data()
    mid = d.get("mid")
    mailing = await _owned(session, mid, user.id) if mid else None
    if not mailing:
        await state.clear()
        return await render(m, "Сессия устарела.", ikb([BACK_MAIN]), state)
    raw = m.text.strip()
    if raw.lower() in ("сброс", "reset", "-"):
        hours = None
    else:
        hours = parse_hours(raw, user.tz_offset)
        if hours is None:
            return await render(m, "❌ Не распознал. Формат: <code>10:00-13:00</code> или <code>10:00-13:00, 18:00-22:00</code>",
                                cancel_kb(f"ml:v:{mid}"), state)
    mailing.hours = hours
    await state.set_state(None)
    if _creating(d, mid):
        return await render(m, f"✅ <b>Рассылка создана!</b>\nВремя активности: {fmt_hours(hours, user.tz_offset)}\n\n"
                               f"Нажмите «Запустить рассылку», чтобы начать отправку.", K.launch_kb(mid), state)
    await session.flush()
    await show_card(m, state, mid, prefix=f"✅ Время активности: {fmt_hours(hours, user.tz_offset)}\n\n")


@router.callback_query(F.data.startswith("ml:hskip:"))
async def cb_hskip(call: CallbackQuery, state: FSMContext, session, user):
    mid = int(call.data.split(":")[2])
    if not await _need(call, session, user.id, mid):
        return
    await call.answer()
    await render(call, "✅ <b>Рассылка создана!</b>\n\nНажмите «Запустить рассылку», чтобы начать отправку.", K.launch_kb(mid), state)


# ═════════════════════════════ аккаунты рассылки ═════════════════════════════
async def show_acc_pick(event, state, session, user, plan, mid: int):
    m = await _owned(session, mid, user.id)
    accs = (await session.execute(select(Account).where(Account.user_id == user.id).order_by(Account.id))).scalars().all()
    chosen = set(m.account_ids or [])
    await render(event, f"🔃 <b>Аккаунты рассылки</b>\n{SEP}\nОтметьте аккаунты — рассылка идёт по кругу.\n"
                 f"Выбрано: <b>{len(chosen)}</b> из {lim(plan.max_accounts)}\n<i>Изменения применятся со следующего цикла.</i>",
                 K.accounts_pick_kb(accs, chosen, f"ml:accset:{mid}", None, ("◀️ Назад", f"ml:v:{mid}")), state)


@router.callback_query(F.data.regexp(r"^ml:acc:\d+$"))
async def cb_acc(call: CallbackQuery, state: FSMContext, session, user, plan):
    mid = int(call.data.split(":")[2])
    if not await _need(call, session, user.id, mid):
        return
    await call.answer()
    await show_acc_pick(call, state, session, user, plan, mid)


@router.callback_query(F.data.startswith("ml:accset:"))
async def cb_accset(call: CallbackQuery, state: FSMContext, session, user, plan):
    _, _, mid, aid = call.data.split(":")
    m = await _need(call, session, user.id, int(mid))
    acc = await session.get(Account, int(aid))
    if not m or not acc or acc.user_id != user.id:
        return
    ids = list(m.account_ids or [])
    if int(aid) in ids:
        if len(ids) == 1:
            return await call.answer("В рассылке должен остаться хотя бы один аккаунт", show_alert=True)
        ids.remove(int(aid))
    elif len(ids) >= plan.max_accounts:
        return await call.answer(f"Тариф {plan.title}: до {lim(plan.max_accounts)} акк. в рассылке", show_alert=True)
    else:
        ids.append(int(aid))
    m.account_ids = ids
    await call.answer()
    await show_acc_pick(call, state, session, user, plan, m.id)
