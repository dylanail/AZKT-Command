"""Auction candidate sources (spec §8.2 step 1, §12.3). Every source returns the same normalized
candidate shape; an unconfigured live source returns `unsupported` with a typed reason, never an
empty "success" (S12).

Normalized candidate:
    {provider, auction_house, lot_no, auction_at (ISO, tz-aware), deadline_at (ISO|None), deadline_source,
     source_url, title, frame_raw, specs: {transmission, ac, drive, year, mileage_km, ...},
     snapshot: {raw provider fields}, images: [urls]}
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..core.config import settings
from ..core.errors import ValidationFailed
from ..core.time import ensure_aware, parse_iso

SPEC_FIELDS = ("transmission", "ac", "drive", "year", "mileage_km", "grade", "color", "engine_cc", "body",
               "inspection", "bed_type", "seats", "model", "make")


@dataclass
class FetchResult:
    status: str                       # ok | unsupported | error
    candidates: list[dict] = field(default_factory=list)
    reason: dict = field(default_factory=dict)   # {kind: not_configured|auth_expired|schema_changed|transient, detail}
    source: str = ""
    fetched_at: str | None = None

    def to_dict(self) -> dict:
        return {"status": self.status, "count": len(self.candidates), "reason": self.reason, "source": self.source,
                "fetched_at": self.fetched_at}


def _iso(v) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return ensure_aware(v).isoformat()
    try:
        return parse_iso(str(v)).isoformat()
    except ValueError:
        raise ValidationFailed(f"invalid datetime in candidate feed: {v!r}")


def normalize(raw: dict, *, provider: str) -> dict:
    """Map a raw provider record to the normalized shape. Identity fields are required."""
    if not isinstance(raw, dict):
        raise ValidationFailed("candidate must be an object")
    lot = str(raw.get("lot_no") or raw.get("lot") or "").strip()
    house = str(raw.get("auction_house") or raw.get("house") or "").strip()
    auction_at = _iso(raw.get("auction_at") or raw.get("auction_date"))
    if not lot or not house or not auction_at:
        raise ValidationFailed("candidate identity needs auction_house, lot_no and auction_at",
                               lot_no=lot or None, auction_house=house or None)
    specs_in = raw.get("specs") if isinstance(raw.get("specs"), dict) else {}
    specs = {k: v for k, v in specs_in.items() if v is not None}
    for k in SPEC_FIELDS:                       # top-level convenience keys become specs too
        if k in raw and raw[k] is not None and k not in specs:
            specs[k] = raw[k]
    deadline = _iso(raw.get("deadline_at") or raw.get("bid_deadline"))
    return {
        "provider": provider,
        "auction_house": house,
        "lot_no": lot,
        "auction_at": auction_at,
        "auction_at_source": raw.get("auction_at_source") or f"{provider}:{house}",
        "deadline_at": deadline,
        "deadline_source": (raw.get("deadline_source") or (f"{provider}:{house}" if deadline else None)),
        "source_url": raw.get("source_url") or raw.get("url"),
        "title": str(raw.get("title") or f"{house} lot {lot}"),
        "frame_raw": raw.get("frame_raw") or raw.get("frame_no") or raw.get("chassis"),
        "specs": specs,
        "snapshot": dict(raw.get("snapshot") or {k: v for k, v in raw.items() if k not in ("specs", "images")}),
        "images": list(raw.get("images") or []),
    }


class AuctionSource:
    name = "abstract"

    async def fetch(self, since: datetime | None = None) -> FetchResult:  # pragma: no cover - interface
        raise NotImplementedError


class FixtureAuctionSource(AuctionSource):
    """Reads JSON fixtures (tests / offline replay). Path must be a list of raw records or {"candidates": [...]}."""
    name = "fixture"

    def __init__(self, path: str | os.PathLike | None = None, items: list[dict] | None = None, provider: str = "fixture"):
        self.path = Path(path) if path else None
        self.items = items
        self.provider = provider

    async def fetch(self, since: datetime | None = None) -> FetchResult:
        items = self.items
        if items is None:
            if self.path is None or not self.path.exists():
                return FetchResult("error", reason={"kind": "invalid_input", "detail": f"fixture not found: {self.path}"}, source=self.name)
            data = json.loads(self.path.read_text())
            items = data.get("candidates", []) if isinstance(data, dict) else data
        out = []
        for raw in items:
            n = normalize(raw, provider=self.provider)
            if since is not None and parse_iso(n["auction_at"]) < ensure_aware(since):
                continue
            out.append(n)
        return FetchResult("ok", out, source=self.name, fetched_at=datetime.now(timezone.utc).isoformat())


class HttpAuctionSource(AuctionSource):
    """Live source stub. Returns `unsupported` with a typed reason until AUCTION_SOURCE_URL and credentials
    are configured (S12). No request is made without configuration; nothing is fabricated."""
    name = "http"

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = base_url or getattr(settings, "AUCTION_SOURCE_URL", "") or os.environ.get("AUCTION_SOURCE_URL", "")
        self.api_key = api_key or getattr(settings, "AUCTION_SOURCE_KEY", "") or os.environ.get("AUCTION_SOURCE_KEY", "")

    def configured(self) -> tuple[bool, str | None]:
        if not self.base_url:
            return False, "AUCTION_SOURCE_URL is not configured"
        if not self.api_key:
            return False, "AUCTION_SOURCE_KEY is not configured"
        return True, None

    async def fetch(self, since: datetime | None = None) -> FetchResult:
        ok, why = self.configured()
        if not ok:
            return FetchResult("unsupported", reason={"kind": "not_configured", "detail": why}, source=self.name)
        # Provider contract (schema, auth, pagination) is confirmed in stage 2; until then this is unsupported.
        return FetchResult("unsupported", reason={"kind": "not_implemented",
                                                  "detail": "live auction feed contract not verified yet"}, source=self.name)


def source_for(spec: dict | None) -> AuctionSource:
    spec = spec or {}
    kind = spec.get("kind", "http")
    if kind == "fixture":
        return FixtureAuctionSource(path=spec.get("path"), items=spec.get("items"), provider=spec.get("provider", "fixture"))
    if kind == "http":
        return HttpAuctionSource(spec.get("base_url"), spec.get("api_key"))
    raise ValidationFailed(f"unknown auction source kind {kind!r}")
