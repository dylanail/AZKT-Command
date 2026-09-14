"""Actor model: who is acting, on whose behalf, with which effective grant (ARCHITECTURE.md)."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

# Permission keys. Role defaults live in policy.ROLE_DEFAULTS; per-person overrides in users.perms.
PERM_KEYS = (
    "vehicles.read", "vehicles.write", "vehicles.all",
    "tasks.read", "tasks.write", "tasks.assign", "tasks.verify",
    "contacts.read", "contacts.write",
    "sales.read", "sales.write",
    "requests.read", "requests.write",
    "shipping.read", "shipping.write",
    "inbox.read", "inbox.draft", "inbox.send",
    "listings.read", "listings.draft", "listings.publish",
    "parts.request", "parts.order",
    "documents.read", "documents.write",
    "costs.read", "finance.status", "finance.write",
    "activity.read", "agents.chat", "intake",
    "approve", "team", "settings", "connections", "knowledge.write", "permissions",
)

# Map of perm key -> external client scope that satisfies it.
PERM_TO_CLIENT_SCOPE = {
    "vehicles.read": "read:vehicles", "vehicles.write": "write:vehicles",
    "tasks.read": "read:tasks", "tasks.write": "write:tasks",
    "contacts.read": "read:contacts", "contacts.write": "write:contacts",
    "sales.read": "read:sales", "sales.write": "write:sales",
    "requests.read": "read:requests", "requests.write": "write:requests",
    "shipping.read": "read:shipping",
    "costs.read": "read:costs", "finance.status": "read:costs",
    "activity.read": "read:activity",
    "inbox.draft": "draft:messages", "inbox.read": "read:sources",
    "intake": "intake", "agents.chat": "ask",
    "documents.read": "read:sources",
}


@dataclass
class Actor:
    kind: str = "user"                 # user | agent | external | system
    user_id: str | None = None         # the person (or the person the agent/client acts for)
    role: str = "owner"
    scope: str = "all"                 # all | assigned
    perms: dict = field(default_factory=dict)   # effective per-key booleans (resolved by policy)
    display_name: str = ""
    client_id: str | None = None       # external client id when kind == external
    client_name: str | None = None
    client_scopes: list = field(default_factory=list)
    client_record_scope: dict = field(default_factory=dict)
    agent_role: str | None = None      # manager / customer_sales / ... when kind == agent
    delegation_depth: int = 0
    mission_id: str | None = None
    run_id: str | None = None

    @property
    def key(self) -> str:
        """Stable identity used for idempotency scoping."""
        if self.kind == "external":
            return f"external:{self.client_id}"
        if self.kind == "system":
            return "system"
        return f"{self.kind}:{self.user_id}"

    @property
    def is_owner(self) -> bool:
        return self.role == "owner" and self.kind in ("user", "agent")

    @property
    def is_human(self) -> bool:
        return self.kind == "user"

    def snapshot(self) -> dict:
        d = asdict(self)
        d.pop("perms", None)
        return d


SYSTEM_ACTOR = Actor(kind="system", user_id=None, role="system", scope="all", display_name="AZKT")
