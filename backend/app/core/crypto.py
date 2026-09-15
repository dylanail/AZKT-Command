"""Application-level encryption for provider tokens (spec §13.3)."""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


def _key() -> bytes:
    raw = settings.ENCRYPTION_KEY
    if not raw:
        if settings.is_production:
            raise RuntimeError("ENCRYPTION_KEY must be set in production")
        raw = "dev-only-" + settings.SESSION_SECRET
    if len(raw) == 44 and raw.endswith("="):
        return raw.encode()
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())


def encrypt(plain: str) -> str:
    return Fernet(_key()).encrypt(plain.encode()).decode()


def decrypt(cipher: str) -> str:
    try:
        return Fernet(_key()).decrypt(cipher.encode()).decode()
    except InvalidToken as e:
        raise ValueError("cannot decrypt stored secret (key rotated?)") from e
