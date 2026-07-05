"""Хеширование паролей и серверные сессии администраторов.

PBKDF2-HMAC-SHA256 из стандартной библиотеки — без внешних зависимостей
(bcrypt/passlib не нужны). Формат хранения: "<соль_hex>$<хеш_hex>".
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

PBKDF2_ITERATIONS = 260_000
SESSION_TTL = timedelta(days=14)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest_hex = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return secrets.compare_digest(digest.hex(), digest_hex)


def new_session_token() -> str:
    return secrets.token_urlsafe(40)


def session_expiry() -> datetime:
    return datetime.utcnow() + SESSION_TTL
