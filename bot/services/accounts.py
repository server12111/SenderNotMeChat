"""Telethon-обвязка: клиенты, прокси, валидация сессий."""
from __future__ import annotations

import asyncio
import re

from telethon import TelegramClient
from telethon.sessions import StringSession

from bot.config import config

# IP DC2 Telegram — через него проверяем, что SOCKS5 реально пропускает трафик до Telegram
_TG_PROBE = ("149.154.167.51", 443)


def parse_proxy(raw: str) -> dict | None:
    """socks5://user:pass@host:port | user:pass@host:port | host:port | host:port:user:pass"""
    raw = re.sub(r"^socks5h?://", "", raw.strip(), flags=re.I)
    user = pwd = None
    try:
        if "@" in raw:
            cred, hp = raw.rsplit("@", 1)
            user, pwd = cred.split(":", 1)
            host, port = hp.split(":")
        else:
            p = raw.split(":")
            if len(p) == 2:
                host, port = p
            elif len(p) == 4:
                host, port, user, pwd = p
            else:
                return None
        port = int(port)
    except ValueError:
        return None
    if not host or not 0 < port < 65536:
        return None
    return {"host": host, "port": port, "user": user, "pwd": pwd}


def telethon_proxy(raw: str | None) -> dict | None:
    p = parse_proxy(raw) if raw else None
    if not p:
        return None
    return {"proxy_type": "socks5", "addr": p["host"], "port": p["port"],
            "username": p["user"], "password": p["pwd"], "rdns": True}


async def check_proxy(raw: str, timeout: float = 10) -> bool:
    from python_socks import ProxyType
    from python_socks.async_.asyncio.v2 import Proxy

    p = parse_proxy(raw)
    if not p:
        return False
    try:
        proxy = Proxy.create(ProxyType.SOCKS5, p["host"], p["port"], p["user"], p["pwd"])
        sock = await asyncio.wait_for(proxy.connect(dest_host=_TG_PROBE[0], dest_port=_TG_PROBE[1]), timeout)
        sock.close()
        return True
    except Exception:
        return False


# Аккаунт представляется Android-устройством, а не «библиотекой Telethon» — меньше риска слёта.
# Устройство стабильно для аккаунта (seed = id), чтобы оно не менялось между запусками.
_DEVICES = [
    ("Samsung SM-S918B", "SDK 34", "10.14.5"),
    ("Google Pixel 8", "SDK 34", "10.14.5"),
    ("Xiaomi 23078PND5G", "SDK 33", "10.13.2"),
    ("OnePlus CPH2581", "SDK 34", "10.14.1"),
]


def make_client(session: str, proxy: str | None = None, listen: bool = False,
                api_id: int | None = None, api_hash: str | None = None, seed: int = 0) -> TelegramClient:
    device, system, app = _DEVICES[seed % len(_DEVICES)]
    return TelegramClient(
        StringSession(session), api_id or config.api_id, api_hash or config.api_hash,
        proxy=telethon_proxy(proxy), receive_updates=listen,
        device_model=device, system_version=system, app_version=app, lang_code="ru", system_lang_code="ru-RU",
        connection_retries=2, request_retries=2, retry_delay=1, timeout=15,
    )


def device_seed(phone: str, fallback: int) -> int:
    """Устройство стабильно для аккаунта и известно ещё до входа: берём цифры номера."""
    digits = re.sub(r"\D", "", phone or "")
    return int(digits) if digits else fallback


def client_for(acc, listen: bool = False) -> TelegramClient:
    """Клиент по записи Account (расшифровывает сессию и свой API, если задан)."""
    from bot.utils.crypto import decrypt
    return make_client(decrypt(acc.session), acc.proxy, listen, acc.api_id,
                       decrypt(acc.api_hash) if acc.api_hash else None, seed=device_seed(acc.phone, acc.id))


def session_from_file(path: str) -> str:
    """Telethon-файл .session (sqlite, без расширения в path) -> StringSession."""
    from telethon.sessions import SQLiteSession

    sess = SQLiteSession(path)
    try:
        if not sess.auth_key:
            raise ValueError("в файле нет авторизации")
        out = StringSession()
        out.set_dc(sess.dc_id, sess.server_address, sess.port)
        out.auth_key = sess.auth_key
        return out.save()
    finally:
        sess.close()


async def validate_session(session: str, proxy: str | None = None, api_id: int | None = None,
                           api_hash: str | None = None):
    """Подключается, проверяет авторизацию. -> (me, нормализованная StringSession). Бросает исключение при ошибке."""
    client = make_client(session, proxy, api_id=api_id, api_hash=api_hash)
    try:
        await asyncio.wait_for(client.connect(), 30)
        if not await client.is_user_authorized():
            raise ValueError("Сессия не авторизована или отозвана")
        me = await client.get_me()
        return me, client.session.save()
    finally:
        await client.disconnect()


async def session_from_tdata(tdata_path: str) -> str:
    """Опционально: нужен пакет opentele (pip install opentele)."""
    from opentele.api import UseCurrentSession
    from opentele.td import TDesktop

    tdesk = TDesktop(tdata_path)
    if not tdesk.isLoaded():
        raise ValueError("tdata повреждена или защищена локальным паролем")
    client = await tdesk.ToTelethon(session=StringSession(), flag=UseCurrentSession)
    try:
        await client.connect()
        return client.session.save()
    finally:
        await client.disconnect()
