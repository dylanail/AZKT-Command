"""Non-production destination guard (spec §13/§14, acceptance H08).

A staging or development deployment must never reach a production destination by accident: an email to a real
customer, a publish to the live website, a Telegram message to the owner's real chat. The rule is simple and
mechanical: outside `ENV=production`, any transport that actually delivers may only reach destinations listed in
`NON_PROD_DESTINATION_ALLOWLIST` (comma-separated emails, `@domains`, chat ids or URL prefixes). Transports that do
not leave the process (memory, log) are exempt because nothing is delivered.
"""
from __future__ import annotations

from .config import settings
from .errors import Blocked


def _allowlist() -> list[str]:
    return [x.strip().lower() for x in (settings.NON_PROD_DESTINATION_ALLOWLIST or "").split(",") if x.strip()]


def guard_active() -> bool:
    return settings.ENV != "production"


def _allowed(target: str, allow: list[str]) -> bool:
    t = (target or "").strip().lower()
    if not t:
        return False
    for a in allow:
        if a.startswith("@") and "@" in t and t.endswith(a):
            return True
        if t == a or t.startswith(a):
            return True
    return False


def assert_destination_allowed(kind: str, *targets: str) -> None:
    """Raise Blocked when a delivering transport in a non-production environment targets something not allowlisted.

    `kind` is 'email' | 'site' | 'telegram' (used only for the message)."""
    if not guard_active():
        return
    allow = _allowlist()
    bad = [t for t in targets if not _allowed(t, allow)]
    if bad:
        raise Blocked(f"{settings.ENV} must not reach a production {kind} destination: {', '.join(bad)} "
                      f"(add it to NON_PROD_DESTINATION_ALLOWLIST if this is intended)", status="destination_blocked")
