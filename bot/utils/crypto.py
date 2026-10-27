from __future__ import annotations

from cryptography.fernet import Fernet

from bot.config import DATA_DIR, config


def _load_key() -> bytes:
    if config.encryption_key:
        return config.encryption_key.encode()
    path = DATA_DIR / "secret.key"
    if not path.exists():
        path.write_bytes(Fernet.generate_key())
    return path.read_bytes().strip()


_fernet = Fernet(_load_key())


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _fernet.decrypt(value.encode()).decode()
