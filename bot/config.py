from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

def _data_dir() -> Path:
    """Всё, что нужно пережить перезапуск (база, ключ шифрования сессий, медиа), лежит в одной папке.
    На хостинге это /app/data (сохраняется при перезапуске); локально — ./data. Можно задать DATA_DIR."""
    env = os.getenv("DATA_DIR", "").strip()
    if env:
        p = Path(env)
        return p if p.is_absolute() else BASE_DIR / p
    return Path("/app/data") if Path("/app").is_dir() and os.name != "nt" else BASE_DIR / "data"


def _writable(path: Path) -> bool:
    """Папка существует (создаём при необходимости) и в неё реально можно писать."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


DATA_DIR = _data_dir()
DATA_DIR_PROBLEM: str | None = None
if not _writable(DATA_DIR):
    # Папка недоступна для записи (нет прав / нет такого диска). Не падаем с «unable to open database file»:
    # берём запасную, но предупреждаем — данные в ней могут не пережить перезапуск.
    import tempfile
    for _fallback in (BASE_DIR / "data", Path(tempfile.gettempdir()) / "sender_data"):
        if _writable(_fallback):
            DATA_DIR_PROBLEM = f"папка данных {DATA_DIR} недоступна для записи, использую {_fallback}"
            DATA_DIR = _fallback
            break
    else:
        raise SystemExit(f"Нет доступной для записи папки для данных (пробовал {DATA_DIR}). Задайте DATA_DIR.")
MEDIA_DIR = DATA_DIR / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def _sqlite_file(p: Path) -> Path:
    """Относительный путь — внутрь DATA_DIR; абсолютный — как есть, но только если туда можно писать."""
    if not p.is_absolute():
        return DATA_DIR / p.name
    if _writable(p.parent):
        return p
    global DATA_DIR_PROBLEM
    DATA_DIR_PROBLEM = (DATA_DIR_PROBLEM or "") + f" Папка для базы {p.parent} недоступна, база будет в {DATA_DIR}."
    return DATA_DIR / p.name


def _database_url() -> str:
    """SQLite всегда кладём внутрь DATA_DIR (относительные пути из старых .env не уводят базу из сохраняемой папки).
    PostgreSQL и прочие — как есть."""
    default = f"sqlite+aiosqlite:///{(DATA_DIR / 'bot.db').as_posix()}"
    url = os.getenv("DATABASE_URL", "").strip().strip("'\"")
    if not url:
        return default

    if "://" not in url:  # просто путь к файлу («/app/data/bot.db», «data/bot.db», «bot.db»)
        p = Path(url)
        return f"sqlite+aiosqlite:///{_sqlite_file(p).as_posix()}"

    scheme = url.split("://", 1)[0]
    if scheme.startswith("sqlite"):
        path = url.split(":///", 1)[1] if ":///" in url else ""
        if not path:
            return default
        p = Path(path)
        # абсолютный путь — как есть, относительный — внутрь DATA_DIR
        return f"sqlite+aiosqlite:///{_sqlite_file(p).as_posix()}"

    if scheme in ("postgres", "postgresql"):  # так хостинги выдают URL; драйвер у нас asyncpg
        url = "postgresql+asyncpg://" + url.split("://", 1)[1]
    try:
        from sqlalchemy.engine import make_url
        make_url(url)
        if not url.startswith(("postgresql", "mysql")):
            raise ValueError("неизвестная СУБД")
    except Exception:  # нераспознаваемая строка не должна ронять запуск
        import logging
        logging.getLogger(__name__).warning("DATABASE_URL не распознан, использую SQLite в %s", DATA_DIR)
        return default
    return url


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    api_id: int
    api_hash: str
    database_url: str
    support_username: str
    max_running: int
    encryption_key: str


def _load() -> Config:
    admins = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
    return Config(
        bot_token=os.getenv("BOT_TOKEN", ""),
        admin_ids=frozenset(admins),
        api_id=int(os.getenv("API_ID", "0") or 0),
        api_hash=os.getenv("API_HASH", ""),
        database_url=_database_url(),
        support_username=os.getenv("SUPPORT_USERNAME", "").lstrip("@"),
        max_running=int(os.getenv("MAX_RUNNING", "20") or 20),
        encryption_key=os.getenv("ENCRYPTION_KEY", ""),
    )


config = _load()
