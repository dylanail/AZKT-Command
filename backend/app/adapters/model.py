"""Model adapter (Anthropic SDK). Deterministic code owns permissions, money and timers;
the model interprets, extracts, plans and writes (spec §10.7).

- Budget gate: discretionary model work stops when the configured daily/monthly USD cap is reached
  or when no cap is configured at all (MODEL_DAILY_BUDGET_USD=0) unless `essential=True`.
- Every call records a ModelUsage row (run/mission/workflow).
- `refusal` stop reason is surfaced as ModelRefused; unavailability as ModelUnavailable.
- Structured extraction uses `output_config.format` (JSON schema) and validates with pydantic.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select

from ..core.config import settings
from ..models.runtime import ModelUsage

log = logging.getLogger("azkt.model")

PRICES_PER_MTOK = {  # USD, from the Claude API reference cached 2026-06
    "claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0), "claude-opus-4-8": (5.0, 25.0),
}


class ModelUnavailable(Exception):
    """No key / budget exhausted / provider down. Callers keep deterministic paths working."""


class ModelRefused(Exception):
    def __init__(self, category: str | None, explanation: str | None):
        super().__init__(explanation or "model refused")
        self.category = category
        self.explanation = explanation


@dataclass
class ModelResult:
    text: str
    data: Any
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    stop_reason: str
    raw: Any = None


def cost_for(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    i, o = PRICES_PER_MTOK.get(model, (10.0, 50.0))
    return (Decimal(input_tokens) * Decimal(str(i)) + Decimal(output_tokens) * Decimal(str(o))) / Decimal(1_000_000)


async def spend(db, since: datetime) -> Decimal:
    v = await db.scalar(select(func.coalesce(func.sum(ModelUsage.cost_usd), 0)).where(ModelUsage.at >= since))
    return Decimal(v or 0)


async def budget_state(db) -> dict:
    now = datetime.now(timezone.utc)
    day = await spend(db, now - timedelta(days=1))
    month = await spend(db, now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    daily_cap = Decimal(str(settings.MODEL_DAILY_BUDGET_USD or 0))
    monthly_cap = Decimal(str(settings.MODEL_MONTHLY_BUDGET_USD or 0))
    configured = daily_cap > 0 or monthly_cap > 0
    over = (daily_cap > 0 and day >= daily_cap) or (monthly_cap > 0 and month >= monthly_cap)
    return {"configured": configured, "over_cap": over, "day_usd": str(day), "month_usd": str(month),
            "daily_cap_usd": str(daily_cap), "monthly_cap_usd": str(monthly_cap),
            "api_key_present": bool(settings.ANTHROPIC_API_KEY or _env_key())}


def _env_key() -> str:
    import os
    return os.environ.get("ANTHROPIC_API_KEY", "") or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")


def available() -> bool:
    return bool(settings.ANTHROPIC_API_KEY or _env_key())


class ModelClient:
    """Thin async wrapper over anthropic.AsyncAnthropic with budget + usage accounting."""

    def __init__(self, db, *, workflow: str = "chat", run_id: str | None = None, mission_id: str | None = None,
                 essential: bool = False):
        self.db = db
        self.workflow = workflow
        self.run_id = run_id
        self.mission_id = mission_id
        self.essential = essential
        self._client = None

    async def _ensure(self):
        if not available():
            raise ModelUnavailable("no ANTHROPIC_API_KEY configured")
        st = await budget_state(self.db)
        if not self.essential:
            if not st["configured"]:
                raise ModelUnavailable("model budget not configured (MODEL_DAILY_BUDGET_USD); discretionary AI work is off")
            if st["over_cap"]:
                raise ModelUnavailable("model budget cap reached; deterministic operations continue")
        if self._client is None:
            import anthropic
            kwargs = {}
            if settings.ANTHROPIC_API_KEY:
                kwargs["api_key"] = settings.ANTHROPIC_API_KEY
            self._client = anthropic.AsyncAnthropic(timeout=float(settings.MODEL_RUN_TIMEOUT_SECONDS), **kwargs)
        return self._client

    async def _record(self, model: str, usage, duration_ms: int) -> Decimal:
        it = int(getattr(usage, "input_tokens", 0) or 0)
        ot = int(getattr(usage, "output_tokens", 0) or 0)
        cost = cost_for(model, it, ot)
        self.db.add(ModelUsage(run_id=self.run_id, mission_id=self.mission_id, workflow=self.workflow, model=model,
                               input_tokens=it, output_tokens=ot,
                               cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
                               cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
                               cost_usd=cost, duration_ms=duration_ms, at=datetime.now(timezone.utc)))
        await self.db.flush()
        return cost

    @staticmethod
    def image_block(data: bytes, media_type: str) -> dict:
        return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                            "data": base64.standard_b64encode(data).decode()}}

    async def complete(self, *, system: str, messages: list[dict], model: str | None = None, max_tokens: int = 16000,
                       effort: str | None = None, tools: list[dict] | None = None, output_schema: dict | None = None,
                       tool_choice: dict | None = None) -> ModelResult:
        import anthropic
        client = await self._ensure()
        model = model or settings.AZKT_MODEL
        kwargs: dict[str, Any] = dict(model=model, max_tokens=max_tokens, system=system, messages=messages,
                                      thinking={"type": "adaptive"}, output_config={"effort": effort or settings.AZKT_MODEL_EFFORT})
        if tools:
            kwargs["tools"] = tools
            if tool_choice and not model.startswith("claude-fable"):
                kwargs["tool_choice"] = tool_choice
        if output_schema:
            kwargs["output_config"]["format"] = {"type": "json_schema", "schema": output_schema}
        t0 = time.monotonic()
        try:
            resp = await client.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise ModelUnavailable(f"authentication failed: {e.message}") from e
        except anthropic.RateLimitError as e:
            raise ModelUnavailable(f"rate limited: {e.message}") from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise ModelUnavailable(f"provider error {e.status_code}") from e
            raise
        except anthropic.APIConnectionError as e:
            raise ModelUnavailable(f"connection error: {e}") from e
        cost = await self._record(resp.model, resp.usage, int((time.monotonic() - t0) * 1000))
        if resp.stop_reason == "refusal":
            sd = getattr(resp, "stop_details", None)
            raise ModelRefused(getattr(sd, "category", None), getattr(sd, "explanation", None))
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        data = None
        if output_schema and text:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
        return ModelResult(text=text, data=data, model=resp.model, input_tokens=resp.usage.input_tokens,
                           output_tokens=resp.usage.output_tokens, cost_usd=cost, stop_reason=resp.stop_reason, raw=resp)

    async def extract(self, schema_model: type[BaseModel], *, system: str, user_content: list | str,
                      model: str | None = None, effort: str = "medium") -> BaseModel:
        """Structured extraction validated against a pydantic model."""
        schema = schema_model.model_json_schema()
        schema = _strict_schema(schema)
        res = await self.complete(system=system, messages=[{"role": "user", "content": user_content}],
                                  model=model or settings.AZKT_MODEL_LIGHT, effort=effort, output_schema=schema,
                                  max_tokens=8000)
        if res.data is None:
            raise ValueError("model returned no structured output")
        return schema_model.model_validate(res.data)


def _strict_schema(schema: dict) -> dict:
    """Inline $defs and forbid additional properties (structured outputs requirement)."""
    defs = schema.pop("$defs", {}) or {}

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"].split("/")[-1]
                return walk(json.loads(json.dumps(defs.get(ref, {}))))
            if node.get("type") == "object":
                node["additionalProperties"] = False
                props = node.get("properties", {})
                node["required"] = list(props.keys())
                for k, v in props.items():
                    props[k] = walk(v)
            for key in ("items", "anyOf", "oneOf", "allOf"):
                if key in node:
                    node[key] = walk(node[key]) if isinstance(node[key], dict) else [walk(x) for x in node[key]]
            node.pop("title", None)
            node.pop("default", None)
            return node
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node
    return walk(schema)


# ── Transcription ────────────────────────────────────────────────────────────
class TranscriptionUnavailable(Exception):
    pass


async def transcribe(audio: bytes, content_type: str) -> str:
    """Server-side speech-to-text. The Claude Messages API has no audio input, so this delegates to an
    owner-configured transcription service (TRANSCRIBE_URL, e.g. a self-hosted whisper endpoint). When none
    is configured the caller keeps the audio, shows 'Transcript needed' and offers typed entry (spec §7.4)."""
    import os
    url = os.environ.get("TRANSCRIBE_URL", "")
    if not url:
        raise TranscriptionUnavailable("no transcription service configured (TRANSCRIBE_URL)")
    import httpx
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(url, files={"file": ("audio", audio, content_type)},
                         headers={"Authorization": f"Bearer {os.environ.get('TRANSCRIBE_TOKEN', '')}"} if os.environ.get("TRANSCRIBE_TOKEN") else {})
        if r.status_code != 200:
            raise TranscriptionUnavailable(f"transcription service returned {r.status_code}")
        body = r.json()
        return body.get("text") or body.get("transcript") or ""
