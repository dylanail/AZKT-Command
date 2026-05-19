"""Unified chat history across every agent.

Two sources, merged newest-first:

  - ``chat_messages`` table — conversations started from this dashboard.
  - OpenClaw session transcripts — everything else the agent did on its
    own, INCLUDING Telegram DMs. Telegram is wired at the OpenClaw layer,
    so those turns only ever land in the transcripts, never in our DB.

Transcript parsing is deliberately defensive (mirrors usage/collector.py):
OpenClaw's transcript schema varies by build, so we hunt for any
message-shaped object rather than assuming one layout. Read-only.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import CONFIG_DIR, settings
from ..models import ChatMessage

OPENCLAW_HOME = Path(os.path.expanduser(settings.OPENCLAW_HOME))
_AGENTS_YAML = CONFIG_DIR / "agents.yaml"
_ROLES = {"user", "assistant", "system", "tool"}
_MAX_CHARS = 4000
_FILE_CAP = 8  # newest N transcript files per agent — keeps it snappy


def _agent_ids() -> dict[str, str]:
    try:
        data = yaml.safe_load(_AGENTS_YAML.read_text()) or {}
    except OSError:
        return {}
    return {a["key"]: a.get("agent_id", "")
            for a in data.get("agents", []) if a.get("agent_id")}


def _text_from_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict):
                parts.append(blk.get("text") or blk.get("content") or "")
            elif isinstance(blk, str):
                parts.append(blk)
        return "".join(p for p in parts if isinstance(p, str))
    return ""


def _norm_ts(ts, mtime: float) -> str:
    if ts is None:
        return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    if isinstance(ts, (int, float)):
        v = ts / 1000 if ts > 1e11 else ts
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    return str(ts)


def _iter_messages(obj, mtime: float):
    """Yield (role, text, iso_ts) from any message-shaped dict, recursively."""
    if isinstance(obj, dict):
        role = obj.get("role")
        if role in _ROLES and ("content" in obj or "text" in obj):
            text = _text_from_content(obj.get("content", obj.get("text", "")))
            if text and text.strip():
                ts = obj.get("ts") or obj.get("timestamp") or obj.get("created_at")
                yield role, text.strip()[:_MAX_CHARS], _norm_ts(ts, mtime)
        for v in obj.values():
            yield from _iter_messages(v, mtime)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_messages(v, mtime)


def _transcript_messages(agent_key: str, agent_id: str) -> list[dict]:
    sess_dir = OPENCLAW_HOME / "agents" / agent_id / "sessions"
    if not sess_dir.exists():
        return []
    files = [f for f in sess_dir.rglob("*") if f.is_file()]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    out: list[dict] = []
    for f in files[:_FILE_CAP]:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        mtime = f.stat().st_mtime
        parsed_any = False
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue
            parsed_any = True
            for role, content, ts in _iter_messages(doc, mtime):
                out.append({"agent": agent_key, "role": role, "content": content,
                            "at": ts, "source": "transcript", "session": f.stem})
        if not parsed_any:
            try:
                doc = json.loads(text)
            except json.JSONDecodeError:
                continue
            for role, content, ts in _iter_messages(doc, mtime):
                out.append({"agent": agent_key, "role": role, "content": content,
                            "at": ts, "source": "transcript", "session": f.stem})
    return out


async def history(db: AsyncSession, agent: str = "all", limit: int = 200,
                  source: str = "all") -> list[dict]:
    items: list[dict] = []

    if source in ("all", "dashboard"):
        q = select(ChatMessage)
        if agent and agent != "all":
            q = q.where(ChatMessage.agent_key == agent)
        q = q.order_by(ChatMessage.created_at.desc()).limit(limit)
        for m in (await db.execute(q)).scalars().all():
            items.append({
                "agent": m.agent_key, "role": m.role, "content": m.content,
                "at": m.created_at.isoformat() if m.created_at else None,
                "source": "dashboard", "session": None,
            })

    if source in ("all", "transcript"):
        ids = _agent_ids()
        targets = ({agent: ids.get(agent, "")}
                   if agent and agent != "all" else ids)
        for key, aid in targets.items():
            if aid:
                items.extend(_transcript_messages(key, aid))

    items = [i for i in items if i.get("at")]
    items.sort(key=lambda i: i["at"], reverse=True)
    return items[:limit]
