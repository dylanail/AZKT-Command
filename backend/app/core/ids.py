from __future__ import annotations

import hashlib
import json
import secrets
import uuid


def new_id() -> str:
    return str(uuid.uuid4())


def token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def stable_hash(obj) -> str:
    """Deterministic hash of a JSON-able payload (approval binding, idempotency)."""
    return sha256_hex(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str))


def human_ref(prefix: str, n: int) -> str:
    return f"{prefix}-{n:04d}"
