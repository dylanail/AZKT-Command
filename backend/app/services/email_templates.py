"""The four production email templates (spec §5.4, designs in
`docs/handoff/reference/design-v2/AZKT Email Reminders.dc.html`).

Design rules taken from the reference and kept literal:
  * subject = action + person + time. Never "Reminder:" — the inbox preview alone says what to do.
  * one 36px amber rule (#E6B35C) as the only brand mark; charcoal (#121212) ink and one charcoal button.
  * no colour except the status word (red overdue #B3261E, green paid #1E7F4F).
  * the button deep-links to the exact record; **text links open an authenticated review page** that only
    renders a confirm button. A GET never marks done / snoozes / approves (spec §5.4, invariant "No GET
    side effects", C06) — that is the one safe adaptation of the design's "without opening the app" note.
  * money detail only in owner email (`ctx["owner"] is True`).
  * a plain-text alternative is always produced.

`render(kind, ctx) -> (subject, text, html)`.
"""
from __future__ import annotations

import html as _html
from datetime import datetime, timezone
from typing import Any

from ..core.config import settings
from ..core.time import PHOENIX, ensure_aware, fmt_local, to_zone

KINDS = ("task_reminder", "overdue", "digest", "deposit_confirmed")

INK = "#121212"
MUTED = "#6e6e73"
FAINT = "#8e8e93"
LINE = "#eceef2"
BORDER = "#d8dbe2"
AMBER = "#E6B35C"
RED = "#B3261E"
GREEN = "#1E7F4F"
BODY_BG = "#e9ecf2"


# ── links ───────────────────────────────────────────────────────────────────
def origin() -> str:
    return (settings.PUBLIC_ORIGIN or "").rstrip("/")


def deep_link(entity_kind: str | None, entity_id: str | None, *, tab: str | None = None) -> str:
    """The charcoal button target: the exact record, never a generic list."""
    o = origin()
    if entity_kind == "task" and entity_id:
        return f"{o}/tasks/{entity_id}"
    if entity_kind == "opportunity" and entity_id:
        return f"{o}/sales/opportunities/{entity_id}"
    if entity_kind == "vehicle" and entity_id:
        return f"{o}/vehicles/{entity_id}?tab={tab or 'sale'}"
    if entity_kind == "import_request" and entity_id:
        return f"{o}/requests/{entity_id}"
    if entity_kind == "invoice" and entity_id:
        return f"{o}/finance/invoices/{entity_id}"
    if entity_kind == "approval" and entity_id:
        return f"{o}/approvals/{entity_id}"
    return f"{o}/tasks"


def review_link(task_id: str, action: str) -> str:
    """Secondary text link. Opens the authenticated task page with the action *pre-selected*;
    the page renders a confirm button and posts. The GET itself changes nothing."""
    return f"{origin()}/tasks/{task_id}?action={action}"


# ── small helpers ───────────────────────────────────────────────────────────
def _esc(v: Any) -> str:
    return _html.escape("" if v is None else str(v), quote=True)


def _tz(ctx: dict) -> str:
    return ctx.get("timezone") or PHOENIX


def when_label(dt: datetime | None, tz: str) -> str:
    return fmt_local(ensure_aware(dt), tz) if dt else "Not recorded"


def clock(dt: datetime | None, tz: str) -> str:
    if dt is None:
        return "no time set"
    z = to_zone(ensure_aware(dt), tz)
    label = {"America/Phoenix": "AZ", "Asia/Tokyo": "JST", "UTC": "UTC"}.get(tz, z.tzname() or tz)
    return f"{z.strftime('%H:%M')} {label}"


def day_phrase(dt: datetime | None, tz: str, now: datetime | None = None) -> str:
    """'Today, Sep 14' / 'yesterday, Sep 13' / 'Tue Sep 16' — relative to the reader's own day."""
    if dt is None:
        return "Not recorded"
    now = now or datetime.now(timezone.utc)
    d = to_zone(ensure_aware(dt), tz).date()
    today = to_zone(ensure_aware(now), tz).date()
    delta = (d - today).days
    label = to_zone(ensure_aware(dt), tz).strftime("%b %-d")
    if delta == 0:
        return f"Today, {label}"
    if delta == -1:
        return f"Yesterday, {label}"
    if delta == 1:
        return f"Tomorrow, {label}"
    return to_zone(ensure_aware(dt), tz).strftime("%a %b %-d")


def _short_when(dt: datetime | None, tz: str, now: datetime) -> str:
    """Subject-line time: 'yesterday 13:30', 'today 09:40', else 'Sep 13 · 13:30'."""
    if dt is None:
        return "no time set"
    z = to_zone(ensure_aware(dt), tz)
    delta = (z.date() - to_zone(ensure_aware(now), tz).date()).days
    word = {0: "today", -1: "yesterday", 1: "tomorrow"}.get(delta)
    return f"{word} {z.strftime('%H:%M')}" if word else f"{z.strftime('%b %-d')} · {z.strftime('%H:%M')}"


def humanize(minutes: float) -> str:
    minutes = int(abs(minutes))
    if minutes < 1:
        return "now"
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    if hours < 24:
        rem = minutes % 60
        return f"{hours} hour{'s' if hours != 1 else ''}" + (f" {rem} min" if rem else "")
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''}"


def lead_phrase(due_at: datetime | None, now: datetime) -> str:
    if due_at is None:
        return ""
    delta = (ensure_aware(due_at) - ensure_aware(now)).total_seconds() / 60
    if delta >= 0:
        return f"in {humanize(delta)}"
    return f"{humanize(delta)} ago"


# ── html chrome ─────────────────────────────────────────────────────────────
def _shell(status_word: str, status_colour: str, inner: str, footer: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"></head>
<body style="margin:0;background:{BODY_BG};color:{INK};font-family:'Instrument Sans',Helvetica,Arial,sans-serif;font-size:15px;line-height:1.5">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{BODY_BG};padding:24px 12px">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;background:#ffffff;border:1px solid {BORDER};border-radius:12px">
<tr><td style="padding:26px 22px 8px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
<td style="font-size:16px;font-weight:700;letter-spacing:.04em;color:{INK}">ARIZONA KEI TRUCKS</td>
<td align="right" style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:{status_colour};font-weight:600">{_esc(status_word)}</td>
</tr></table>
<div style="height:2px;width:36px;background:{AMBER};margin:18px 0"></div>
{inner}
</td></tr>
<tr><td style="padding:18px 22px 22px;font-size:12px;color:{FAINT};line-height:1.6">{footer}</td></tr>
</table>
</td></tr></table>
</body></html>"""


def _button(label: str, href: str) -> str:
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:18px 0 8px">'
            f'<tr><td style="background:{INK};border-radius:10px">'
            f'<a href="{_esc(href)}" style="display:inline-block;padding:12px 20px;color:#ffffff;text-decoration:none;'
            f'font-weight:600;font-size:14px">{_esc(label)}</a></td></tr></table>')


def _rows(pairs: list[tuple[str, str]]) -> str:
    if not pairs:
        return ""
    cells = "".join(
        f'<tr><td style="padding:6px 14px 6px 0;color:{MUTED};font-size:14px;vertical-align:top;white-space:nowrap">{_esc(k)}</td>'
        f'<td style="padding:6px 0;font-size:14px;color:{INK}">{_esc(v)}</td></tr>' for k, v in pairs)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="border-top:1px solid {LINE};border-bottom:1px solid {LINE};margin-top:4px">{cells}</table>')


def _headline(title: str, sub_html: str) -> str:
    return (f'<div style="font-size:22px;font-weight:700;letter-spacing:-.02em;line-height:1.25;color:{INK}">{_esc(title)}</div>'
            f'<div style="font-size:15px;color:#3a3a3c;margin-top:6px">{sub_html}</div>')


def _secondary(links: list[tuple[str, str]]) -> str:
    if not links:
        return ""
    items = " &nbsp;·&nbsp; ".join(f'<a href="{_esc(href)}" style="color:{MUTED}">{_esc(label)}</a>' for label, href in links)
    return f'<div style="font-size:13px;color:{MUTED};margin-top:6px">{items}</div>'


FOOTER_TAIL = "Arizona Kei Trucks · Phoenix, AZ · <a href=\"{o}/settings/reminders\" style=\"color:%s\">Reminder settings</a>" % FAINT


def _footer(note: str) -> str:
    return f"{_esc(note)}<br>{FOOTER_TAIL.format(o=origin())}"


# ── money ───────────────────────────────────────────────────────────────────
def money(amount: Any, currency: str | None) -> str:
    if amount in (None, ""):
        return "Not recorded"
    try:
        from decimal import Decimal
        a = Decimal(str(amount))
        return f"{a:,.2f} {currency or ''}".strip()
    except Exception:  # noqa: BLE001
        return f"{amount} {currency or ''}".strip()


# ── templates ───────────────────────────────────────────────────────────────
def _task_reminder(ctx: dict) -> tuple[str, str, str]:
    tz = _tz(ctx)
    now = ensure_aware(ctx.get("now")) or datetime.now(timezone.utc)
    due = ensure_aware(ctx.get("due_at"))
    title = (ctx.get("title") or "Task").strip()
    lead = lead_phrase(due, now)
    late = bool(ctx.get("late"))
    # subject: action + person + time. Never "Reminder:".
    subject = f"{title} {lead} · {clock(due, tz)}".strip() if lead else f"{title} · {clock(due, tz)}"
    if late:
        subject = f"{title} · {clock(due, tz)} (late reminder)"
    sub = f'{_esc(day_phrase(due, tz, now))} · <b style="font-weight:600;color:{INK}">{_esc(clock(due, tz))}</b>'
    if lead:
        sub += f" · {_esc(lead)}"
    if late:
        sub += f' · <span style="color:{RED};font-weight:600">late — sent after a service interruption</span>'
    pairs = _detail_pairs(ctx)
    inner = (_headline(title, sub) + _rows(pairs)
             + _button(ctx.get("button_label") or "Open in AZKT", ctx.get("link") or deep_link("task", ctx.get("task_id")))
             + _secondary([("Mark done", review_link(ctx["task_id"], "done")),
                           ("Snooze 1 hour", review_link(ctx["task_id"], "snooze")),
                           ("Reschedule", review_link(ctx["task_id"], "reschedule"))] if ctx.get("task_id") else []))
    note = ctx.get("offset_note") or "You set this reminder for this task. Change reminder timing in Sales › Tasks."
    html = _shell("Late reminder" if late else "Reminder", RED if late else MUTED, inner, _footer(note))
    text = _text_block(subject, title, [
        f"{day_phrase(due, tz, now)} · {clock(due, tz)}" + (f" · {lead}" if lead else ""),
        "This reminder is late — it was delayed by a service interruption." if late else "",
    ], pairs, ctx.get("link") or deep_link("task", ctx.get("task_id")),
        [("Mark done", review_link(ctx["task_id"], "done")), ("Snooze 1 hour", review_link(ctx["task_id"], "snooze"))]
        if ctx.get("task_id") else [], note)
    return subject, text, html


def _overdue(ctx: dict) -> tuple[str, str, str]:
    tz = _tz(ctx)
    now = ensure_aware(ctx.get("now")) or datetime.now(timezone.utc)
    due = ensure_aware(ctx.get("due_at"))
    title = (ctx.get("title") or "Task").strip()
    since = lead_phrase(due, now)
    subject = f"Overdue: {title[0].lower() + title[1:] if title else title} · was due {_short_when(due, tz, now)}"
    sub = (f'Was due <b style="font-weight:600;color:{RED}">{_esc(day_phrase(due, tz, now))} · {_esc(clock(due, tz))}</b>'
           + (f" · {_esc(since)}" if since else ""))
    pairs = _detail_pairs(ctx)
    inner = (_headline(title, sub) + _rows(pairs)
             + _button(ctx.get("button_label") or "Open in AZKT", ctx.get("link") or deep_link("task", ctx.get("task_id")))
             + _secondary([("Mark done", review_link(ctx["task_id"], "done")),
                           ("Reschedule", review_link(ctx["task_id"], "reschedule")),
                           ("Cancel task", review_link(ctx["task_id"], "cancel"))] if ctx.get("task_id") else []))
    note = "You'll get one overdue email per task, then it stays in your Tasks list."
    html = _shell("Overdue", RED, inner, _footer(note))
    text = _text_block(subject, title, [f"Was due {day_phrase(due, tz, now)} · {clock(due, tz)}" + (f" · {since}" if since else "")],
                       pairs, ctx.get("link") or deep_link("task", ctx.get("task_id")),
                       [("Mark done", review_link(ctx["task_id"], "done")),
                        ("Reschedule", review_link(ctx["task_id"], "reschedule"))] if ctx.get("task_id") else [], note)
    return subject, text, html


def _digest(ctx: dict) -> tuple[str, str, str]:
    tz = _tz(ctx)
    now = ensure_aware(ctx.get("now")) or datetime.now(timezone.utc)
    overdue = list(ctx.get("overdue") or [])
    today = list(ctx.get("today") or [])
    approvals = list(ctx.get("approvals") or [])
    tomorrow = list(ctx.get("tomorrow") or [])
    bits = []
    if today:
        bits.append(f"{len(today)} task{'s' if len(today) != 1 else ''}")
    if overdue:
        bits.append(f"{len(overdue)} overdue")
    if approvals:
        bits.append(f"{len(approvals)} approval{'s' if len(approvals) != 1 else ''} waiting")
    subject = "Today: " + ", ".join(bits) if bits else "Today: nothing scheduled"
    lead = ctx.get("greeting") or _digest_greeting(len(today), len(overdue), len(approvals))
    rows_html, rows_text = [], []
    for item in overdue:
        rows_html.append(_digest_row("Overdue", item.get("text", ""), RED))
        rows_text.append(f"  Overdue   {item.get('text','')}")
    for item in today:
        rows_html.append(_digest_row(item.get("time", ""), item.get("text", ""), INK))
        rows_text.append(f"  {item.get('time',''):<9} {item.get('text','')}")
    for item in approvals:
        rows_html.append(_digest_row("Approval", item.get("text", ""), MUTED))
        rows_text.append(f"  Approval  {item.get('text','')}")
    table = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
             f'style="border-top:1px solid {LINE};margin-top:4px">{"".join(rows_html)}</table>')
    tail = ""
    if tomorrow:
        tail = (f'<div style="font-size:14px;color:#3a3a3c;margin-top:16px">Tomorrow: '
                f'{_esc("; ".join(t.get("text", "") for t in tomorrow))}.</div>')
    inner = (f'<div style="font-size:22px;font-weight:700;letter-spacing:-.02em;line-height:1.25;color:{INK}">{_esc(lead)}</div>'
             + table + tail + _button("Open Tasks", f"{origin()}/tasks"))
    note = "Sent only on days with an open task, overdue item or waiting approval."
    html = _shell(to_zone(now, tz).strftime("%A, %b %-d"), MUTED, inner, _footer(note))
    text = "\n".join([subject, "", lead, ""] + rows_text
                     + ([""] + [f"Tomorrow: {'; '.join(t.get('text','') for t in tomorrow)}."] if tomorrow else [])
                     + ["", f"Open Tasks: {origin()}/tasks", "", note])
    return subject, text, html


def _digest_greeting(n_today: int, n_overdue: int, n_appr: int) -> str:
    if not (n_today or n_overdue or n_appr):
        return "Good morning. Nothing scheduled."
    words = {0: "Nothing", 1: "One thing", 2: "Two things", 3: "Three things"}.get(n_today, f"{n_today} things")
    s = f"Good morning. {words} today"
    if n_overdue:
        s += f", {'one' if n_overdue == 1 else n_overdue} already late"
    if n_appr:
        s += f", {'one' if n_appr == 1 else n_appr} waiting on you"
    return s + "."


def _digest_row(left: str, text: str, colour: str) -> str:
    return (f'<tr><td style="padding:12px 14px 12px 0;border-bottom:1px solid {LINE};font-size:14px;font-weight:600;'
            f'color:{colour};white-space:nowrap;vertical-align:top">{_esc(left)}</td>'
            f'<td style="padding:12px 0;border-bottom:1px solid {LINE};font-size:14px;color:{INK}">{_esc(text)}</td></tr>')


def _deposit_confirmed(ctx: dict) -> tuple[str, str, str]:
    tz = _tz(ctx)
    now = ensure_aware(ctx.get("now")) or datetime.now(timezone.utc)
    at = ensure_aware(ctx.get("confirmed_at")) or now
    person = ctx.get("person") or "The buyer"
    what = ctx.get("what") or ctx.get("vehicle") or ctx.get("request") or "the obligation"
    subject = f"Deposit paid · {person} · {what}"
    sub = (f'{_esc(day_phrase(at, tz, now))} · <b style="font-weight:600;color:{INK}">{_esc(clock(at, tz))}</b> · '
           f'{_esc(ctx.get("moved_to") or "handed off")}')
    pairs: list[tuple[str, str]] = []
    if ctx.get("vehicle"):
        pairs.append(("Vehicle", ctx["vehicle"]))
    if ctx.get("request"):
        pairs.append(("Request", ctx["request"]))
    if ctx.get("owner"):   # money detail is only sent to the owner's address
        pairs.append(("Deposit", money(ctx.get("amount"), ctx.get("currency"))
                      + (f" · {ctx['receipt_ref']}" if ctx.get("receipt_ref") else " · no provider receipt recorded")))
    pairs.append(("Next", ctx.get("next_step") or "Open the record for the next step"))
    if ctx.get("cancelled_tasks"):
        n = len(ctx["cancelled_tasks"])
        pairs.append(("Open tasks", f"{n} cancelled automatically (deposit chase)"))
    inner = (_headline(f"{person} paid the deposit", sub) + _rows(pairs)
             + _button(ctx.get("button_label") or "Open the record", ctx.get("link") or deep_link(ctx.get("entity_kind"), ctx.get("entity_id"))))
    note = "Money detail is only sent to the owner's address."
    html = _shell("Deposit paid", GREEN, inner, _footer(note))
    text = _text_block(subject, f"{person} paid the deposit",
                       [f"{day_phrase(at, tz, now)} · {clock(at, tz)} · {ctx.get('moved_to') or 'handed off'}"],
                       pairs, ctx.get("link") or deep_link(ctx.get("entity_kind"), ctx.get("entity_id")), [], note)
    return subject, text, html


def _detail_pairs(ctx: dict) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for label, key in (("Pipeline", "pipeline"), ("Vehicle", "vehicle"), ("Looking for", "looking_for"),
                       ("Contact", "contact"), ("Owner", "owner_name"), ("Your note", "note")):
        v = ctx.get(key)
        if v:
            pairs.append((label, str(v)))
    if ctx.get("owner") and ctx.get("amount") not in (None, ""):
        pairs.append(("Budget", money(ctx.get("amount"), ctx.get("currency"))))
    return pairs


def _text_block(subject: str, title: str, lines: list[str], pairs: list[tuple[str, str]], link: str,
                secondary: list[tuple[str, str]], note: str) -> str:
    out = [subject, "", title]
    out += [ln for ln in lines if ln]
    if pairs:
        out.append("")
        out += [f"{k}: {v}" for k, v in pairs]
    out += ["", f"Open in AZKT: {link}"]
    if secondary:
        out.append("")
        out += [f"{label}: {href}" for label, href in secondary]
        out.append("These links open AZKT and ask you to confirm; nothing changes until you do.")
    out += ["", note, "Arizona Kei Trucks · Phoenix, AZ"]
    return "\n".join(out)


RENDERERS = {"task_reminder": _task_reminder, "overdue": _overdue, "digest": _digest,
             "deposit_confirmed": _deposit_confirmed}


def render(kind: str, ctx: dict) -> tuple[str, str, str]:
    """(subject, text, html) for one of the four designed emails."""
    fn = RENDERERS.get(kind)
    if fn is None:
        raise ValueError(f"unknown email template {kind!r}; expected one of {KINDS}")
    subject, text, html = fn(dict(ctx or {}))
    assert not subject.lower().startswith("reminder:"), "subject rule: action + person + time, never 'Reminder:'"
    return subject, text, html
