"""Forbidden-recipient guard.

Email send is post-v1, but the rule is enforced now so any future send path
(or an agent action routed through the dashboard) physically cannot reply to
wordpress@azkeitrucks.com. Mirrors the inbox agent's existing validator.
"""
from __future__ import annotations

from ..core.config import settings


class ForbiddenRecipientError(Exception):
    pass


def forbidden_set() -> set[str]:
    return {e.strip().lower() for e in settings.FORBIDDEN_RECIPIENTS.split(",") if e.strip()}


def assert_allowed(*recipients: str) -> None:
    bad = forbidden_set()
    for r in recipients:
        if r and r.strip().lower() in bad:
            raise ForbiddenRecipientError(f"recipient '{r}' is on the forbidden list")
