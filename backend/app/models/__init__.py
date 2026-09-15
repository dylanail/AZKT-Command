"""SQLAlchemy models, one module per domain. Every module in this package is
imported so Base.metadata knows every table (Alembic autogenerate + create_all)."""
from __future__ import annotations

import importlib
import pkgutil

from ..db import Base  # noqa: F401

for _m in pkgutil.iter_modules(__path__):
    if not _m.name.startswith("_"):
        importlib.import_module(f"{__name__}.{_m.name}")

from .auth import Credential, Invitation, User  # noqa: E402,F401
from .legacy import (  # noqa: E402,F401
    IRQ,
    AgentState,
    ChatMessage,
    Customer,
    NotificationLog,
    Setting,
    ShippingEvent,
    StageTransition,
    SyncState,
    UsageEvent,
)
from .vehicles import Vehicle  # noqa: E402,F401
