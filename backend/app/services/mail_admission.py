"""Personal-mailbox allowlist admission (spec §4.1, §3.3; acceptance A05, A06).

Admission is decided from **metadata only** — envelope sender, recipients, thread id and labels —
*before* any body, attachment or snippet is fetched. Rejected mail is counted and discarded: its
text never reaches storage, the corpus, the model or the logs. Matching cannot widen the allowlist
(a body that merely mentions "Sebastian" is not an approved sender), and neither can retrieved or
inbound content.

Narrowing the allowlist quarantines what it used to admit: corpus chunks are tombstoned through
``corpus.tombstone`` and stored bodies are deleted, keeping only minimal audit metadata
(provider id, thread, admission rule, timestamps, reason).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from ..models.comms import Connection, Conversation, Message
from .matching import normalize_email

PERSONAL_PROVIDERS = ("gmail_personal",)
RULE_KINDS = ("sender", "domain", "thread", "label")


@dataclass
class AdmissionResult:
    admitted: bool
    rule: str | None = None            # sender|domain|thread|label|account (why it was admitted)
    reason: str = ""                   # why it was rejected (no content, ever)
    participants_outside: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"admitted": self.admitted, "rule": self.rule, "reason": self.reason,
                "participants_outside": self.participants_outside}


def requires_admission(conn: Connection | None) -> bool:
    return bool(conn is not None and conn.provider in PERSONAL_PROVIDERS)


def rules_for(conn: Connection | None) -> dict:
    cfg = dict((conn.config if conn else None) or {})
    return {
        "senders": sorted({normalize_email(s) or "" for s in (cfg.get("allowlist_senders") or []) if s} - {""}),
        "domains": sorted({str(d).strip().lstrip("@").lower() for d in (cfg.get("allowlist_domains") or []) if d}),
        "threads": sorted({str(t).strip() for t in (cfg.get("allowlist_threads") or []) if t}),
        "labels": sorted({str(l).strip() for l in (cfg.get("allowlist_labels") or []) if l}),
        "expected_identity": normalize_email(cfg.get("expected_identity")) or normalize_email(conn.account_identity if conn else None),
    }


def _domain(addr: str | None) -> str:
    a = normalize_email(addr)
    return a.split("@")[-1] if a else ""


def _authorized_party(addr: str | None, rules: dict) -> bool:
    a = normalize_email(addr)
    if not a:
        return False
    if rules["expected_identity"] and a == rules["expected_identity"]:
        return True
    return a in rules["senders"] or _domain(a) in rules["domains"]


def evaluate(rules: dict, meta: dict) -> AdmissionResult:
    """Decide admission from message metadata. `meta` carries from/to/cc/thread_id/label_ids only."""
    sender = normalize_email(meta.get("from"))
    if not sender:
        return AdmissionResult(False, reason="no usable envelope sender")
    participants = [normalize_email(a) for a in ([meta.get("from")] + list(meta.get("to") or []) + list(meta.get("cc") or []))]
    participants = [p for p in participants if p]
    outside = sorted({p for p in participants if not _authorized_party(p, rules)})
    if sender in rules["senders"]:
        return AdmissionResult(True, rule="sender", participants_outside=outside)
    if _domain(sender) in rules["domains"]:
        return AdmissionResult(True, rule="domain", participants_outside=outside)
    labels = set(str(l) for l in (meta.get("label_ids") or []))
    if rules["labels"] and labels & set(rules["labels"]):
        return AdmissionResult(True, rule="label", participants_outside=outside)
    thread_id = str(meta.get("thread_id") or "")
    if thread_id and thread_id in rules["threads"]:
        # an approved thread that gains unrelated participants is re-evaluated per message (A06)
        if outside:
            return AdmissionResult(False, reason="approved thread gained unrelated participants",
                                   participants_outside=outside)
        return AdmissionResult(True, rule="thread", participants_outside=[])
    return AdmissionResult(False, reason="sender, domain, thread and label are all outside the allowlist",
                           participants_outside=outside)


def admit(conn: Connection | None, meta: dict) -> AdmissionResult:
    """Business mailboxes admit everything they receive; personal mailboxes apply the allowlist."""
    if not requires_admission(conn):
        return AdmissionResult(True, rule="account")
    return evaluate(rules_for(conn), meta)


def count_excluded(conn: Connection, reason: str) -> dict:
    """Rejected content is counted, never stored (spec §4.1, §4.2 exclusion counts)."""
    counts = dict(conn.excluded_counts or {})
    counts["total"] = int(counts.get("total", 0)) + 1
    by = dict(counts.get("by_reason") or {})
    by[reason] = int(by.get(reason, 0)) + 1
    counts["by_reason"] = by
    counts["last_at"] = datetime.now(timezone.utc).isoformat()
    conn.excluded_counts = counts
    return counts


def audit_only(msg: Message) -> None:
    """Strip everything but minimal audit metadata from a stored message (A06 retention rule)."""
    msg.body_text = ""
    msg.body_new_text = ""
    msg.body_html = ""
    msg.snippet = ""
    msg.subject = ""
    msg.attachments = []
    msg.extracted = {}
    msg.headers = {k: v for k, v in (msg.headers or {}).items() if k == "message-id"}
    msg.to_addrs = []
    msg.cc_addrs = []


async def quarantine_unauthorized(ctx, conn: Connection, *, reason: str = "allowlist_narrowed") -> dict:
    """Re-evaluate every stored message of a personal connection against the CURRENT allowlist.
    No-longer-authorized messages lose their text from retrieval (corpus tombstone) and storage."""
    from .knowledge import tombstone_source
    if not requires_admission(conn):
        return {"checked": 0, "quarantined": 0, "message_ids": []}
    rules = rules_for(conn)
    rows = (await ctx.db.execute(select(Message).where(Message.connection_id == conn.id,
                                                       Message.admitted.is_(True),
                                                       Message.quarantined_at.is_(None)))).scalars().all()
    quarantined: list[str] = []
    for m in rows:
        res = evaluate(rules, {"from": m.from_addr, "to": list(m.to_addrs or []), "cc": list(m.cc_addrs or []),
                               "thread_id": m.provider_thread_id, "label_ids": list(m.label_ids or [])})
        if res.admitted:
            continue
        await tombstone_source(ctx, "message", m.id, reason=reason)
        audit_only(m)
        m.admitted = False
        m.excluded_reason = res.reason or reason
        m.quarantined_at = ctx.now
        m.bump(ctx.actor.user_id)
        count_excluded(conn, f"quarantined:{res.reason or reason}")
        quarantined.append(m.id)
    conv_ids = sorted({m.conversation_id for m in rows if m.id in quarantined})
    for cid in conv_ids:
        conv = await ctx.db.get(Conversation, cid)
        if conv is None:
            continue
        remaining = (await ctx.db.execute(select(Message).where(Message.conversation_id == cid,
                                                                Message.admitted.is_(True)))).scalars().all()
        if not remaining:
            conv.state = "archived"
            conv.no_reply_reason = "quarantined: no longer authorized by the personal allowlist"
            conv.bump(ctx.actor.user_id)
    if quarantined:
        ctx.record(f"Quarantined {len(quarantined)} personal message(s) after an allowlist change",
                   entity_kind="connection", entity_id=conn.id, kind="access", state="quarantined",
                   visibility="owner", details={"reason": reason, "messages": len(quarantined)})
        ctx.emit("connection.degraded", aggregate_type="connection", aggregate_id=conn.id,
                 payload={"provider": conn.provider, "kind": "allowlist_narrowed", "quarantined": len(quarantined)})
    return {"checked": len(rows), "quarantined": len(quarantined), "message_ids": quarantined,
            "conversations": conv_ids}
