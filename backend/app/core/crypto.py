"""Application-level encryption for provider tokens (spec §13.3)."""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


def _key() -> bytes:
    """The Fernet key, derived the same way every time from the configured value.

    `.strip()` is load-bearing, not tidiness. A 44-character key is used verbatim while anything else
    is hashed into one, so a single trailing newline — which `openssl rand -base64 32 | pbcopy` puts
    on the clipboard — silently sends the same secret down the other branch and yields a *different*
    key. Nothing fails at that moment: tokens encrypt fine. The damage appears later, when the
    whitespace is tidied up and every stored provider token has become undecryptable.
    """
    raw = settings.ENCRYPTION_KEY.strip()
    if not raw:
        if settings.is_production:
            raise RuntimeError("ENCRYPTION_KEY must be set in production")
        raw = "dev-only-" + settings.SESSION_SECRET.strip()
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
