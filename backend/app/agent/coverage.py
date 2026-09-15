"""Capability coverage map: owner UI command -> Manager tool (spec §10.9, acceptance I06).

"Every business record/field the owner can edit in the app must have a supported command/tool route
available through Manager." This module builds that map from live data — the command REGISTRY and each
stage-1 service's `COMMANDS_FOR_MANAGER` dict — so it cannot drift from the code. `GET /api/agent/coverage`
renders it for the owner and `test_runtime` asserts it is complete.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil

from ..domain.actors import Actor
from ..domain.commands import REGISTRY
from ..domain.policy import has_perm
from . import tools

log = logging.getLogger("azkt.agent.coverage")


def ui_actions() -> dict[str, list[str]]:
    """command name -> the owner UI actions that route to it (from services/*.COMMANDS_FOR_MANAGER)."""
    out: dict[str, list[str]] = {}
    from .. import services as pkg
    for m in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda x: x.name):
        if m.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{pkg.__name__}.{m.name}")
        except Exception as e:  # noqa: BLE001  (a concurrently edited sibling module must not break the map)
            log.warning("coverage: skipping services.%s: %s: %s", m.name, type(e).__name__, e)
            continue
        table = getattr(mod, "COMMANDS_FOR_MANAGER", None)
        if isinstance(table, dict):
            for action, command in table.items():
                out.setdefault(command, []).append(action)
    return out


def coverage_map() -> dict:
    """Every registered command with its Manager tool, action class, permission and UI action(s)."""
    actions = ui_actions()
    writes = tools.write_tools()
    rows = []
    for name, spec in sorted(REGISTRY.items()):
        t = writes.get(name)
        rows.append({
            "command": name,
            "tool": t.tool_name if t else None,
            "action_class": spec.action_class,
            "perm": spec.perm,
            "ui_action": actions.get(name, []),
            "covered": t is not None,
            "excluded_reason": tools.EXCLUDED_COMMANDS.get(name),
            "description": (spec.description or "").split("\n")[0][:240],
        })
    reads = [{"tool": s.tool_name, "name": s.name, "perm": s.perm, "kind": "read",
              "description": s.description[:240]} for s in sorted(tools.READ_TOOLS.values(), key=lambda s: s.name)]
    ui_total = sorted(actions.keys())
    gaps = [c for c in ui_total if c not in writes]
    return {
        "policy_version": _policy_version(),
        "commands": rows,
        "read_tools": reads,
        "counts": {"commands": len(rows), "write_tools": len(writes), "read_tools": len(reads),
                   "ui_commands": len(ui_total), "excluded": len(tools.EXCLUDED_COMMANDS)},
        "ui_commands": ui_total,
        "gaps": gaps,
        "excluded": [{"command": k, "reason": v} for k, v in sorted(tools.EXCLUDED_COMMANDS.items())],
        "complete": not gaps,
    }


def for_actor(actor: Actor) -> dict:
    """The same map annotated with what this actor could actually run right now (I06: an employee does
    not inherit owner access by asking the owner-capable Manager)."""
    m = coverage_map()
    for row in m["commands"]:
        row["available"] = bool(row["covered"] and (row["perm"] is None or has_perm(actor, row["perm"]))
                                and not (row["action_class"] == "forbidden_for_agents" and actor.kind != "user")
                                and not (row["action_class"] == "owner_only" and actor.role != "owner"))
    for row in m["read_tools"]:
        row["available"] = row["perm"] is None or has_perm(actor, row["perm"])
    m["actor"] = {"kind": actor.kind, "role": actor.role, "scope": actor.scope,
                  "client_id": actor.client_id, "client_scopes": list(actor.client_scopes or [])}
    m["counts"]["available_commands"] = sum(1 for r in m["commands"] if r["available"])
    return m


def _policy_version() -> str:
    from ..domain import policy
    return policy.POLICY_VERSION
