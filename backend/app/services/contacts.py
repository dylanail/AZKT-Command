"""Contacts and identity commands (spec §3.1 Contact/identity, §3.3 Matching, §2.3 Contacts).

- Identities keep the raw value and a normalized form (matching.normalize_identity).
- Duplicate contacts merge only with review: agents/managers `contacts.merge_propose` (consequential,
  approval kind `contact_merge`); the owner runs `contacts.merge` directly. Merges are audited and
  undoable (`contacts.unmerge` restores from the ContactMerge snapshot). Aliases are kept, never lost.
- `search_text` is maintained on every write.
"""
from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy import select, update

from ..core.errors import Blocked, Conflict, NotFound, ValidationFailed
from ..domain.commands import CommandContext, command
from ..models.contacts import Contact, ContactIdentity, ContactMerge
from .matching import normalize_identity

CONTACT_ROLES = ("buyer", "vendor", "exporter", "importer", "carrier", "dispatcher", "port", "other")
IDENTITY_KINDS = ("email", "phone", "telegram", "instagram", "provider", "other")
CONTACT_STATUSES = ("active", "provisional", "merged", "archived")
TAB_ROLES = {"buyers": ("buyer",), "vendors": ("vendor",), "exporters": ("exporter", "importer"),
             "carriers": ("carrier", "dispatcher", "port")}

# Tables whose rows are relinked from the merged contact to the survivor (all restored on unmerge).
# (label, module path, model name, column)
RELINK_TARGETS = (
    ("opportunities", "..models.sales", "Opportunity", "contact_id"),
    ("tasks", "..models.tasks", "Task", "contact_id"),
    ("cases", "..models.tasks", "Case", "contact_id"),
    ("commitments", "..models.tasks", "Commitment", "contact_id"),
    ("conversations", "..models.comms", "Conversation", "contact_id"),
    ("vehicles", "..models.vehicles", "Vehicle", "buyer_contact_id"),
    ("import_requests", "..models.sourcing", "ImportRequest", "contact_id"),
    ("sales", "..models.finance", "Sale", "buyer_contact_id"),
    ("agreements", "..models.finance", "Agreement", "contact_id"),
    ("invoices", "..models.finance", "Invoice", "contact_id"),
    ("payments", "..models.finance", "Payment", "contact_id"),
)


# ── serializers ──────────────────────────────────────────────────────────────
def serialize_identity(i: ContactIdentity) -> dict:
    return {"id": i.id, "contact_id": i.contact_id, "kind": i.kind, "value": i.value_raw, "value_norm": i.value_norm,
            "verified": bool(i.verified), "is_primary": bool(i.is_primary), "source": i.source, "label": i.label,
            "country": i.country, "verified_by": i.verified_by,
            "verified_at": i.verified_at.isoformat() if i.verified_at else None}


def serialize_contact(c: Contact, identities: list[ContactIdentity] | None = None) -> dict:
    d = {"id": c.id, "version": c.version, "name": c.name, "company": c.company, "roles": list(c.roles or []),
         "primary_email": c.primary_email, "primary_phone": c.primary_phone, "status": c.status,
         "verified": bool(c.verified), "consent": dict(c.consent or {}), "source": c.source, "source_ref": c.source_ref,
         "notes": c.notes, "merged_into_id": c.merged_into_id, "aliases": list(c.aliases or []),
         "provisional_reason": c.provisional_reason, "extra": dict(c.extra or {}),
         "archived_at": c.archived_at.isoformat() if c.archived_at else None,
         "merged_at": c.merged_at.isoformat() if c.merged_at else None,
         "created_at": c.created_at.isoformat() if c.created_at else None,
         "updated_at": c.updated_at.isoformat() if c.updated_at else None,
         "legacy_customer_id": c.legacy_customer_id}
    if identities is not None:
        d["identities"] = [serialize_identity(i) for i in identities]
    return d


def serialize_merge(m: ContactMerge) -> dict:
    return {"id": m.id, "survivor_id": m.survivor_id, "merged_id": m.merged_id, "reason": m.reason,
            "merged_by": m.merged_by, "approval_id": m.approval_id, "snapshot": m.snapshot,
            "reverted_at": m.reverted_at.isoformat() if m.reverted_at else None, "reverted_by": m.reverted_by,
            "created_at": m.created_at.isoformat() if m.created_at else None}


def build_search_text(c: Contact, identities: list[ContactIdentity]) -> str:
    parts: list[str] = [c.name or "", c.company or ""]
    parts += list(c.roles or [])
    for a in c.aliases or []:
        parts += [str(a.get("name") or ""), str(a.get("company") or "")]
    for i in identities:
        parts += [i.value_raw or "", i.value_norm or ""]
    if c.primary_email:
        parts.append(c.primary_email)
    if c.primary_phone:
        parts.append(c.primary_phone)
    return " | ".join(p.strip().lower() for p in parts if p and p.strip())


# ── helpers ──────────────────────────────────────────────────────────────────
async def _get(ctx: CommandContext, contact_id: str, expected_version: int | None = None) -> Contact:
    c = (await ctx.db.execute(select(Contact).where(Contact.id == contact_id).with_for_update())).scalar_one_or_none()
    if c is None:
        raise NotFound("contact not found")
    if expected_version is not None and c.version != expected_version:
        raise Conflict("contact changed since you loaded it", current_version=c.version)
    return c


async def identities_of(db, contact_id: str) -> list[ContactIdentity]:
    rows = (await db.execute(select(ContactIdentity).where(ContactIdentity.contact_id == contact_id)
                             .order_by(ContactIdentity.is_primary.desc(), ContactIdentity.created_at))).scalars().all()
    return list(rows)


async def _refresh(ctx: CommandContext, c: Contact, identities: list[ContactIdentity] | None = None) -> list[ContactIdentity]:
    ids = identities if identities is not None else await identities_of(ctx.db, c.id)
    c.search_text = build_search_text(c, ids)
    if not c.primary_email:
        c.primary_email = next((i.value_norm for i in ids if i.kind == "email"), None)
    if not c.primary_phone:
        c.primary_phone = next((i.value_norm for i in ids if i.kind == "phone"), None)
    return ids


async def _reload(ctx: CommandContext, *rows) -> None:
    """Flush pending changes and reload server-generated columns (updated_at is expired after an UPDATE flush;
    reading it lazily would attempt sync IO under asyncio)."""
    await ctx.db.flush()
    for r in rows:
        await ctx.db.refresh(r)


def _emit_contact(ctx: CommandContext, c: Contact, change: str, **extra) -> None:
    ctx.emit("contact.changed", aggregate_type="contact", aggregate_id=c.id, aggregate_version=c.version,
             payload={"contact_id": c.id, "change": change, "status": c.status, **extra})


def _validate_roles(roles: list[str]) -> list[str]:
    out = []
    for r in roles or []:
        r = (r or "").strip().lower()
        if r not in CONTACT_ROLES:
            raise ValidationFailed(f"role must be one of {CONTACT_ROLES}", role=r)
        if r not in out:
            out.append(r)
    return out


class IdentityIn(BaseModel):
    kind: str
    value: str = Field(min_length=1, max_length=320)
    label: str = ""
    verified: bool | None = None       # default: True when a signed-in person enters it manually
    is_primary: bool = False
    source: str | None = None
    country: str | None = None         # phones: ISO country default (US)


def _normalize(inp: IdentityIn, ctx: CommandContext, default_source: str) -> dict:
    kind = (inp.kind or "").strip().lower()
    if kind not in IDENTITY_KINDS:
        raise ValidationFailed(f"identity kind must be one of {IDENTITY_KINDS}", kind=kind)
    country = (inp.country or "US").upper()
    norm = normalize_identity(kind, inp.value, country)
    if not norm:
        raise ValidationFailed(f"{kind} could not be normalized", value=inp.value)
    source = inp.source or default_source
    verified = inp.verified
    if verified is None:
        verified = ctx.actor.kind == "user" and source == "manual"
    return {"kind": kind, "value_raw": inp.value.strip(), "value_norm": norm, "label": inp.label or "",
            "verified": bool(verified), "is_primary": bool(inp.is_primary), "source": source,
            "country": country if kind == "phone" else None}


async def _live_contacts_for(db, kind: str, norm: str) -> list[Contact]:
    rows = (await db.execute(select(Contact).join(ContactIdentity, ContactIdentity.contact_id == Contact.id)
                             .where(ContactIdentity.kind == kind, ContactIdentity.value_norm == norm))).scalars().unique().all()
    out: list[Contact] = []
    for c in rows:
        if c.status == "merged" and c.merged_into_id:
            s = await db.get(Contact, c.merged_into_id)
            if s is not None and s.status != "archived" and all(x.id != s.id for x in out):
                out.append(s)
        elif c.status != "archived" and all(x.id != c.id for x in out):
            out.append(c)
    return out


# ── create / update ──────────────────────────────────────────────────────────
class ContactCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    company: str | None = None
    roles: list[str] = Field(default_factory=list)
    identities: list[IdentityIn] = Field(default_factory=list)
    source: str = "manual"
    source_ref: str | None = None      # ingestion dedupe key (message id, provider customer id ...)
    notes: str = ""
    consent: dict = Field(default_factory=dict)
    status: str = "active"             # active | provisional
    provisional_reason: str | None = None
    extra: dict = Field(default_factory=dict)
    legacy_customer_id: str | None = None
    force_new: bool = False            # create even if an identity already belongs to one existing contact


@command("contacts.create", input=ContactCreateIn, perm="contacts.write", action_class="internal",
         description="Create a person/company contact with identities. Retries and known identities do not duplicate rows.")
async def contacts_create(ctx: CommandContext, inp: ContactCreateIn) -> dict:
    if inp.status not in ("active", "provisional"):
        raise ValidationFailed("status must be active or provisional")
    roles = _validate_roles(inp.roles)
    if inp.source_ref:
        existing = (await ctx.db.execute(select(Contact).where(Contact.source_ref == inp.source_ref,
                                                               Contact.status != "merged"))).scalars().first()
        if existing is not None:
            return {"contact": serialize_contact(existing, await identities_of(ctx.db, existing.id)),
                    "created": False, "matched_by": "source_ref"}
    normalized = [_normalize(i, ctx, inp.source) for i in inp.identities]
    seen: set[tuple[str, str]] = set()
    normalized = [n for n in normalized if not ((n["kind"], n["value_norm"]) in seen or seen.add((n["kind"], n["value_norm"])))]
    if not inp.force_new and normalized:
        found: dict[str, Contact] = {}
        hits: list[str] = []
        for n in normalized:
            for c in await _live_contacts_for(ctx.db, n["kind"], n["value_norm"]):
                found[c.id] = c
                hits.append(f"{n['kind']} {n['value_norm']}")
        if len(found) == 1:
            c = next(iter(found.values()))
            return {"contact": serialize_contact(c, await identities_of(ctx.db, c.id)), "created": False,
                    "matched_by": hits}
        if len(found) > 1:
            raise Blocked("identity already belongs to several contacts; resolve before creating another",
                          candidates=[{"contact_id": c.id, "name": c.name} for c in found.values()], matched_by=hits)
    c = Contact(name=inp.name.strip(), company=(inp.company or "").strip() or None, roles=roles, source=inp.source,
                source_ref=inp.source_ref, notes=inp.notes or "", consent=dict(inp.consent or {}), status=inp.status,
                provisional_reason=inp.provisional_reason if inp.status == "provisional" else None,
                extra=dict(inp.extra or {}), legacy_customer_id=inp.legacy_customer_id,
                created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id, aliases=[])
    ctx.db.add(c)
    await ctx.db.flush()
    ids: list[ContactIdentity] = []
    for n in normalized:
        i = ContactIdentity(contact_id=c.id, created_by=ctx.actor.user_id, **n)
        if n["verified"]:
            i.verified_by, i.verified_at = ctx.actor.user_id, ctx.now
        ctx.db.add(i)
        ids.append(i)
    await ctx.db.flush()
    c.primary_email = next((i.value_norm for i in ids if i.kind == "email" and i.is_primary), None) or \
        next((i.value_norm for i in ids if i.kind == "email"), None)
    c.primary_phone = next((i.value_norm for i in ids if i.kind == "phone" and i.is_primary), None) or \
        next((i.value_norm for i in ids if i.kind == "phone"), None)
    c.search_text = build_search_text(c, ids)
    ctx.changed.append({"kind": "contact", "id": c.id, "version": c.version})
    ctx.record(f"Created contact: {c.name}", entity_kind="contact", entity_id=c.id, kind="task", state=c.status,
               details={"roles": roles, "identities": len(ids), "source": c.source})
    _emit_contact(ctx, c, "created")
    return {"contact": serialize_contact(c, ids), "created": True}


class ContactUpdateIn(BaseModel):
    contact_id: str
    expected_version: int | None = None
    name: str | None = None
    company: str | None = None
    roles: list[str] | None = None
    notes: str | None = None
    consent: dict | None = None
    source: str | None = None
    extra: dict | None = None
    legacy_customer_id: str | None = None


@command("contacts.update", input=ContactUpdateIn, perm="contacts.write", action_class="internal",
         description="Edit contact fields (name, company, roles, notes, consent).")
async def contacts_update(ctx: CommandContext, inp: ContactUpdateIn) -> dict:
    c = await _get(ctx, inp.contact_id, inp.expected_version)
    if c.status == "merged":
        raise Blocked("contact was merged; edit the surviving contact", merged_into_id=c.merged_into_id)
    if inp.name is not None:
        if not inp.name.strip():
            raise ValidationFailed("name cannot be empty")
        c.name = inp.name.strip()
    if inp.company is not None:
        c.company = inp.company.strip() or None
    if inp.roles is not None:
        c.roles = _validate_roles(inp.roles)
    if inp.notes is not None:
        c.notes = inp.notes
    if inp.consent is not None:
        c.consent = {**(c.consent or {}), **inp.consent}
    if inp.source is not None:
        c.source = inp.source
    if inp.extra is not None:
        c.extra = {**(c.extra or {}), **inp.extra}
    if inp.legacy_customer_id is not None:
        c.legacy_customer_id = inp.legacy_customer_id
    ids = await _refresh(ctx, c)
    ctx.touch(c, "contact")
    await _reload(ctx, c)
    ctx.record(f"Updated contact: {c.name}", entity_kind="contact", entity_id=c.id, kind="task", state=c.status)
    _emit_contact(ctx, c, "updated")
    return {"contact": serialize_contact(c, ids)}


# ── identities ───────────────────────────────────────────────────────────────
class IdentityAddIn(IdentityIn):
    contact_id: str
    expected_version: int | None = None


@command("contacts.add_identity", input=IdentityAddIn, perm="contacts.write", action_class="internal",
         description="Add an email/phone/handle alias. Idempotent per (kind, normalized value).")
async def contacts_add_identity(ctx: CommandContext, inp: IdentityAddIn) -> dict:
    c = await _get(ctx, inp.contact_id, inp.expected_version)
    if c.status in ("merged", "archived"):
        raise Blocked(f"contact is {c.status}")
    n = _normalize(inp, ctx, "manual")
    ids = await identities_of(ctx.db, c.id)
    existing = next((i for i in ids if i.kind == n["kind"] and i.value_norm == n["value_norm"]), None)
    shared = [x.id for x in await _live_contacts_for(ctx.db, n["kind"], n["value_norm"]) if x.id != c.id]
    if existing is not None:
        changed = False
        if n["verified"] and not existing.verified:
            existing.verified, existing.verified_by, existing.verified_at = True, ctx.actor.user_id, ctx.now
            changed = True
        if n["is_primary"] and not existing.is_primary:
            for i in ids:
                if i.kind == existing.kind:
                    i.is_primary = False
            existing.is_primary = True
            changed = True
        if changed:
            if existing.kind == "email" and existing.is_primary:
                c.primary_email = existing.value_norm
            if existing.kind == "phone" and existing.is_primary:
                c.primary_phone = existing.value_norm
            ctx.touch(c, "contact")
            _emit_contact(ctx, c, "identity_updated", identity_id=existing.id)
        return {"contact": serialize_contact(c, ids), "identity": serialize_identity(existing), "created": False,
                "shared_with": shared}
    i = ContactIdentity(contact_id=c.id, created_by=ctx.actor.user_id, **n)
    if n["verified"]:
        i.verified_by, i.verified_at = ctx.actor.user_id, ctx.now
    if n["is_primary"]:
        for x in ids:
            if x.kind == n["kind"]:
                x.is_primary = False
    ctx.db.add(i)
    await ctx.db.flush()
    ids.append(i)
    if i.kind == "email" and (i.is_primary or not c.primary_email):
        c.primary_email = i.value_norm
    if i.kind == "phone" and (i.is_primary or not c.primary_phone):
        c.primary_phone = i.value_norm
    c.search_text = build_search_text(c, ids)
    ctx.touch(c, "contact")
    ctx.record(f"Added {i.kind} to {c.name}: {i.value_raw}", entity_kind="contact", entity_id=c.id, kind="task",
               state=c.status, details={"identity_id": i.id, "shared_with": shared})
    _emit_contact(ctx, c, "identity_added", identity_id=i.id, shared_with=shared)
    return {"contact": serialize_contact(c, ids), "identity": serialize_identity(i), "created": True, "shared_with": shared}


class IdentityRemoveIn(BaseModel):
    contact_id: str
    identity_id: str
    expected_version: int | None = None


@command("contacts.remove_identity", input=IdentityRemoveIn, perm="contacts.write", action_class="internal",
         description="Remove an alias from a contact.")
async def contacts_remove_identity(ctx: CommandContext, inp: IdentityRemoveIn) -> dict:
    c = await _get(ctx, inp.contact_id, inp.expected_version)
    i = await ctx.db.get(ContactIdentity, inp.identity_id)
    if i is None or i.contact_id != c.id:
        raise NotFound("identity not found on this contact")
    removed = serialize_identity(i)
    await ctx.db.delete(i)
    await ctx.db.flush()
    ids = await identities_of(ctx.db, c.id)
    if i.kind == "email" and c.primary_email == i.value_norm:
        c.primary_email = next((x.value_norm for x in ids if x.kind == "email"), None)
    if i.kind == "phone" and c.primary_phone == i.value_norm:
        c.primary_phone = next((x.value_norm for x in ids if x.kind == "phone"), None)
    c.search_text = build_search_text(c, ids)
    ctx.touch(c, "contact")
    await _reload(ctx, c)
    ctx.record(f"Removed {removed['kind']} from {c.name}: {removed['value']}", entity_kind="contact", entity_id=c.id,
               kind="task", state=c.status)
    _emit_contact(ctx, c, "identity_removed", identity_id=removed["id"])
    return {"contact": serialize_contact(c, ids), "removed": removed}


# ── merge / unmerge ──────────────────────────────────────────────────────────
class MergeIn(BaseModel):
    survivor_id: str
    merged_id: str
    reason: str = ""
    expected_survivor_version: int | None = None
    expected_merged_version: int | None = None


def _merge_summary(p: MergeIn) -> str:
    return f"Merge contact {p.merged_id[:8]} into {p.survivor_id[:8]}"


async def _merge_revalidate(ctx: CommandContext, inp: MergeIn, approval) -> list[str]:
    failing: list[str] = []
    for label, cid, ver in (("survivor", inp.survivor_id, inp.expected_survivor_version),
                            ("merged", inp.merged_id, inp.expected_merged_version)):
        c = await ctx.db.get(Contact, cid)
        if c is None:
            failing.append(f"{label} contact no longer exists")
        elif c.status in ("merged", "archived"):
            failing.append(f"{label} contact is {c.status}")
        elif ver is not None and c.version != ver:
            failing.append(f"{label} contact changed since the merge was proposed")
    return failing


def _relink_models():
    import importlib
    out = []
    for label, mod, name, col in RELINK_TARGETS:
        try:
            m = importlib.import_module(mod, package=__package__)
            model = getattr(m, name)
        except Exception:  # noqa: BLE001 - another domain's module may be mid-edit in dev
            continue
        out.append((label, model, col))
    return out


async def _do_merge(ctx: CommandContext, inp: MergeIn) -> dict:
    if inp.survivor_id == inp.merged_id:
        raise ValidationFailed("survivor and merged contact must differ")
    # lock in a stable order to avoid deadlocks
    first, second = sorted([inp.survivor_id, inp.merged_id])
    await _get(ctx, first)
    await _get(ctx, second)
    s = await _get(ctx, inp.survivor_id, inp.expected_survivor_version)
    m = await _get(ctx, inp.merged_id, inp.expected_merged_version)
    for label, c in (("survivor", s), ("merged", m)):
        if c.status == "merged":
            raise Blocked(f"{label} contact is already merged", merged_into_id=c.merged_into_id)
        if c.status == "archived":
            raise Blocked(f"{label} contact is archived; restore it first")
    now = ctx.now
    snapshot: dict = {
        "merged": {"name": m.name, "company": m.company, "roles": list(m.roles or []), "primary_email": m.primary_email,
                   "primary_phone": m.primary_phone, "status": m.status, "verified": bool(m.verified),
                   "consent": dict(m.consent or {}), "source": m.source, "notes": m.notes, "search_text": m.search_text,
                   "extra": dict(m.extra or {}), "aliases": list(m.aliases or []), "provisional_reason": m.provisional_reason,
                   "version": m.version},
        "survivor_before": {"name": s.name, "company": s.company, "roles": list(s.roles or []), "notes": s.notes,
                            "consent": dict(s.consent or {}), "aliases": list(s.aliases or []),
                            "primary_email": s.primary_email, "primary_phone": s.primary_phone,
                            "extra": dict(s.extra or {}), "version": s.version},
        "relinked": {}, "identities_moved": [], "identities_kept_on_merged": [],
    }
    # relink rows in other tables
    for label, model, col in _relink_models():
        column = getattr(model, col)
        ids = [r[0] for r in (await ctx.db.execute(select(model.id).where(column == m.id))).all()]
        if ids:
            await ctx.db.execute(update(model).where(model.id.in_(ids)).values({col: s.id}))
            snapshot["relinked"][label] = ids
    # identities: move unless the survivor already has the same (kind, norm)
    s_ids = await identities_of(ctx.db, s.id)
    have = {(i.kind, i.value_norm) for i in s_ids}
    for i in await identities_of(ctx.db, m.id):
        if (i.kind, i.value_norm) in have:
            snapshot["identities_kept_on_merged"].append(i.id)
            continue
        i.contact_id = s.id
        i.is_primary = False
        snapshot["identities_moved"].append(i.id)
        s_ids.append(i)
    # survivor absorbs aliases, roles, notes, consent (opt-out wins), primaries
    aliases = list(s.aliases or [])
    alias = {"name": m.name, "company": m.company, "from_contact_id": m.id, "at": now.isoformat()}
    if (m.name and m.name != s.name) or (m.company and m.company != s.company):
        aliases.append(alias)
    for a in m.aliases or []:
        if a not in aliases:
            aliases.append(a)
    s.aliases = aliases
    s.roles = _validate_roles(list(s.roles or []) + list(m.roles or []))
    if m.notes and m.notes.strip():
        s.notes = (s.notes + "\n" if s.notes else "") + f"[merged from {m.name}] {m.notes}"
    consent = dict(s.consent or {})
    for k, v in (m.consent or {}).items():
        if v is False or k not in consent:
            consent[k] = v
    s.consent = consent
    if not s.company and m.company:
        s.company = m.company
    s.extra = {**(s.extra or {}), "merged_from": list((s.extra or {}).get("merged_from", [])) + [m.id]}
    await _refresh(ctx, s, s_ids)
    m.status = "merged"
    m.merged_into_id = s.id
    m.merged_at = now
    await ctx.db.flush()
    rec = ContactMerge(survivor_id=s.id, merged_id=m.id, snapshot=snapshot, reason=inp.reason or "",
                       merged_by=ctx.actor.user_id, approval_id=ctx.approval.id if ctx.approval else None,
                       created_by=ctx.actor.user_id)
    ctx.db.add(rec)
    await ctx.db.flush()
    ctx.touch(s, "contact")
    ctx.touch(m, "contact")
    await _reload(ctx, s, m)
    ctx.record(f"Merged contact {m.name} into {s.name}", entity_kind="contact", entity_id=s.id, kind="task",
               state="merged", details={"merge_id": rec.id, "merged_id": m.id, "relinked": {k: len(v) for k, v in snapshot["relinked"].items()},
                                        "reason": inp.reason})
    _emit_contact(ctx, s, "merged", merge_id=rec.id, merged_id=m.id)
    _emit_contact(ctx, m, "merged_away", merge_id=rec.id, survivor_id=s.id)
    ctx.emit("identity.match_resolved", aggregate_type="contact", aggregate_id=s.id,
             payload={"change": "merge", "survivor_id": s.id, "merged_id": m.id, "merge_id": rec.id})
    return {"merge": serialize_merge(rec), "survivor": serialize_contact(s, s_ids), "merged": serialize_contact(m)}


@command("contacts.merge_propose", input=MergeIn, perm="contacts.write", action_class="consequential",
         approval_kind="contact_merge", summary=_merge_summary, revalidate=_merge_revalidate,
         consequence=lambda p: {"targets": {"contacts": [p.survivor_id, p.merged_id]}, "reversible": True,
                                "moves_money": False},
         description="Propose merging two contacts. Executes only under the owner's exact approval (spec §3.3).")
async def contacts_merge_propose(ctx: CommandContext, inp: MergeIn) -> dict:
    # Reached only under an exact approval (or an owner-granted standing permission): perform the merge.
    return await _do_merge(ctx, inp)


@command("contacts.merge", input=MergeIn, perm="contacts.write", action_class="owner_only",
         description="Owner merges two contacts: relinks opportunities/tasks/conversations/identities, keeps aliases, audited and undoable.")
async def contacts_merge(ctx: CommandContext, inp: MergeIn) -> dict:
    return await _do_merge(ctx, inp)


class UnmergeIn(BaseModel):
    merge_id: str
    reason: str = ""


@command("contacts.unmerge", input=UnmergeIn, perm="contacts.write", action_class="owner_only",
         description="Undo a merge from its snapshot: rows and identities go back, aliases restored.")
async def contacts_unmerge(ctx: CommandContext, inp: UnmergeIn) -> dict:
    rec = (await ctx.db.execute(select(ContactMerge).where(ContactMerge.id == inp.merge_id).with_for_update())).scalar_one_or_none()
    if rec is None:
        raise NotFound("merge not found")
    if rec.reverted_at is not None:
        s = await ctx.db.get(Contact, rec.survivor_id)
        m = await ctx.db.get(Contact, rec.merged_id)
        return {"merge": serialize_merge(rec), "survivor": serialize_contact(s), "merged": serialize_contact(m), "reverted": False}
    first, second = sorted([rec.survivor_id, rec.merged_id])
    await _get(ctx, first)
    await _get(ctx, second)
    s = await _get(ctx, rec.survivor_id)
    m = await _get(ctx, rec.merged_id)
    if m.status != "merged" or m.merged_into_id != s.id:
        raise Blocked("merged contact is no longer in the merged state recorded by this merge")
    snap = rec.snapshot or {}
    models = {label: (model, col) for label, model, col in _relink_models()}
    for label, ids in (snap.get("relinked") or {}).items():
        if label in models and ids:
            model, col = models[label]
            column = getattr(model, col)
            await ctx.db.execute(update(model).where(model.id.in_(ids), column == s.id).values({col: m.id}))
    moved = snap.get("identities_moved") or []
    if moved:
        await ctx.db.execute(update(ContactIdentity).where(ContactIdentity.id.in_(moved)).values(contact_id=m.id))
    ms = snap.get("merged") or {}
    m.status = ms.get("status", "active")
    m.merged_into_id = None
    m.merged_at = None
    for k in ("name", "company", "roles", "primary_email", "primary_phone", "consent", "notes", "aliases",
              "provisional_reason", "extra"):
        if k in ms:
            setattr(m, k, ms[k])
    sb = snap.get("survivor_before") or {}
    for k in ("name", "company", "roles", "notes", "consent", "aliases", "primary_email", "primary_phone", "extra"):
        if k in sb:
            setattr(s, k, sb[k])
    await ctx.db.flush()
    s_ids = await _refresh(ctx, s)
    m_ids = await _refresh(ctx, m)
    rec.reverted_at = ctx.now
    rec.reverted_by = ctx.actor.user_id
    rec.reason = (rec.reason + "\n" if rec.reason else "") + f"[unmerged] {inp.reason}" if inp.reason else rec.reason
    ctx.touch(s, "contact")
    ctx.touch(m, "contact")
    ctx.touch(rec, "contact_merge")
    await _reload(ctx, s, m, rec)
    ctx.record(f"Unmerged contact {m.name} from {s.name}", entity_kind="contact", entity_id=m.id, kind="task",
               state=m.status, details={"merge_id": rec.id, "reason": inp.reason}, exception=True)
    _emit_contact(ctx, s, "unmerged", merge_id=rec.id, restored_id=m.id)
    _emit_contact(ctx, m, "restored", merge_id=rec.id, survivor_id=s.id)
    ctx.emit("identity.match_resolved", aggregate_type="contact", aggregate_id=m.id,
             payload={"change": "unmerge", "survivor_id": s.id, "merged_id": m.id, "merge_id": rec.id})
    return {"merge": serialize_merge(rec), "survivor": serialize_contact(s, s_ids), "merged": serialize_contact(m, m_ids),
            "reverted": True}


# ── lifecycle ────────────────────────────────────────────────────────────────
class ContactRefIn(BaseModel):
    contact_id: str
    expected_version: int | None = None
    reason: str = ""


@command("contacts.archive", input=ContactRefIn, perm="contacts.write", action_class="internal",
         description="Archive a contact; queued approvals bound to it are invalidated (invariant 12).")
async def contacts_archive(ctx: CommandContext, inp: ContactRefIn) -> dict:
    c = await _get(ctx, inp.contact_id, inp.expected_version)
    if c.status == "merged":
        raise Blocked("contact was merged; archive the surviving contact instead", merged_into_id=c.merged_into_id)
    if c.status == "archived":
        return {"contact": serialize_contact(c, await identities_of(ctx.db, c.id)), "archived": False}
    c.extra = {**(c.extra or {}), "status_before_archive": c.status}
    c.status = "archived"
    c.archived_at = ctx.now
    from .approvals import invalidate_for_entity
    n = await invalidate_for_entity(ctx, "contact", c.id, f"contact archived: {inp.reason or 'no reason given'}")
    ctx.touch(c, "contact")
    await _reload(ctx, c)
    ctx.record(f"Archived contact: {c.name}", entity_kind="contact", entity_id=c.id, kind="task", state="archived",
               details={"reason": inp.reason, "invalidated_approvals": n})
    _emit_contact(ctx, c, "archived")
    return {"contact": serialize_contact(c, await identities_of(ctx.db, c.id)), "archived": True, "invalidated_approvals": n}


@command("contacts.restore", input=ContactRefIn, perm="contacts.write", action_class="internal",
         description="Restore an archived contact.")
async def contacts_restore(ctx: CommandContext, inp: ContactRefIn) -> dict:
    c = await _get(ctx, inp.contact_id, inp.expected_version)
    if c.status != "archived":
        return {"contact": serialize_contact(c, await identities_of(ctx.db, c.id)), "restored": False}
    c.status = (c.extra or {}).get("status_before_archive") or "active"
    if c.status not in ("active", "provisional"):
        c.status = "active"
    c.archived_at = None
    ctx.touch(c, "contact")
    ctx.record(f"Restored contact: {c.name}", entity_kind="contact", entity_id=c.id, kind="task", state=c.status)
    _emit_contact(ctx, c, "restored")
    return {"contact": serialize_contact(c, await identities_of(ctx.db, c.id)), "restored": True}


class ProvisionalIn(BaseModel):
    contact_id: str
    expected_version: int | None = None
    provisional: bool = True
    reason: str | None = None


@command("contacts.mark_provisional", input=ProvisionalIn, perm="contacts.write", action_class="internal",
         description="Mark a contact provisional (unknown low-risk sender) or confirm it as active.")
async def contacts_mark_provisional(ctx: CommandContext, inp: ProvisionalIn) -> dict:
    c = await _get(ctx, inp.contact_id, inp.expected_version)
    if c.status in ("merged", "archived"):
        raise Blocked(f"contact is {c.status}")
    if inp.provisional:
        c.status = "provisional"
        c.provisional_reason = inp.reason or c.provisional_reason or "unconfirmed identity"
        what = f"Marked provisional: {c.name}"
    else:
        c.status = "active"
        c.provisional_reason = None
        c.verified = True
        what = f"Confirmed contact: {c.name}"
    ctx.touch(c, "contact")
    ctx.record(what, entity_kind="contact", entity_id=c.id, kind="task", state=c.status, details={"reason": inp.reason})
    _emit_contact(ctx, c, "provisional" if inp.provisional else "confirmed")
    return {"contact": serialize_contact(c, await identities_of(ctx.db, c.id))}
