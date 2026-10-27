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


DATA_DIR = _data_dir()
MEDIA_DIR = DATA_DIR / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def _database_url() -> str:
    """SQLite всегда кладём внутрь DATA_DIR (относительные пути из старых .env не уводят базу из сохраняемой папки).
    PostgreSQL и прочие — как есть."""
    url = os.getenv("DATABASE_URL", "").strip()
    if url and not url.startswith("sqlite"):
        return url
    name = Path(url.split("///", 1)[1]).name if "///" in url else "bot.db"
    if url and "///" in url and Path(url.split("///", 1)[1]).is_absolute():
        return url
    return f"sqlite+aiosqlite:///{(DATA_DIR / name).as_posix()}"


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
