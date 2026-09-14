"""Owner-only settings surface (spec §2.3 Settings, §11.5 deterministic controls).

GET /api/settings merges defaults with stored overrides and adds gate rules, pause controls and
connection-freshness placeholders. Non-owners get a bare 403. Every write is a command.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, current_actor
from ..db import get_db
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..domain.policy import has_perm
from ..services import settings_store as store

router = APIRouter(prefix="/api/settings", tags=["settings"])


async def _owner(actor: Actor = Depends(current_actor)) -> Actor:
    if actor.kind != "user" or not has_perm(actor, "settings"):
        raise HTTPException(403, "not allowed")
    return actor


@router.get("")
async def get_settings(actor: Actor = Depends(_owner), db: AsyncSession = Depends(get_db)):
    entries = await store.all_entries(db)
    return {
        "settings": entries,
        "effective": {k: v["value"] for k, v in entries.items()},
        "gate_rules": await store.effective_gate_rules(db),
        "controls": await store.list_controls(db),
        "outstanding": await store.outstanding_actions(db),
        "connections": await store.connection_freshness(db),
        "allowed_keys": sorted(store.SETTINGS_SPEC),
    }


@router.get("/pause")
async def get_pause(actor: Actor = Depends(_owner), db: AsyncSession = Depends(get_db)):
    controls = await store.list_controls(db)
    return {"items": controls, "total": len(controls), "outstanding": await store.outstanding_actions(db),
            "any_paused": any(c["paused"] for c in controls)}


@router.post("/pause")
async def set_pause(body: store.PauseIn, actor: Actor = Depends(_owner), ctx: CommandContext = Depends(command_context)):
    return (await dispatch(ctx, "settings.pause", body)).to_dict()


@router.get("/gate-rules")
async def gate_rules(to_state: str | None = None, actor: Actor = Depends(_owner), db: AsyncSession = Depends(get_db)):
    rules = await store.effective_gate_rules(db, to_state)
    return {"items": rules, "total": len(rules), "requirements": list(store.GATE_REQUIREMENTS),
            "factual": sorted(store.FACTUAL_REQUIREMENTS)}


@router.post("/gate-rules")
async def upsert_gate_rule(body: store.GateRuleUpsertIn, actor: Actor = Depends(_owner), ctx: CommandContext = Depends(command_context)):
    return (await dispatch(ctx, "settings.gate_rule_upsert", body)).to_dict()


class _Ver(BaseModel):
    expected_version: int | None = None


@router.post("/gate-rules/{rule_id}/deactivate")
async def deactivate_gate_rule(rule_id: str, body: _Ver | None = None, actor: Actor = Depends(_owner),
                               ctx: CommandContext = Depends(command_context)):
    body = body or _Ver()
    return (await dispatch(ctx, "settings.gate_rule_deactivate", {"rule_id": rule_id, **body.model_dump()})).to_dict()


@router.post("/gate-rules/reset")
async def reset_gate_rules(body: store.GateRulesResetIn | None = None, actor: Actor = Depends(_owner),
                           ctx: CommandContext = Depends(command_context)):
    return (await dispatch(ctx, "settings.gate_rules_reset", body or store.GateRulesResetIn())).to_dict()


@router.get("/{key}")
async def get_setting(key: str, actor: Actor = Depends(_owner), db: AsyncSession = Depends(get_db)):
    if key not in store.SETTINGS_SPEC:
        raise HTTPException(404, "unknown setting")
    return await store.get_entry(db, key)


class _Update(BaseModel):
    value: dict
    expected_version: int | None = None
    replace: bool = False


@router.put("/{key}")
async def put_setting(key: str, body: _Update, actor: Actor = Depends(_owner), ctx: CommandContext = Depends(command_context)):
    return (await dispatch(ctx, "settings.update", {"key": key, **body.model_dump()})).to_dict()


@router.post("/{key}")
async def post_setting(key: str, body: _Update, actor: Actor = Depends(_owner), ctx: CommandContext = Depends(command_context)):
    return (await dispatch(ctx, "settings.update", {"key": key, **body.model_dump()})).to_dict()
