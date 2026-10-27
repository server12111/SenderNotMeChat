from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import config
from bot.database.models import DEFAULT_METHODS, Base

_is_sqlite = config.database_url.startswith("sqlite")

engine = create_async_engine(
    config.database_url,
    connect_args={"timeout": 30} if _is_sqlite else {},
    pool_pre_ping=not _is_sqlite,
)

if _is_sqlite:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")  # писатель ждёт освобождения блокировки, а не падает с «database is locked»
        cur.close()


Session: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)


def _add_missing_columns(conn) -> None:
    """Лёгкая миграция: добавляет в существующие таблицы новые колонки (со скалярным default)."""
    for table in Base.metadata.sorted_tables:
        existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table.name})")}
        for col in table.columns:
            if col.name in existing:
                continue
            ddl = col.type.compile(dialect=conn.dialect)
            default = getattr(getattr(col, "default", None), "arg", None)
            if isinstance(default, bool):
                default = int(default)
            clause = f" DEFAULT {default!r}" if isinstance(default, (int, float, str)) else ""
            conn.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}{clause}")


def _seed_methods(conn) -> None:
    existing = {row[0] for row in conn.exec_driver_sql("SELECT key FROM payment_methods")}
    for key, title, currency, rate, sort in DEFAULT_METHODS:
        if key not in existing:
            conn.exec_driver_sql(
                "INSERT INTO payment_methods (key, title, currency, rate, enabled, sort) VALUES (?, ?, ?, ?, 0, ?)",
                (key, title, currency, rate, sort))


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if _is_sqlite:
            await conn.run_sync(_add_missing_columns)
        await conn.run_sync(_seed_methods)
