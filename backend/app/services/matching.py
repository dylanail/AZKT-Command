"""Shared matching service (spec §3.3). Used by Inbox, Square, ledger, Drive, candidates and shipping.

Results are `matched | proposed | ambiguous | unmatched` with candidates, reasons and evidence.
Automatic `matched` requires an unambiguous authorized rule:
  - exact provider reference (identity kind "provider", e.g. "square:customer:abc"),
  - an established conversation mapping whose new participants are consistent,
  - a unique *verified* identity alias (email/phone/handle),
  - a unique exact stock/frame reference with corroboration (or an explicit reference).
Name/company/subject/amount/model/year/color similarity only RANKS candidates.

Contact matching and business-record (vehicle) matching are separate results.
Pure async functions over the db; no writes. Untrusted text is data: extraction never changes scope.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.contacts import Contact, ContactIdentity
from ..models.vehicles import Vehicle

MATCH_STATES = ("matched", "proposed", "ambiguous", "unmatched")

# ── normalization ────────────────────────────────────────────────────────────
COUNTRY_CODES = {"US": "1", "CA": "1", "JP": "81", "GB": "44", "MX": "52", "AU": "61", "DE": "49", "FR": "33"}
TRUNK_PREFIX = {"JP": "0", "GB": "0", "AU": "0", "DE": "0", "FR": "0"}  # national trunk digit dropped in E.164


def normalize_email(raw: str | None) -> str | None:
    """Lowercase only. Dots and plus aliases are preserved (spec §3.3: do not merge unrelated people)."""
    if not raw:
        return None
    v = raw.strip().strip("<>").strip().lower()
    if "@" not in v or v.startswith("@") or v.endswith("@"):
        return None
    return v


def normalize_phone(raw: str | None, country_default: str = "US") -> str | None:
    """E.164 with a default country. Raw is kept by the caller. Returns None if not a plausible number."""
    if not raw:
        return None
    s = raw.strip()
    # strip extensions
    s = re.split(r"(?i)\b(?:ext|x|extension)\b", s)[0]
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if plus:
        e164 = digits
    elif digits.startswith("00") and len(digits) > 10:
        e164 = digits[2:]
    elif digits.startswith("011") and len(digits) > 11 and (country_default or "US").upper() in ("US", "CA"):
        e164 = digits[3:]
    else:
        cc = COUNTRY_CODES.get((country_default or "US").upper(), "1")
        trunk = TRUNK_PREFIX.get((country_default or "US").upper(), "")
        if cc == "1":
            if len(digits) == 11 and digits.startswith("1"):
                e164 = digits
            elif len(digits) == 10:
                e164 = "1" + digits
            else:
                return None
        else:
            national = digits[1:] if trunk and digits.startswith(trunk) else digits
            if digits.startswith(cc) and len(digits) >= 10 + len(cc) - 1 and not digits.startswith(trunk or "!"):
                e164 = digits
            else:
                e164 = cc + national
    if len(e164) < 8 or len(e164) > 15:
        return None
    return "+" + e164


def normalize_frame(raw: str | None) -> str | None:
    """Uppercase; strip spaces/punctuation only. Leading zeros and letters are kept. Raw is stored separately."""
    if not raw:
        return None
    v = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    return v or None


def normalize_stock_no(raw: str | None) -> str | None:
    if not raw:
        return None
    v = re.sub(r"[\s_]", "", raw).upper()
    m = re.fullmatch(r"(STK)-?(\d{2,8})", v)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return v or None


def normalize_handle(raw: str | None) -> str | None:
    if not raw:
        return None
    v = raw.strip().lstrip("@").lower()
    return v or None


def normalize_identity(kind: str, value: str, country_default: str = "US") -> str | None:
    if kind == "email":
        return normalize_email(value)
    if kind == "phone":
        return normalize_phone(value, country_default)
    if kind in ("telegram", "instagram", "handle"):
        return normalize_handle(value)
    if kind == "provider":
        return (value or "").strip().lower() or None
    return (value or "").strip().lower() or None


def normalize_name(raw: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (raw or "").lower())).strip()


# ── message-level extraction (B05) ──────────────────────────────────────────
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_STOCK_RE = re.compile(r"\bSTK[-\s]?\d{3,8}\b", re.IGNORECASE)
_FRAME_RE = re.compile(r"\b[A-Z]{1,3}\d{1,3}[A-Z]{0,2}-\d{5,7}\b")
_INVOICE_RE = re.compile(
    r"(?i)\b(?:invoice|inv|請求書)\s*(?:no\.?|number|#|№|:)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]{2,})")
_INVOICE_BARE_RE = re.compile(r"\bINV-?\d[A-Z0-9\-]*\b")
_AMOUNT_RE = re.compile(
    r"(?:(?P<cur1>USD|US\$|\$|JPY|¥|￥|EUR|€)\s?(?P<amt1>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?))"
    r"|(?:(?P<amt2>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s?(?P<cur2>USD|JPY|yen|円|dollars|EUR))",
    re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<![\w-])\+?\(?\d[\d\s().\-]{5,18}\d(?![\w-])")
_CURRENCY_ALIASES = {"$": "USD", "us$": "USD", "usd": "USD", "dollars": "USD", "¥": "JPY", "￥": "JPY",
                     "jpy": "JPY", "yen": "JPY", "円": "JPY", "€": "EUR", "eur": "EUR"}


def _overlaps(span: tuple[int, int], taken: list[tuple[int, int]]) -> bool:
    a, b = span
    return any(a < d and c < b for c, d in taken)


def extract_items(text: str | None) -> list[dict]:
    """Deterministic regex extraction of separately linkable items from a message.

    Returns [{kind: stock_no|frame_no|invoice_no|amount|email|phone, value, norm, span:[start,end]}]
    ordered by position. A supplier email listing three vehicles yields three separate stock/frame items.
    """
    if not text:
        return []
    items: list[dict] = []
    taken: list[tuple[int, int]] = []

    def add(kind: str, m_start: int, m_end: int, value: str, norm) -> None:
        span = (m_start, m_end)
        if _overlaps(span, taken):
            return
        taken.append(span)
        items.append({"kind": kind, "value": value, "norm": norm, "span": [m_start, m_end]})

    for m in _EMAIL_RE.finditer(text):
        add("email", m.start(), m.end(), m.group(0), normalize_email(m.group(0)))
    for m in _STOCK_RE.finditer(text):
        add("stock_no", m.start(), m.end(), m.group(0), normalize_stock_no(m.group(0)))
    for m in _FRAME_RE.finditer(text):
        add("frame_no", m.start(), m.end(), m.group(0), normalize_frame(m.group(0)))
    for m in _INVOICE_RE.finditer(text):
        add("invoice_no", m.start(1), m.end(1), m.group(1), m.group(1).upper())
    for m in _INVOICE_BARE_RE.finditer(text):
        add("invoice_no", m.start(), m.end(), m.group(0), m.group(0).upper())
    for m in _AMOUNT_RE.finditer(text):
        cur = (m.group("cur1") or m.group("cur2") or "").lower()
        amt = m.group("amt1") or m.group("amt2")
        try:
            dec = Decimal(amt.replace(",", ""))
        except (InvalidOperation, AttributeError):
            continue
        add("amount", m.start(), m.end(), m.group(0), {"amount": str(dec), "currency": _CURRENCY_ALIASES.get(cur, cur.upper())})
    for m in _PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) < 7 or len(digits) > 15:
            continue
        add("phone", m.start(), m.end(), m.group(0).strip(), normalize_phone(m.group(0)))
    items.sort(key=lambda i: i["span"][0])
    return items


# ── results ──────────────────────────────────────────────────────────────────
@dataclass
class MatchResult:
    state: str                                   # matched | proposed | ambiguous | unmatched
    entity_kind: str = "contact"                 # contact | vehicle
    entity_id: str | None = None
    candidates: list[dict] = field(default_factory=list)   # [{contact_id|vehicle_id, score, reasons}]
    reasons: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    @property
    def contact_id(self) -> str | None:
        return self.entity_id if self.entity_kind == "contact" else None

    @property
    def vehicle_id(self) -> str | None:
        return self.entity_id if self.entity_kind == "vehicle" else None

    def to_dict(self) -> dict:
        d = {"state": self.state, "entity_kind": self.entity_kind, "candidates": self.candidates,
             "reasons": self.reasons, "evidence": self.evidence}
        d[f"{self.entity_kind}_id"] = self.entity_id
        return d


class _Cands:
    """Accumulates per-entity scores/reasons; `authorized` marks rules that may auto-link."""

    def __init__(self, key: str):
        self.key = key
        self.rows: dict[str, dict] = {}
        self.authorized: dict[str, list[str]] = {}

    def add(self, eid: str, score: float, reason: str, *, authorized: bool = False) -> None:
        r = self.rows.setdefault(eid, {self.key: eid, "score": 0.0, "reasons": []})
        r["score"] = round(min(1.0, r["score"] + score), 3)
        if reason not in r["reasons"]:
            r["reasons"].append(reason)
        if authorized:
            self.authorized.setdefault(eid, []).append(reason)

    def ranked(self) -> list[dict]:
        return sorted(self.rows.values(), key=lambda r: (-r["score"], r[self.key]))


def _decide(c: _Cands, reasons: list[str], evidence: dict, kind: str) -> MatchResult:
    ranked = c.ranked()
    if not ranked:
        return MatchResult("unmatched", kind, None, [], reasons + ["no candidate"], evidence)
    if c.authorized:
        if len(c.authorized) == 1:
            eid = next(iter(c.authorized))
            return MatchResult("matched", kind, eid, ranked, reasons + c.authorized[eid], evidence)
        return MatchResult("ambiguous", kind, None, ranked,
                           reasons + [f"{len(c.authorized)} {kind}s satisfy an authorized rule"], evidence)
    top = ranked[0]["score"]
    ties = [r for r in ranked if r["score"] == top]
    if len(ties) > 1:
        return MatchResult("ambiguous", kind, None, ranked, reasons + [f"{len(ties)} {kind}s rank equally"], evidence)
    return MatchResult("proposed", kind, ranked[0][c.key], ranked, reasons + ["ranked by similarity only; review before linking"], evidence)


async def _live_contact_ids(db: AsyncSession, ids: Iterable[str]) -> dict[str, str]:
    """Map candidate contact ids to their surviving id (follows merged_into); drops archived."""
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    rows = (await db.execute(select(Contact.id, Contact.status, Contact.merged_into_id).where(Contact.id.in_(ids)))).all()
    out: dict[str, str] = {}
    for cid, status, into in rows:
        if status == "merged" and into:
            out[cid] = into
        elif status != "archived":
            out[cid] = cid
    return out


async def _identity_hits(db: AsyncSession, kind: str, norm: str) -> list[ContactIdentity]:
    rows = (await db.execute(select(ContactIdentity).where(ContactIdentity.kind == kind,
                                                            ContactIdentity.value_norm == norm))).scalars().all()
    return list(rows)


async def resolve_contact(db: AsyncSession, *, email: str | None = None, phone: str | None = None,
                          name: str | None = None, provider_ref: str | dict | None = None,
                          thread_mapping: str | dict | None = None, company: str | None = None,
                          handle: str | None = None, country_default: str = "US") -> MatchResult:
    """Resolve a person from message/provider evidence. Never writes."""
    reasons: list[str] = []
    ev: dict = {}
    c = _Cands("contact_id")

    # 1. exact provider reference
    if provider_ref:
        ref = provider_ref if isinstance(provider_ref, str) else ":".join(
            str(provider_ref.get(k)) for k in ("provider", "kind", "id") if provider_ref.get(k))
        norm = normalize_identity("provider", ref)
        ev["provider_ref"] = norm
        hits = await _identity_hits(db, "provider", norm) if norm else []
        live = await _live_contact_ids(db, [h.contact_id for h in hits])
        for cid in set(live.values()):
            c.add(cid, 1.0, "exact provider reference", authorized=True)

    # 2. established conversation mapping with consistent participants
    if thread_mapping:
        mapped_contact, participants = None, []
        if isinstance(thread_mapping, dict):
            mapped_contact = thread_mapping.get("contact_id")
            participants = [normalize_email(p) for p in (thread_mapping.get("participants") or []) if p]
            state = thread_mapping.get("contact_match", "matched")
        else:
            from ..models.comms import Conversation
            conv = await db.get(Conversation, thread_mapping)
            if conv is not None:
                mapped_contact, state = conv.contact_id, conv.contact_match
                participants = [normalize_email(p if isinstance(p, str) else (p or {}).get("email"))
                                for p in (conv.participants or [])]
                participants = [p for p in participants if p]
            else:
                state = "unmatched"
        if mapped_contact and state == "matched":
            live = await _live_contact_ids(db, [mapped_contact])
            cid = live.get(mapped_contact)
            e = normalize_email(email)
            if cid and (not e or not participants or e in participants):
                c.add(cid, 0.95, "established conversation mapping", authorized=True)
                ev["thread_mapping"] = "consistent"
            elif cid:
                c.add(cid, 0.5, "conversation mapping but new participant address")
                ev["thread_mapping"] = "new_participant"
                reasons.append("new participant not in the established mapping")

    # 3. identity aliases
    for kind, raw in (("email", email), ("phone", phone), ("telegram", handle)):
        if not raw:
            continue
        norm = normalize_identity(kind, raw, country_default)
        ev[kind] = norm
        if not norm:
            reasons.append(f"{kind} could not be normalized")
            continue
        hits = await _identity_hits(db, kind, norm)
        live = await _live_contact_ids(db, [h.contact_id for h in hits])
        by_contact: dict[str, bool] = {}
        for h in hits:
            cid = live.get(h.contact_id)
            if cid:
                by_contact[cid] = by_contact.get(cid, False) or bool(h.verified)
        if len(by_contact) == 1:
            cid, verified = next(iter(by_contact.items()))
            if verified:
                c.add(cid, 0.9, f"unique verified {kind} alias", authorized=True)
            else:
                c.add(cid, 0.7, f"unique unverified {kind} alias")
                reasons.append(f"{kind} alias is not verified")
        elif len(by_contact) > 1:
            for cid in by_contact:
                c.add(cid, 0.5, f"{kind} alias shared by {len(by_contact)} contacts")
            reasons.append(f"{kind} alias belongs to {len(by_contact)} contacts")

    # 4. name / company: ranking only
    nname = normalize_name(name)
    if nname:
        ev["name"] = nname
        pattern = f"%{nname}%"
        rows = (await db.execute(select(Contact).where(
            Contact.status.notin_(("merged", "archived")),
            or_(func.lower(Contact.name).like(pattern), Contact.search_text.ilike(pattern))).limit(25))).scalars().all()
        for r in rows:
            exact = normalize_name(r.name) == nname
            c.add(r.id, 0.4 if exact else 0.2, "exact name" if exact else "name similarity")
            if company and r.company and normalize_name(r.company) == normalize_name(company):
                c.add(r.id, 0.1, "company matches")
        if not rows:
            reasons.append("no contact with that name")

    # conflicting authorized rules pointing at different people -> ambiguous
    if len(c.authorized) > 1:
        reasons.append("authorized rules disagree")
    res = _decide(c, reasons, ev, "contact")
    return res


async def resolve_vehicle(db: AsyncSession, *, stock_no: str | None = None, frame_no: str | None = None,
                          text: str | None = None, corroboration: Iterable[str] = (),
                          model: str | None = None, model_year: int | None = None,
                          color: str | None = None) -> MatchResult:
    """Resolve a vehicle. Explicit stock/frame refs match when unique; refs found only in free text need
    corroboration (a second agreeing identifier, or a caller-supplied corroboration list)."""
    reasons: list[str] = []
    ev: dict = {"corroboration": list(corroboration)}
    c = _Cands("vehicle_id")
    corroborated = bool(list(corroboration))

    explicit: list[tuple[str, str]] = []
    if stock_no:
        explicit.append(("stock_no", normalize_stock_no(stock_no)))
    if frame_no:
        explicit.append(("frame_no", normalize_frame(frame_no)))
    extracted: list[tuple[str, str]] = []
    if text:
        for it in extract_items(text):
            if it["kind"] in ("stock_no", "frame_no") and it["norm"]:
                extracted.append((it["kind"], it["norm"]))
    ev["explicit"] = [v for _, v in explicit]
    ev["extracted"] = [v for _, v in extracted]

    async def lookup(kind: str, norm: str) -> list[Vehicle]:
        col = Vehicle.stock_no if kind == "stock_no" else Vehicle.frame_no_norm
        rows = (await db.execute(select(Vehicle).where(col == norm, Vehicle.archived_at.is_(None)))).scalars().all()
        return list(rows)

    hits_by_vehicle: dict[str, set[str]] = {}
    for kind, norm in explicit:
        rows = await lookup(kind, norm)
        if len(rows) == 1:
            c.add(rows[0].id, 1.0, f"explicit {kind} reference", authorized=True)
            hits_by_vehicle.setdefault(rows[0].id, set()).add(kind)
        elif len(rows) > 1:
            for r in rows:
                c.add(r.id, 0.6, f"{kind} not unique")
            reasons.append(f"{kind} {norm} is not unique")
        else:
            reasons.append(f"no vehicle with {kind} {norm}")
    for kind, norm in extracted:
        rows = await lookup(kind, norm)
        if len(rows) == 1:
            hits_by_vehicle.setdefault(rows[0].id, set()).add(f"text:{kind}")
            c.add(rows[0].id, 0.8, f"{kind} found in text")
        elif len(rows) > 1:
            reasons.append(f"{kind} {norm} in text is not unique")
    # corroboration: a text reference is authorized when a second identifier agrees or the caller corroborates
    for vid, kinds in hits_by_vehicle.items():
        text_kinds = {k for k in kinds if k.startswith("text:")}
        if text_kinds and (corroborated or len(kinds) >= 2):
            c.add(vid, 0.1, "text reference corroborated", authorized=True)
    if extracted and not corroborated and all(len(k) < 2 for k in hits_by_vehicle.values()):
        reasons.append("reference found in text only; corroboration required to auto-link")

    # ranking signals: model / year / color
    if model or model_year or color:
        q = select(Vehicle).where(Vehicle.archived_at.is_(None))
        if model:
            q = q.where(func.lower(Vehicle.model) == model.lower())
        if model_year:
            q = q.where(Vehicle.model_year == model_year)
        rows = (await db.execute(q.limit(50))).scalars().all()
        for r in rows:
            score, why = 0.0, []
            if model:
                score += 0.2
                why.append("model")
            if model_year:
                score += 0.1
                why.append("year")
            if color and r.color and r.color.lower() == color.lower():
                score += 0.1
                why.append("color")
            elif color:
                continue
            c.add(r.id, score, "similar " + "/".join(why))
        ev["similarity"] = {"model": model, "model_year": model_year, "color": color}
    return _decide(c, reasons, ev, "vehicle")
