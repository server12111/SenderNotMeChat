from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Наивное UTC-время: во всей БД храним только UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    full_name: Mapped[str] = mapped_column(String(256), default="")
    is_banned: Mapped[bool] = mapped_column(default=False)
    tz_offset: Mapped[int] = mapped_column(default=0)
    notify: Mapped[bool] = mapped_column(default=True)
    discount: Mapped[int] = mapped_column(default=0)  # разовая скидка из промокода, центы USD
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(default=utcnow)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), unique=True)
    plan: Mapped[str] = mapped_column(String(16), default="none")
    expires_at: Mapped[datetime | None] = mapped_column(default=None)
    notified_3d: Mapped[bool] = mapped_column(default=False)
    notified_1d: Mapped[bool] = mapped_column(default=False)
    notified_exp: Mapped[bool] = mapped_column(default=False)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    tg_id: Mapped[int] = mapped_column(BigInteger)
    phone: Mapped[str] = mapped_column(String(32), default="")
    name: Mapped[str] = mapped_column(String(128), default="")
    title: Mapped[str | None] = mapped_column(String(48), default=None)  # своё имя («Переименовать»)
    session: Mapped[str] = mapped_column(Text)  # зашифрованная StringSession
    api_id: Mapped[int | None] = mapped_column(default=None)  # свой API (иначе из .env)
    api_hash: Mapped[str | None] = mapped_column(Text, default=None)  # зашифрован
    proxy: Mapped[str | None] = mapped_column(Text, default=None)
    auto_subscribe: Mapped[bool] = mapped_column(default=True)  # автоподписка на спонсоров
    is_active: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok | limited | dead
    cooldown_until: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    @property
    def display_name(self) -> str:
        return self.title or self.name or self.phone


class Mailing(Base):
    """Постоянная рассылка: круг по всем чатам -> пауза «КД между циклами» -> снова. Живёт, пока включена."""

    __tablename__ = "mailings"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    account_ids: Mapped[list] = mapped_column(JSON, default=list)
    interval_seconds: Mapped[int] = mapped_column(default=600)  # КД между циклами
    delay_min: Mapped[int] = mapped_column(default=15)  # КД между чатами (случайно min..max)
    delay_max: Mapped[int] = mapped_column(default=40)
    spintax: Mapped[bool] = mapped_column(default=False)
    hours: Mapped[list | None] = mapped_column(JSON, default=None)  # [[start_min, end_min], ...] в UTC; None = 24/7
    folders: Mapped[list] = mapped_column(JSON, default=list)  # slug-и t.me/addlist/..., добавляются на аккаунт в начале цикла
    is_active: Mapped[bool] = mapped_column(default=False, index=True)
    cycle: Mapped[int] = mapped_column(default=0)
    cycle_sent: Mapped[int] = mapped_column(default=0)
    cycle_failed: Mapped[int] = mapped_column(default=0)
    next_cycle_at: Mapped[datetime | None] = mapped_column(default=None)
    last_sent_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class MailingMessage(Base):
    """Вариант сообщения. Вариантов несколько — каждый раз берётся случайный."""

    __tablename__ = "mailing_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    mailing_id: Mapped[int] = mapped_column(ForeignKey("mailings.id"), index=True)
    text: Mapped[str] = mapped_column(Text, default="")  # HTML
    media_path: Mapped[str | None] = mapped_column(Text, default=None)


class MailingTarget(Base):
    __tablename__ = "mailing_targets"
    __table_args__ = (Index("ix_target_mailing_cycle", "mailing_id", "done_cycle"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mailing_id: Mapped[int] = mapped_column(ForeignKey("mailings.id"))
    target: Mapped[str] = mapped_column(String(256))  # @username | числовой id | +inviteHash
    done_cycle: Mapped[int] = mapped_column(default=-1)  # цикл, в котором цель обработана; pending, если < Mailing.cycle
    last_error: Mapped[str | None] = mapped_column(String(128), default=None)


class SendLog(Base):
    """Одна строка = одно успешно отправленное сообщение (дневные лимиты и дашборд)."""

    __tablename__ = "send_log"
    __table_args__ = (Index("ix_sendlog_user_at", "user_id", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    account_id: Mapped[int] = mapped_column(index=True)
    mailing_id: Mapped[int] = mapped_column()
    at: Mapped[datetime] = mapped_column(default=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


# ───────────── тарифы, оплата, промокоды (система из проекта invite) ─────────────
def fmt_usd(cents: int) -> str:
    return f"${cents // 100}" if cents % 100 == 0 else f"${cents / 100:.2f}"


class PromoType:
    DISCOUNT = "discount_usd"  # value — в центах
    ADD_DAYS = "add_days"
    ALL = (DISCOUNT, ADD_DAYS)
    TITLES = {DISCOUNT: "Скидка ($)", ADD_DAYS: "Дни подписки"}


class OrderStatus:
    NEW = "new"  # реквизиты показаны
    PENDING = "pending"  # пользователь нажал «Я оплатил», ждёт проверки
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class Plan(Base):
    """Тариф, который админ создаёт в панели. tier определяет возможности (PRO / VIP)."""

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    price: Mapped[int] = mapped_column()  # центы USD
    duration_days: Mapped[int] = mapped_column()
    tier: Mapped[str] = mapped_column(String(8), default="pro")


class Promocode(Base):
    __tablename__ = "promocodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    type: Mapped[str] = mapped_column(String(16))
    value: Mapped[int] = mapped_column()
    tier: Mapped[str] = mapped_column(String(8), default="pro")  # какой тариф выдать за дни, если своего нет
    max_activations: Mapped[int] = mapped_column(default=0)  # 0 = без лимита
    used_count: Mapped[int] = mapped_column(default=0)


class PromocodeActivation(Base):
    __tablename__ = "promocode_activations"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey("promocodes.id", ondelete="CASCADE"), primary_key=True)
    activated_at: Mapped[datetime] = mapped_column(default=utcnow)


class PaymentMethod(Base):
    """Способ оплаты; реквизиты и курс задаёт админ."""

    __tablename__ = "payment_methods"

    key: Mapped[str] = mapped_column(String(16), primary_key=True)
    title: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(8))
    rate: Mapped[float] = mapped_column(default=0)  # единиц валюты за $1
    auto_rate: Mapped[bool | None] = mapped_column(default=None)  # None = авто, если есть источник
    rate_updated_at: Mapped[datetime | None] = mapped_column(default=None)
    address: Mapped[str | None] = mapped_column(String(255), default=None)
    enabled: Mapped[bool] = mapped_column(default=False)
    sort: Mapped[int] = mapped_column(default=0)


DEFAULT_METHODS = [
    # key, title, currency, rate, sort
    ("uah", "Карта (грн)", "грн", 41.0, 1),
    ("ton", "USDT · TON", "USDT", 1.0, 2),
    ("trc20", "USDT · TRC20", "USDT", 1.0, 3),
    ("erc20", "USDT · ERC20", "USDT", 1.0, 4),
    ("bep20", "USDT · BEP20", "USDT", 1.0, 5),
    ("btc", "BTC", "BTC", 0.0, 6),
]


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    plan_id: Mapped[int] = mapped_column()
    # условия тарифа и реквизиты фиксируются на момент заказа
    plan_name: Mapped[str] = mapped_column(String(64))
    tier: Mapped[str] = mapped_column(String(8), default="pro")
    days: Mapped[int] = mapped_column()
    discount: Mapped[int] = mapped_column(default=0)  # центы
    amount_usd: Mapped[int] = mapped_column()  # центы, уже со скидкой
    method: Mapped[str] = mapped_column(String(16))
    amount_text: Mapped[str] = mapped_column(String(64))
    address: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default=OrderStatus.NEW, index=True)
    admin_msgs: Mapped[str | None] = mapped_column(String(1024), default=None)  # JSON [[chat_id, msg_id], ...]
    decided_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(default=None)
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
