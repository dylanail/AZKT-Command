"""Versioned buyer requirements and deterministic candidate evaluation (spec §8.1–8.2, G01).

A requirement is `{key, tier: must|prefer|avoid, text, field?, op?, value?}`. `field/op/value` say how
the requirement is checked against a candidate's `specs` dict. Without a check, the outcome is
`unknown` (never guessed from free text). Outcomes carry evidence: the field looked at, what was
observed and where it came from.

Rules enforced here (invariants, not prompts):
- any `must` that fails  -> mandatory_fail  (the match is rejected regardless of score)
- any `must` that is unknown -> mandatory_unknown (bid readiness blocked; needs confirmation)
- score ranks only among candidates without a mandatory fail; it never overrides a gate
- changing a must-have is a revision that needs buyer evidence (checked by the sourcing commands)
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from ..core.errors import ValidationFailed

TIERS = ("must", "prefer", "avoid")
OPS = ("eq", "ne", "in", "not_in", "gte", "lte", "gt", "lt", "contains", "truthy", "falsy")
RESULTS = ("pass", "fail", "unknown")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s[:48] or "requirement"


def normalize_requirements(items: list[dict] | None) -> list[dict]:
    """Validate and canonicalize a requirement list (stable keys, allowed tiers/ops)."""
    out: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(items or []):
        if not isinstance(raw, dict):
            raise ValidationFailed(f"requirement #{i + 1} must be an object")
        tier = str(raw.get("tier") or "must").lower()
        if tier not in TIERS:
            raise ValidationFailed(f"requirement #{i + 1}: tier must be one of {TIERS}")
        text = str(raw.get("text") or "").strip()
        key = str(raw.get("key") or _slug(text)).strip()
        if not key:
            raise ValidationFailed(f"requirement #{i + 1}: key or text required")
        if key in seen:
            raise ValidationFailed(f"duplicate requirement key {key!r}")
        seen.add(key)
        r: dict = {"key": key, "tier": tier, "text": text or key}
        field = raw.get("field")
        if field:
            op = str(raw.get("op") or "eq").lower()
            if op not in OPS:
                raise ValidationFailed(f"requirement {key!r}: op must be one of {OPS}")
            r.update({"field": str(field), "op": op})
            if op not in ("truthy", "falsy"):
                if "value" not in raw:
                    raise ValidationFailed(f"requirement {key!r}: value required for op {op}")
                r["value"] = raw["value"]
        for extra_key in ("note", "source_ref"):
            if raw.get(extra_key):
                r[extra_key] = raw[extra_key]
        out.append(r)
    return out


def must_set(requirements: list[dict]) -> dict[str, dict]:
    return {r["key"]: {k: r.get(k) for k in ("field", "op", "value", "text")} for r in requirements if r.get("tier") == "must"}


def must_changed(old: list[dict], new: list[dict]) -> bool:
    """True when the set of must-have requirements (or how one is checked) differs."""
    return must_set(old) != must_set(new)


def usable(requirements: list[dict]) -> tuple[bool, str | None]:
    """Active search needs at least one must-have with a real check (spec §8.1)."""
    musts = [r for r in requirements or [] if r.get("tier") == "must"]
    if not musts:
        return False, "No must-have requirements recorded"
    if not any(r.get("field") for r in musts):
        return False, "Must-have requirements have no checkable criteria (field/op/value)"
    return True, None


def _num(v):
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _norm(v):
    if isinstance(v, str):
        return v.strip().lower()
    if isinstance(v, bool):
        return v
    n = _num(v)
    return n if n is not None else v


def _compare(observed, op: str, expected) -> bool | None:
    """None = cannot decide (unknown)."""
    if op == "truthy":
        return bool(observed)
    if op == "falsy":
        return not bool(observed)
    o = _norm(observed)
    if op == "eq":
        return o == _norm(expected)
    if op == "ne":
        return o != _norm(expected)
    if op in ("in", "not_in"):
        opts = [_norm(x) for x in (expected if isinstance(expected, (list, tuple)) else [expected])]
        hit = o in opts
        return hit if op == "in" else not hit
    if op == "contains":
        if isinstance(observed, (list, tuple)):
            return _norm(expected) in [_norm(x) for x in observed]
        if isinstance(observed, str):
            return str(_norm(expected)) in o
        return None
    a, b = _num(observed), _num(expected)
    if a is None or b is None:
        return None
    return {"gte": a >= b, "lte": a <= b, "gt": a > b, "lt": a < b}[op]


def evaluate(specs: dict | None, requirements: list[dict], *, spec_sources: dict | None = None) -> list[dict]:
    """Per-requirement outcomes with evidence. `specs` is the candidate snapshot's structured spec dict.
    `spec_sources` optionally maps field -> where that spec value came from (snapshot / translation:rev)."""
    specs = specs or {}
    spec_sources = spec_sources or {}
    outcomes: list[dict] = []
    for r in requirements or []:
        key, tier = r["key"], r.get("tier", "must")
        field = r.get("field")
        ev: dict = {"field": field, "op": r.get("op"), "expected": r.get("value"), "observed": None, "source": None}
        if not field:
            outcomes.append({"key": key, "tier": tier, "result": "unknown", "text": r.get("text"),
                             "evidence": {**ev, "reason": "no checkable criterion"}})
            continue
        if field not in specs or specs.get(field) is None:
            outcomes.append({"key": key, "tier": tier, "result": "unknown", "text": r.get("text"),
                             "evidence": {**ev, "reason": "not in snapshot"}})
            continue
        observed = specs[field]
        ev["observed"] = observed
        ev["source"] = spec_sources.get(field, "snapshot")
        met = _compare(observed, r.get("op", "eq"), r.get("value"))
        if met is None:
            result = "unknown"
            ev["reason"] = "value not comparable"
        elif tier == "avoid":
            result = "fail" if met else "pass"   # the avoided condition being present is a fail
        else:
            result = "pass" if met else "fail"
        outcomes.append({"key": key, "tier": tier, "result": result, "text": r.get("text"), "evidence": ev})
    return outcomes


def summarize(outcomes: list[dict]) -> dict:
    """Gate flags + score. Score is None when a mandatory requirement fails (cannot rank / override)."""
    musts = [o for o in outcomes if o["tier"] == "must"]
    mandatory_fail = any(o["result"] == "fail" for o in musts)
    mandatory_unknown = any(o["result"] == "unknown" for o in musts)
    prefs = [o for o in outcomes if o["tier"] in ("prefer", "avoid")]
    known = [o for o in prefs if o["result"] != "unknown"]
    preference_score = (round(100 * sum(1 for o in known if o["result"] == "pass") / len(known)) if known else None)
    return {
        "mandatory_fail": mandatory_fail,
        "mandatory_unknown": mandatory_unknown,
        "bid_ready": not mandatory_fail and not mandatory_unknown,
        "score": None if mandatory_fail else preference_score,
        "preference_score": preference_score,
        "needs_confirmation": [o["key"] for o in musts if o["result"] == "unknown"],
        "failed_must": [o["key"] for o in musts if o["result"] == "fail"],
        "counts": {r: sum(1 for o in outcomes if o["result"] == r) for r in RESULTS},
    }


def rank(matches: list[dict]) -> list[dict]:
    """Order match summaries: never a mandatory-fail; unknowns after fully known; then score desc."""
    eligible = [m for m in matches if not m.get("mandatory_fail")]
    # a recorded score of 0 is a real score and ranks above "no score recorded" (which stays last)
    return sorted(eligible, key=lambda m: (m.get("mandatory_unknown", False),
                                           -(m["score"] if m.get("score") is not None else -1)))


def tiers(requirements: list[dict]) -> dict:
    return {t: [r for r in requirements or [] if r.get("tier") == t] for t in TIERS}
