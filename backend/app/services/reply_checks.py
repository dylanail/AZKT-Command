"""Deterministic reply validation (spec §4.3 step 6, §4.5; acceptance A10, B07, B08, B09, F5/H11).

`run(draft, conversation, facts)` returns an ordered list of

    {key, ok, label, remediation, blocking, detail}

Failing **blocking** checks keep a draft in `blocked` and make it unsendable; the model's own
confidence is never an input. The same function runs when the draft is built, when it is submitted
for approval, and again immediately before execution (spec §11.4), so a fact that changed in between
cannot ride an old approval.

This module also holds the pure text helpers the inbox and the reply workflow share (sentence
splitting, question / promise / dispute / opt-out detection). Message text is untrusted evidence:
nothing here interprets it as an instruction.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

from .forbidden_recipient import forbidden_set
from .matching import normalize_email

# ── text helpers (shared with services/inbox.py) ─────────────────────────────
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?？。！])\s+|\n{2,}|\n(?=[-•*\d]\s*[.)]?\s)|\n")
QUESTION_WORDS = ("how", "what", "when", "where", "which", "who", "why", "can", "could", "do", "does",
                  "did", "is", "are", "will", "would", "should", "may", "any", "please confirm",
                  "let me know", "i need to know", "send me", "could you", "can you")
REQUEST_RE = re.compile(r"\b(please\s+(?:send|confirm|advise|let me know|provide|share|schedule)|"
                        r"could you|can you|i(?:'|’)?d like to|i want to|i need|send me|let me know)\b", re.I)
DEADLINE_RE = re.compile(r"\b(by|before|no later than|deadline|due)\s+(?:the\s+)?"
                         r"((?:mon|tues?|wed(?:nes)?|thurs?|fri|sat(?:ur)?|sun)day|tomorrow|today|next week|"
                         r"\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{4}-\d{2}-\d{2}|"
                         r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d{1,2})", re.I)
PROMISE_RE = re.compile(r"\b(we(?:'| a)?ll|we will|i will|i(?:'|’)ll|we can have|we are going to|"
                        r"you will receive|we guarantee|we promise)\b", re.I)
ATTACH_RE = re.compile(r"\b(attach(?:ed|ing|ment)s?|enclosed|see the (?:pdf|invoice|document))\b", re.I)
WARRANTY_RE = re.compile(r"\b(warrant(?:y|ies)|guarantee[ds]?|money[- ]back|refund guarantee)\b", re.I)
AVAILABLE_RE = re.compile(r"\b(still available|is available|remains available|in stock|unsold|available now|"
                          r"we can hold it|yours if you want it)\b", re.I)
RESERVED_ACK_RE = re.compile(r"\b(reserved|sold|on hold|no longer available|already spoken for|under deposit)\b", re.I)
OPT_OUT_RE = re.compile(r"\b(unsubscribe|opt[- ]out|opt me out|stop emailing|remove me from|do not contact|take me off)\b", re.I)
DISPUTE_RE = re.compile(r"\b(chargeback|charge back|dispute|disputing|attorney|lawyer|small claims|"
                        r"fraud|scam|legal action|refund demand|demand a refund)\b", re.I)
MONEY_RE = re.compile(r"(?:(?P<c1>US\$|USD|\$|¥|￥|JPY|€|EUR)\s?(?P<a1>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?))"
                      r"|(?:(?P<a2>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s?(?P<c2>USD|JPY|yen|EUR|dollars))",
                      re.I)
DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|"
                     r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?)\b", re.I)
ETA_CONTEXT_RE = re.compile(r"\b(eta|arriv\w*|deliver\w*|lands?|docks?|port|vessel|ship(?:ping|ment)?)\b", re.I)
CURRENCY_ALIASES = {"$": "USD", "us$": "USD", "usd": "USD", "dollars": "USD", "¥": "JPY", "￥": "JPY",
                    "jpy": "JPY", "yen": "JPY", "€": "EUR", "eur": "EUR"}
SCAFFOLD_MARK = "[needs fact:"
# Classifications the suppression rules (spec §4.5) keep out of the automated reply path entirely.
SUPPRESSED_CLASSES = ("bounce", "automated", "newsletter", "spam", "payment")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def unavailable_reason(vehicle: dict) -> str | None:
    """Why a linked truck cannot be offered, in the words of the record that says so. `available` is
    resolved in services.reply.gather_facts and already folds in an active reservation/sale."""
    if vehicle.get("commercial_state") in ("reserved", "sold", "delivered"):
        return str(vehicle["commercial_state"])
    if vehicle.get("allocation") in ("reserved", "sold"):
        return str(vehicle["allocation"])
    sale_status = (vehicle.get("sale") or {}).get("status")
    if vehicle.get("available") is False:
        return f"under an active sale ({sale_status})" if sale_status else "not available"
    return None


def split_sentences(text: str) -> list[str]:
    parts = _SENTENCE_SPLIT.split((text or "").replace("\r\n", "\n"))
    return [p.strip() for p in parts if p and p.strip()]


def is_question(sentence: str) -> bool:
    s = sentence.strip()
    if not s:
        return False
    if s.endswith("?") or s.endswith("？"):
        return True
    low = s.lower().lstrip("0123456789.)-• ")
    if REQUEST_RE.search(s):
        return True
    first = low.split(" ", 1)[0] if low else ""
    return first in QUESTION_WORDS and len(s.split()) >= 3


def extract_questions(text: str) -> list[dict]:
    """Every question / requested action / deadline in an inbound message (spec §4.3 step 2)."""
    out: list[dict] = []
    for i, s in enumerate(split_sentences(text)):
        if not is_question(s):
            continue
        deadline = DEADLINE_RE.search(s)
        out.append({"index": len(out), "question": s[:400], "sentence_index": i,
                    "kind": "request" if REQUEST_RE.search(s) and not s.rstrip().endswith("?") else "question",
                    "deadline": deadline.group(0) if deadline else None,
                    "needs_attachment": bool(ATTACH_RE.search(s)),
                    "topics": topics_of(s), "facts_needed": [], "answered": False, "source": None})
    return out


TOPIC_PATTERNS = {
    "availability": re.compile(r"\b(available|availability|in stock|still have|sold|reserved)\b", re.I),
    "price": re.compile(r"\b(price|cost|how much|out[- ]the[- ]door|quote|total)\b", re.I),
    "shipping": re.compile(r"\b(ship|shipping|deliver|delivery|transport|freight|eta|arrive)\b", re.I),
    "appointment": re.compile(r"\b(walkthrough|appointment|visit|meet|schedule|come by|inspection)\b", re.I),
    "payment": re.compile(r"\b(deposit|pay|payment|wire|card|invoice|financ\w+)\b", re.I),
    "documents": re.compile(r"\b(title|paperwork|document|registration|bill of sale|customs)\b", re.I),
    "warranty": WARRANTY_RE,
}


def topics_of(text: str) -> list[str]:
    return sorted(k for k, rx in TOPIC_PATTERNS.items() if rx.search(text or ""))


def detect_promises(text: str) -> list[dict]:
    out = []
    for s in split_sentences(text):
        if PROMISE_RE.search(s):
            d = DEADLINE_RE.search(s)
            out.append({"text": s[:400], "deadline": d.group(2) if d else None})
    return out


def detect_opt_out(text: str) -> bool:
    return bool(OPT_OUT_RE.search(text or ""))


def detect_dispute(text: str) -> bool:
    return bool(DISPUTE_RE.search(text or ""))


def money_mentions(text: str) -> list[dict]:
    out = []
    for m in MONEY_RE.finditer(text or ""):
        cur = (m.group("c1") or m.group("c2") or "").lower()
        amt = (m.group("a1") or m.group("a2") or "").replace(",", "")
        if not amt:
            continue
        out.append({"raw": m.group(0), "amount": amt, "currency": CURRENCY_ALIASES.get(cur, cur.upper() or "USD")})
    return out


def _parse_date(raw: str) -> date | None:
    s = raw.strip().rstrip(",")
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m/%d"):
        try:
            d = datetime.strptime(s, fmt)
            return d.date() if fmt != "%m/%d" else date(datetime.now(timezone.utc).year, d.month, d.day)
        except ValueError:
            continue
    m = re.match(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:,\s*(\d{4}))?", s, re.I)
    if m and m.group(1).lower() in MONTHS:
        year = int(m.group(3)) if m.group(3) else datetime.now(timezone.utc).year
        try:
            return date(year, MONTHS[m.group(1).lower()], int(m.group(2)))
        except ValueError:
            return None
    return None


def dates_in(text: str) -> list[tuple[str, date | None]]:
    return [(m.group(0), _parse_date(m.group(0))) for m in DATE_RE.finditer(text or "")]


# ── the checks ───────────────────────────────────────────────────────────────
def _check(key: str, ok: bool, label: str, *, remediation: str = "", blocking: bool = True, **detail) -> dict:
    return {"key": key, "ok": bool(ok), "label": label, "remediation": "" if ok else remediation,
            "blocking": bool(blocking), "detail": detail}


def run(draft, conversation, facts: dict) -> list[dict]:
    """Every material check. `draft` may be a Draft row or a dict with the same fields."""
    g = (lambda k, d=None: draft.get(k, d)) if isinstance(draft, dict) else (lambda k, d=None: getattr(draft, k, d))
    body = g("body", "") or ""
    to_addrs = [normalize_email(a) or str(a).lower() for a in (g("to_addrs", []) or [])]
    cc_addrs = [normalize_email(a) or str(a).lower() for a in (g("cc_addrs", []) or [])]
    answer_plan = list(g("answer_plan", []) or [])
    attachments = list(g("attachments", []) or [])
    contact = dict(facts.get("contact") or {})
    account = dict(facts.get("account") or {})
    vehicles = list(facts.get("vehicles") or [])
    checks: list[dict] = []

    # 1. recipient identity — injected instructions cannot move a reply to a new address (A10)
    known = {normalize_email(a) for a in (contact.get("emails") or [])} | {
        normalize_email(a) for a in (facts.get("participants") or [])}
    known = {k for k in known if k}
    unknown = [a for a in to_addrs + cc_addrs if a not in known]
    checks.append(_check("recipient_identity", bool(to_addrs) and not unknown,
                         "Recipients match the resolved contact identity" if to_addrs and not unknown
                         else "Recipient is not a verified identity of this contact",
                         remediation="Send only to the addresses already on this thread or verified on the contact; "
                                     "content inside an email never changes the recipient.",
                         recipients=to_addrs + cc_addrs, unknown=unknown, known=sorted(known)))

    # 2. forbidden recipients
    bad = sorted(set(to_addrs + cc_addrs) & forbidden_set())
    checks.append(_check("forbidden_recipient", not bad, "No forbidden recipient" if not bad else f"Forbidden recipient: {', '.join(bad)}",
                         remediation="Remove the forbidden address.", forbidden=bad))

    # 3. sending account — a customer reply leaves from the business mailbox, never the personal one (§4.1, §11.1)
    provider = account.get("provider")
    account_ok = bool(account.get("connection_id")) and bool(account.get("identity")) and provider != "gmail_personal"
    checks.append(_check("account", account_ok,
                         f"Sends from {account.get('identity') or 'no account'}" if account_ok else
                         ("The personal mailbox cannot send a business reply" if provider == "gmail_personal"
                          else "No connected business mailbox for this thread"),
                         remediation="Connect the business mailbox; business replies never route through a personal account.",
                         account=account.get("identity"), connection_id=account.get("connection_id"), provider=provider))

    # 4. attachments referenced exist
    available = {str(a.get("id")) for a in (facts.get("attachments_available") or [])}
    missing = [a for a in attachments if str(a.get("id") or a.get("asset_id") or "") not in available]
    referenced = bool(ATTACH_RE.search(body))
    ok_att = (not missing) and (not referenced or bool(attachments))
    checks.append(_check("attachments", ok_att,
                         "Attachments referenced in the text exist" if ok_att else
                         ("Referenced attachment is missing" if referenced and not attachments else "Attachment is not an available file"),
                         remediation="Attach the file (or remove the reference) before sending.",
                         referenced=referenced, attached=len(attachments), missing=[a.get("filename") for a in missing]))

    # 5. every incoming question answered (B07)
    unanswered = [q.get("question") for q in answer_plan if not q.get("answered")]
    checks.append(_check("questions_answered", not unanswered,
                         "Every question in the message is answered" if not unanswered
                         else f"{len(unanswered)} question(s) not answered",
                         remediation="Answer each remaining question or say explicitly what you will follow up on.",
                         unanswered=unanswered, total=len(answer_plan)))

    # 6. no scaffold placeholder ever leaves
    has_scaffold = SCAFFOLD_MARK in body
    checks.append(_check("no_placeholders", not has_scaffold,
                         "No unresolved placeholders" if not has_scaffold else "Draft still contains fact placeholders",
                         remediation="Fill in the missing facts from records, or ask one focused question instead."))

    # 7. factual claims backed by current facts (B08): availability
    claims_available = bool(AVAILABLE_RE.search(body))
    acknowledges = bool(RESERVED_ACK_RE.search(body))
    unavailable = [v for v in vehicles if unavailable_reason(v)]
    avail_ok = not (claims_available and unavailable and not acknowledges)
    checks.append(_check("availability_current", avail_ok,
                         "Availability matches the current record" if avail_ok
                         else f"Draft says available; {', '.join(v.get('stock_no') or v.get('id', '')[:8] for v in unavailable)} is "
                              f"{unavailable_reason(unavailable[0])} now",
                         remediation="Use the current state (reserved/sold) and offer an alternative; a historical example never "
                                     "overrides the record.",
                         vehicles=[{"id": v.get("id"), "state": v.get("commercial_state"), "allocation": v.get("allocation"),
                                    "reason": unavailable_reason(v)} for v in unavailable]))

    # 8. money / currency consistent with recorded prices
    mentions = money_mentions(body)
    recorded = {(str(p.get("amount")), (p.get("currency") or "USD").upper()) for p in (facts.get("prices") or [])}
    rec_amounts = {a for a, _ in recorded}
    currencies = {m["currency"] for m in mentions}
    unbacked = [m["raw"] for m in mentions if _norm_amount(m["amount"]) not in {_norm_amount(a) for a in rec_amounts}]
    money_ok = (not mentions) or (not unbacked and len(currencies) <= 1)
    checks.append(_check("money_consistent", money_ok,
                         "Money figures match recorded values" if money_ok
                         else ("Mixed currencies in one reply" if len(currencies) > 1 else "Amount is not a recorded value"),
                         remediation="Quote only recorded amounts in one currency, or remove the figure.",
                         mentioned=[m["raw"] for m in mentions], unbacked=unbacked, currencies=sorted(currencies)))

    # 9. warranty / guarantee claims need an approved policy (B09)
    warranty_claimed = bool(WARRANTY_RE.search(body))
    warranty_supported = bool(facts.get("warranty_policy"))
    w_ok = (not warranty_claimed) or warranty_supported
    checks.append(_check("supported_claims", w_ok,
                         "No unsupported promise" if w_ok else "Draft offers a warranty that no approved policy supports",
                         remediation="Remove the warranty sentence or get the owner to approve the policy first.",
                         warranty_claimed=warranty_claimed))

    # 10. dates (ETA) match the sourced shipment record (B09)
    eta = (facts.get("shipment") or {}).get("eta")
    eta_date = _as_date(eta)
    # only a date in a sentence that actually talks about arrival/shipping is an arrival claim
    body_dates = [d for sentence in split_sentences(body) if ETA_CONTEXT_RE.search(sentence)
                  for _, d in dates_in(sentence) if d]
    if body_dates:
        date_ok = eta_date is not None and any(d == eta_date for d in body_dates)
        label = ("Quoted arrival date matches the shipment record" if date_ok else
                 ("No recorded ETA to support a date" if eta_date is None else
                  f"Quoted date differs from the recorded ETA {eta_date.isoformat()}"))
    else:
        date_ok, label = True, "No arrival date claimed"
    checks.append(_check("dates_current", date_ok, label,
                         remediation="Use the recorded ETA, or say the arrival date is not confirmed yet.",
                         recorded_eta=eta_date.isoformat() if eta_date else None,
                         claimed=[d.isoformat() for d in body_dates]))

    # 11. consent / opt-out (spec §4.5)
    opted_out = bool(contact.get("opted_out"))
    checks.append(_check("consent", not opted_out,
                         "Contact has not opted out" if not opted_out else "Contact opted out of email",
                         remediation="Do not email this contact; handle it as an exception."))

    # 12. suppressed traffic is never auto-answered (spec §4.5): bounces, automated notices, lists, spam
    cls = (conversation.classification if conversation is not None else None) or ""
    suppressed = cls in SUPPRESSED_CLASSES
    checks.append(_check("no_auto_reply_class", not suppressed,
                         "Thread is ordinary correspondence" if not suppressed
                         else f"{cls} traffic is never auto-answered",
                         remediation="If a person really is waiting for an answer here, correct the classification first.",
                         classification=cls))

    # 13. dispute / sensitivity requires owner handling
    sensitive = (conversation.sensitivity if conversation is not None else "normal") in ("dispute",)
    checks.append(_check("sensitivity", not sensitive,
                         "Routine correspondence" if not sensitive else "Dispute — owner must handle this thread",
                         remediation="Owner review required before any reply goes out."))

    # 14. thread not taken over by a person (spec §4.3)
    taken = (conversation.state == "taken_over") if conversation is not None else False
    checks.append(_check("takeover", not taken, "Automation is active for this thread" if not taken else "Thread is taken over by a person",
                         remediation="Resume the thread before sending from AZKT."))

    # 15. source freshness (F5 / H11 / spec §12.4)
    fr = (account.get("freshness") or {}).get("state", "disconnected")
    fresh_ok = fr == "ok"
    checks.append(_check("source_freshness", fresh_ok,
                         "Mailbox coverage is current" if fresh_ok else f"Mailbox coverage is {fr}",
                         remediation="Wait for catch-up to finish (or reconnect) so the thread can be refreshed before sending.",
                         freshness=account.get("freshness")))

    # 16. no unresolved coverage gap over this thread
    gaps = list(facts.get("coverage_gaps") or [])
    checks.append(_check("coverage_gap", not gaps,
                         "No coverage gap on this account" if not gaps else f"{len(gaps)} unresolved coverage gap(s)",
                         remediation="Finish the catch-up resync so no inbound message is missing before replying.",
                         gaps=gaps))

    # 17. identity certainty: a proposed/ambiguous contact match blocks sending (spec §11.3, B04)
    match_state = (conversation.contact_match if conversation is not None else "unmatched")
    id_ok = match_state == "matched"
    checks.append(_check("identity_certainty", id_ok,
                         "Contact identity is established" if id_ok else f"Contact match is {match_state}",
                         remediation="Confirm which contact this is (or link the right one) before replying.",
                         contact_match=match_state))

    # 18. record links that are only proposed need review before a customer send (B04/B06)
    proposed = [l for l in (facts.get("links") or []) if l.get("match") != "matched"]
    checks.append(_check("record_links", not proposed,
                         "Linked records are confirmed" if not proposed else f"{len(proposed)} proposed link(s) need review",
                         remediation="Confirm or correct the vehicle/record link before sending.",
                         proposed=[{"kind": l.get("kind"), "id": l.get("id")} for l in proposed]))

    # 19. promises are recorded as commitments, not blocked
    promises = detect_promises(body)
    checks.append(_check("promises_recorded", True,
                         "No promise detected" if not promises else f"{len(promises)} promise(s) will be recorded as commitments",
                         blocking=False, promises=promises))

    # 20. permission: a customer send always needs exact approval on day one (spec §11.2)
    checks.append(_check("permission", True, "Customer sends require exact approval", blocking=False,
                         action_class="consequential"))
    return checks


def _norm_amount(a: str) -> str:
    try:
        from decimal import Decimal
        return str(Decimal(str(a).replace(",", "")).normalize())
    except Exception:  # noqa: BLE001
        return str(a)


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return _parse_date(str(value))


def blocking_failures(checks: list[dict]) -> list[dict]:
    return [c for c in checks if c.get("blocking") and not c.get("ok")]


def summarize(checks: list[dict]) -> dict:
    failing = blocking_failures(checks)
    return {"checks": checks, "failing": failing, "passed": [c for c in checks if c.get("ok")],
            "ok": not failing,
            "first_remediation": failing[0]["remediation"] if failing else ""}
