"""Google Calendar adapter (spec §5.3, §11.2, §12.3).

One interface, two implementations:

  * ``GoogleCalendarAdapter`` — live Calendar v3 REST calls over :class:`adapters.google_oauth.GoogleApi`.
  * ``FakeCalendar``         — the same facade in memory. Every test uses the fake; no live provider
    call is ever made from a test.

Contract notes the calendar slice depends on:

* Typed errors only. Everything raises :class:`core.errors.ProviderError` with a ``kind`` of
  ``invalid_input | permission_denied | auth_expired | rate_limited | transient | schema_changed |
  conflict | unknown_result``. An ``events.insert`` that collides with an id AZKT already used is
  ``conflict`` with code ``duplicate_event`` so the caller adopts the existing entry instead of
  making a second one.
* Capabilities the connection has not been granted return :class:`core.errors.Unsupported`, never an
  empty success (spec §12.3). Calendar **writes** sit behind a separate explicit owner capability
  (spec §11.2 "calendar writes: separate explicit capability permission"), which is why
  :func:`adapter_for` takes ``writes_enabled`` and refuses before it hands back any client.
* A write whose result was lost raises :class:`UnknownWriteResult` carrying the event id AZKT minted.
  The caller marks the task ``unknown`` and reconciles with :meth:`get_event` before any retry
  (spec §11.5.4) — the queue saying "the job ran once" never makes a calendar write exactly-once.
* Event ids are minted here, deterministically, from the task id (:func:`event_id_for`). Calendar v3
  is one of the few Google APIs that lets the client choose the resource id, and that is the whole
  idempotency story: a replayed job inserts the *same* id, so the provider itself refuses the second
  entry rather than AZKT having to trust its own bookkeeping.
* Reminder overrides and attendees are deliberately absent from every payload this adapter sends for
  AZKT: reminders belong to AZKT (spec §5.4) and adding a customer as an attendee would be a customer
  send, which needs its own exact approval (spec §11.2). See :func:`event_body_fields`.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..core.destinations import assert_destination_allowed
from ..core.errors import ProviderError, Unsupported
from ..core.ids import sha256_hex
from ..core.time import PHOENIX, ensure_aware

log = logging.getLogger("azkt.calendar")

API = "https://www.googleapis.com/calendar/v3"
READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
PRIMARY = "primary"

ERROR_KINDS = ("invalid_input", "permission_denied", "auth_expired", "rate_limited", "transient",
               "schema_changed", "conflict", "unknown_result")
OPERATIONS = ("list_calendars", "list_events", "get_event", "insert_event", "patch_event", "delete_event")
WRITE_OPERATIONS = ("insert_event", "patch_event", "delete_event")
# Google Calendar ids are base32hex: the characters a–v and 0–9, 5–1024 long.
_B32HEX = set("abcdefghijklmnopqrstuv0123456789")
# Fields AZKT owns on a managed event. Everything else on the entry — colour, a guest the owner added
# by hand, a Meet link — belongs to whoever put it there and is never sent, so a patch cannot erase it.
AZKT_FIELDS = ("summary", "description", "start", "end", "status", "transparency", "reminders",
               "extendedProperties", "source")


def error_kind(exc: Exception) -> str:
    """The typed kind of a ProviderError (``unknown_result`` for anything else)."""
    if isinstance(exc, ProviderError):
        return str((exc.detail or {}).get("kind") or "unknown_result")
    return "unknown_result"


def _fail(kind: str, message: str, **extra) -> ProviderError:
    return ProviderError(message, kind=kind, provider="google_calendar", **extra)


class UnknownWriteResult(Exception):
    """The calendar may have accepted the write but the result was lost (timeout, 5xx, dropped socket).

    Carries the event id AZKT minted so the caller can look the entry up by that exact id before
    deciding whether a retry would duplicate it."""

    def __init__(self, message: str, provider_ref: str | None = None, operation: str | None = None):
        super().__init__(message)
        self.provider_ref = provider_ref
        self.operation = operation


def event_id_for(task_id: str) -> str:
    """The deterministic calendar event id for a task.

    Derived from the task id rather than stored-and-hoped-for: after a crash, a lost response or a
    restored backup, the same task always resolves to the same event id, so the reconciliation query
    is a plain GET and a replayed insert collides at the provider instead of creating a twin.
    AZKT ids are UUID4 (hex, which is inside the base32hex alphabet); any other id shape is hashed so
    the result still only contains characters Calendar accepts.
    """
    raw = (task_id or "").lower().replace("-", "")
    if 5 <= len(raw) <= 1000 and all(c in _B32HEX for c in raw):
        return f"task{raw}"
    return f"task{sha256_hex(task_id or '')[:40]}"


# ── normalization ────────────────────────────────────────────────────────────
def event_time(dt: datetime, tz: str | None = None) -> dict:
    """One Calendar time field: the UTC instant **and** the IANA zone it was scheduled in.

    Both are sent on purpose (spec §5.3 "store UTC instants plus IANA time zones"). The offset in
    ``dateTime`` fixes the instant, so a DST change can never move the appointment; ``timeZone`` is
    what Calendar displays and what a later edit in Google resolves against, so a Phoenix task and a
    Tokyo task read correctly to the people looking at them."""
    at = ensure_aware(dt).astimezone(timezone.utc)
    return {"dateTime": at.isoformat().replace("+00:00", "Z"), "timeZone": tz or PHOENIX}


def parse_time(field: Any) -> datetime | None:
    """A Calendar start/end field → a UTC instant. An all-day entry (``date``) starts at local midnight."""
    if not isinstance(field, dict):
        return None
    raw = field.get("dateTime") or field.get("date")
    if not raw:
        return None
    try:
        if "T" not in str(raw):
            zone = field.get("timeZone") or "UTC"
            from zoneinfo import ZoneInfo
            d = datetime.fromisoformat(f"{raw}T00:00:00")
            return d.replace(tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)
        return ensure_aware(datetime.fromisoformat(str(raw).replace("Z", "+00:00"))).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def normalize_event(raw: dict) -> dict:
    """Calendar event resource → the shape the calendar service and the read API store."""
    raw = raw or {}
    start, end = parse_time(raw.get("start")), parse_time(raw.get("end"))
    private = ((raw.get("extendedProperties") or {}).get("private") or {})
    return {
        "id": raw.get("id"),
        "status": raw.get("status") or "confirmed",
        "summary": raw.get("summary") or "",
        "description": raw.get("description") or "",
        "start": start,
        "end": end,
        "all_day": bool((raw.get("start") or {}).get("date")),
        "timezone": (raw.get("start") or {}).get("timeZone"),
        "transparency": raw.get("transparency") or "opaque",
        "html_link": raw.get("htmlLink"),
        "etag": raw.get("etag"),
        "updated": raw.get("updated"),
        "recurring_event_id": raw.get("recurringEventId"),
        "attendee_count": len(raw.get("attendees") or []),
        "organizer": (raw.get("organizer") or {}).get("email"),
        "azkt_task_id": private.get("azkt_task_id"),
        "azkt_revision": private.get("azkt_revision"),
        "mine": bool(private.get("azkt_task_id")),
    }


def overlaps(a_start: datetime | None, a_end: datetime | None,
             b_start: datetime | None, b_end: datetime | None) -> bool:
    """Half-open overlap: two appointments that merely touch (10:00–11:00 and 11:00–12:00) do not clash."""
    if not (a_start and a_end and b_start and b_end):
        return False
    return ensure_aware(a_start) < ensure_aware(b_end) and ensure_aware(b_start) < ensure_aware(a_end)


def event_body_fields(*, summary: str, description: str, start: datetime, end: datetime, tz: str,
                      task_id: str, revision: int, source_url: str | None = None) -> dict:
    """The exact payload AZKT writes for a task, and nothing else.

    Two omissions are the point of this function, not oversights:

    * ``reminders`` is pinned to ``useDefault: false`` with **no** overrides. AZKT already sends the
      task reminder the owner configured (spec §5.4); a Google default popup would be a second,
      differently-timed nudge for the same appointment that nobody chose.
    * ``attendees`` never appears — not even empty. Adding the customer would be an outbound customer
      send (Calendar mails the invitation), which needs its own exact approval (spec §11.2); sending
      an empty list on a patch would instead delete a guest the owner added by hand in Google.
    """
    body: dict[str, Any] = {
        "summary": summary,
        "description": description,
        "start": event_time(start, tz),
        "end": event_time(end, tz),
        "status": "confirmed",
        "transparency": "opaque",
        "reminders": {"useDefault": False, "overrides": []},
        "extendedProperties": {"private": {"azkt_task_id": task_id, "azkt_revision": str(revision),
                                           "azkt_source": "azkt"}},
    }
    if source_url:
        body["source"] = {"title": "Open in AZKT", "url": source_url}
    return body


# ── live adapter ─────────────────────────────────────────────────────────────
class GoogleCalendarAdapter:
    """Live Google Calendar. Every provider failure is mapped onto a typed ProviderError kind."""

    provider = "google_calendar"
    kind = "live"
    is_fake = False

    def __init__(self, db, connection, *, writes_enabled: bool = False):
        self.db = db
        self.conn = connection
        self.writes_enabled = writes_enabled
        from .google_oauth import GoogleApi
        self.api = GoogleApi(db, connection)

    # capability discovery (spec §12.3)
    def capabilities(self) -> dict:
        granted = " ".join((self.conn.granted_scopes if self.conn is not None else None) or [])
        return {
            "read": READ_SCOPE in granted or WRITE_SCOPE in granted or not granted,
            # the granted scope proves Google would accept a write; the owner capability decides
            # whether AZKT is allowed to make one at all (spec §11.2)
            "write": bool(self.writes_enabled) and (WRITE_SCOPE in granted or not granted),
        }

    def _require(self, capability: str) -> None:
        if not self.capabilities().get(capability):
            raise Unsupported(f"calendar capability '{capability}' is not enabled for this connection",
                              capability=capability, provider=self.provider,
                              setup_blocked=f"calendar.{capability}")

    def _assert_writable(self, op: str, calendar_id: str) -> None:
        """H08: this adapter really writes, so outside production it may only touch an allowlisted
        calendar. Every write goes through here before a byte leaves the process. `FakeCalendar` never
        comes here — nothing leaves it, exactly like the memory email transport."""
        assert_destination_allowed("calendar", calendar_id or PRIMARY)

    def _url(self, calendar_id: str, suffix: str = "") -> str:
        from urllib.parse import quote
        return f"{API}/calendars/{quote(calendar_id or PRIMARY, safe='')}/events{suffix}"

    async def _read(self, url: str, params: dict | None = None) -> dict:
        return await self.api.request("GET", url, params={k: v for k, v in (params or {}).items() if v is not None})

    async def _write(self, method: str, url: str, *, op: str, event_id: str | None = None, **kw) -> dict:
        """One write call, with the two failure modes that matter kept apart.

        A refusal the provider actually returned (400/403/429) holds nothing and is reported as it is.
        A request that never came back with an answer — a timeout, a dropped connection, a 5xx after
        the server may already have stored the event — is `UnknownWriteResult`: the caller must look
        the event up before it may try again (spec §11.5.4)."""
        try:
            return await self.api.request(method, url, **kw)
        except ProviderError as e:
            k = error_kind(e)
            if k == "transient":
                raise UnknownWriteResult(f"google calendar returned a server error during {op}: {e.message}",
                                         provider_ref=event_id, operation=op) from e
            if "409" in str(e.message):
                raise _fail("conflict", f"an event with id {event_id} already exists on this calendar",
                            code="duplicate_event", event_id=event_id) from e
            raise
        except httpx.HTTPError as e:
            raise UnknownWriteResult(f"the connection to google calendar failed during {op}: {e}",
                                     provider_ref=event_id, operation=op) from e

    # ── reads ────────────────────────────────────────────────────────────
    async def list_calendars(self) -> list[dict]:
        self._require("read")
        body = await self._read(f"{API}/users/me/calendarList", {"minAccessRole": "reader", "maxResults": 100})
        return [{"id": c.get("id"), "summary": c.get("summary"), "primary": bool(c.get("primary")),
                 "access_role": c.get("accessRole"), "timezone": c.get("timeZone")}
                for c in (body.get("items") or [])]

    async def list_events(self, calendar_id: str, *, time_min: datetime, time_max: datetime,
                          max_results: int = 50, single_events: bool = True,
                          show_deleted: bool = False) -> list[dict]:
        """Events overlapping [time_min, time_max). `single_events` expands recurrences so a weekly
        standing meeting is seen on the day it actually occupies, not only on its first instance."""
        self._require("read")
        body = await self._read(self._url(calendar_id), {
            "timeMin": ensure_aware(time_min).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "timeMax": ensure_aware(time_max).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "maxResults": max_results, "singleEvents": "true" if single_events else "false",
            "showDeleted": "true" if show_deleted else "false",
            "orderBy": "startTime" if single_events else None})
        return [normalize_event(e) for e in (body.get("items") or [])]

    async def get_event(self, calendar_id: str, event_id: str) -> dict | None:
        """The event, or None when the calendar does not have it. This is the reconciliation query
        after an unknown write result — it is a read, so it is safe to run before every retry."""
        self._require("read")
        try:
            raw = await self._read(self._url(calendar_id, f"/{event_id}"))
        except ProviderError as e:
            if "404" in str(e.message):
                return None
            raise
        return normalize_event(raw)

    # ── writes (capability-gated, H08-guarded) ───────────────────────────
    async def insert_event(self, calendar_id: str, body: dict, *, event_id: str | None = None) -> dict:
        self._require("write")
        self._assert_writable("insert_event", calendar_id)
        payload = {k: v for k, v in (body or {}).items() if k in AZKT_FIELDS}
        if event_id:
            payload["id"] = event_id
        raw = await self._write("POST", self._url(calendar_id), op="insert_event", event_id=event_id,
                                json=payload, params={"sendUpdates": "none"})
        return normalize_event(raw)

    async def patch_event(self, calendar_id: str, event_id: str, body: dict) -> dict:
        """PATCH, never PUT: only the fields AZKT owns are sent, so a colour, a guest or a Meet link
        the owner added in Google survives an AZKT reschedule."""
        self._require("write")
        self._assert_writable("patch_event", calendar_id)
        payload = {k: v for k, v in (body or {}).items() if k in AZKT_FIELDS}
        raw = await self._write("PATCH", self._url(calendar_id, f"/{event_id}"), op="patch_event",
                                event_id=event_id, json=payload, params={"sendUpdates": "none"})
        return normalize_event(raw)

    async def delete_event(self, calendar_id: str, event_id: str) -> dict:
        self._require("write")
        self._assert_writable("delete_event", calendar_id)
        try:
            await self._write("DELETE", self._url(calendar_id, f"/{event_id}"), op="delete_event",
                              event_id=event_id, params={"sendUpdates": "none"})
        except ProviderError as e:
            if "404" in str(e.message) or "410" in str(e.message):
                # already gone: the intended end state, reported as such rather than as a failure
                return {"deleted": True, "event_id": event_id, "already_absent": True}
            raise
        return {"deleted": True, "event_id": event_id, "already_absent": False}

    # explicitly unsupported: this integration never invites anyone and never changes sharing
    async def invite(self, *_a, **_kw):
        raise Unsupported("AZKT never adds attendees to a calendar event; inviting a customer is a "
                          "customer send and needs its own exact approval")

    async def share_calendar(self, *_a, **_kw):
        raise Unsupported("AZKT never changes who a calendar is shared with")


# ── fake adapter used by every test ──────────────────────────────────────────
class FakeCalendar:
    """In-memory Google Calendar with the same facade, the same typed errors and no network.

    Test knobs mirror the failure modes the acceptance scenarios need: a provider refusal
    (`fail_next_write`), a write the calendar accepted just before the answer was lost
    (`lose_next_write_result`), an operation the connection may not perform (`unsupported_ops`) and
    pre-existing entries for conflict detection (`add_event`).
    """

    provider = "google_calendar"
    kind = "fake"
    is_fake = True

    def __init__(self, *, calendar_id: str = "info@azkeitrucks.com", capabilities: dict | None = None):
        self.default_calendar_id = calendar_id
        self.events: dict[str, dict[str, dict]] = {calendar_id: {}}
        self.calendars: list[dict] = [{"id": calendar_id, "summary": "Arizona Kei Trucks", "primary": True,
                                       "access_role": "owner", "timezone": PHOENIX}]
        self._caps = dict(capabilities or {"read": True, "write": True})
        # observability for tests
        self.calls: list[tuple[str, str]] = []
        self.inserted: list[str] = []
        self.patched: list[str] = []
        self.deleted: list[str] = []
        self.unsupported_ops: set[str] = set()
        self.fail_next_write: Exception | None = None
        self.lose_next_write_result: bool = False
        self._seq = 0

    # -- fixtures ---------------------------------------------------------
    def _cal(self, calendar_id: str) -> dict:
        return self.events.setdefault(calendar_id or self.default_calendar_id, {})

    def add_event(self, *, calendar_id: str | None = None, event_id: str | None = None, summary: str = "Busy",
                  start: datetime, end: datetime, tz: str = PHOENIX, status: str = "confirmed",
                  transparency: str = "opaque") -> dict:
        """Somebody else's entry already on the calendar (the conflict fixture)."""
        cid = calendar_id or self.default_calendar_id
        self._seq += 1
        eid = event_id or f"existing{self._seq}"
        row = {"id": eid, "status": status, "summary": summary, "description": "",
               "start": event_time(start, tz), "end": event_time(end, tz), "transparency": transparency,
               "htmlLink": f"https://calendar.google.com/event?eid={eid}", "etag": f"etag-{self._seq}",
               "updated": datetime.now(timezone.utc).isoformat()}
        self._cal(cid)[eid] = row
        return row

    def stored(self, calendar_id: str | None = None) -> list[dict]:
        return [normalize_event(e) for e in self._cal(calendar_id or self.default_calendar_id).values()]

    def live_count(self, calendar_id: str | None = None) -> int:
        return len([e for e in self._cal(calendar_id or self.default_calendar_id).values()
                    if e.get("status") != "cancelled"])

    # -- facade -----------------------------------------------------------
    def capabilities(self) -> dict:
        return dict(self._caps)

    def _require(self, capability: str) -> None:
        if not self._caps.get(capability):
            raise Unsupported(f"calendar capability '{capability}' is not enabled for this connection",
                              capability=capability, provider=self.provider,
                              setup_blocked=f"calendar.{capability}")

    def _guard(self, op: str, calendar_id: str) -> None:
        self.calls.append((op, calendar_id or self.default_calendar_id))
        if op in self.unsupported_ops:
            raise Unsupported(f"{op} is not available on this connection", capability=op, provider=self.provider)
        if op in WRITE_OPERATIONS:
            self._require("write")
            if self.fail_next_write is not None:
                exc, self.fail_next_write = self.fail_next_write, None
                raise exc
        else:
            self._require("read")

    def _maybe_lose(self, op: str, event_id: str | None) -> None:
        """The calendar stored the change and then the answer was lost (spec §11.5.4)."""
        if self.lose_next_write_result:
            self.lose_next_write_result = False
            raise UnknownWriteResult(f"the connection to google calendar failed during {op}",
                                     provider_ref=event_id, operation=op)

    async def list_calendars(self) -> list[dict]:
        self._guard("list_calendars", self.default_calendar_id)
        return [dict(c) for c in self.calendars]

    async def list_events(self, calendar_id: str, *, time_min: datetime, time_max: datetime,
                          max_results: int = 50, single_events: bool = True,
                          show_deleted: bool = False) -> list[dict]:
        self._guard("list_events", calendar_id)
        out = []
        for raw in self._cal(calendar_id).values():
            if raw.get("status") == "cancelled" and not show_deleted:
                continue
            ev = normalize_event(raw)
            if overlaps(ev["start"], ev["end"], time_min, time_max):
                out.append(ev)
        out.sort(key=lambda e: (e["start"] or datetime.min.replace(tzinfo=timezone.utc), e["id"] or ""))
        return out[:max_results]

    async def get_event(self, calendar_id: str, event_id: str) -> dict | None:
        self._guard("get_event", calendar_id)
        raw = self._cal(calendar_id).get(event_id)
        return normalize_event(raw) if raw else None

    async def insert_event(self, calendar_id: str, body: dict, *, event_id: str | None = None) -> dict:
        self._guard("insert_event", calendar_id)
        cal = self._cal(calendar_id)
        eid = event_id or f"gen{len(cal) + 1}"
        if eid in cal and cal[eid].get("status") != "cancelled":
            raise _fail("conflict", f"an event with id {eid} already exists on this calendar",
                        code="duplicate_event", event_id=eid)
        row = {k: v for k, v in (body or {}).items() if k in AZKT_FIELDS}
        row.update({"id": eid, "status": row.get("status") or "confirmed",
                    "htmlLink": f"https://calendar.google.com/event?eid={eid}",
                    "etag": f"etag-{len(cal) + 1}", "updated": datetime.now(timezone.utc).isoformat()})
        cal[eid] = row
        self.inserted.append(eid)
        self._maybe_lose("insert_event", eid)
        return normalize_event(row)

    async def patch_event(self, calendar_id: str, event_id: str, body: dict) -> dict:
        self._guard("patch_event", calendar_id)
        cal = self._cal(calendar_id)
        row = cal.get(event_id)
        if row is None:
            raise _fail("invalid_input", f"google calendar has no event {event_id} (404)")
        for k, v in (body or {}).items():
            if k in AZKT_FIELDS:
                row[k] = v
        row["updated"] = datetime.now(timezone.utc).isoformat()
        self.patched.append(event_id)
        self._maybe_lose("patch_event", event_id)
        return normalize_event(row)

    async def delete_event(self, calendar_id: str, event_id: str) -> dict:
        self._guard("delete_event", calendar_id)
        cal = self._cal(calendar_id)
        existed = cal.pop(event_id, None)
        self.deleted.append(event_id)
        self._maybe_lose("delete_event", event_id)
        return {"deleted": True, "event_id": event_id, "already_absent": existed is None}

    async def invite(self, *_a, **_kw):
        raise Unsupported("AZKT never adds attendees to a calendar event; inviting a customer is a "
                          "customer send and needs its own exact approval")

    async def share_calendar(self, *_a, **_kw):
        raise Unsupported("AZKT never changes who a calendar is shared with")


# ── factory ──────────────────────────────────────────────────────────────────
_ADAPTER: Any | None = None


def set_adapter(adapter: Any | None) -> None:
    """Tests and the setup preview install the active client here (never a live call in tests)."""
    global _ADAPTER
    _ADAPTER = adapter


def current_adapter() -> Any | None:
    return _ADAPTER


def adapter_for(db, conn, *, writes_enabled: bool = False, write: bool = True) -> Any:
    """The active Calendar client, or `Unsupported` naming exactly what is missing.

    The setup gate is evaluated **before** the fixture short-circuit, unlike the other adapters. A
    calendar write needs two independent things — a connected Google account and the owner's explicit
    capability switch (spec §11.2) — and a test has to be able to prove that a blocked write really
    writes nothing, which it cannot do if installing a fake also bypasses the gate.
    """
    if conn is None or conn.status == "disconnected":
        raise Unsupported("Google Calendar is not connected; connect it under Settings → Calendar",
                          setup_blocked="calendar.oauth")
    if conn.status in ("expired", "error"):
        raise Unsupported(f"the Google Calendar connection needs attention ({conn.status}); reconnect it "
                          "under Settings → Calendar", setup_blocked="calendar.oauth")
    granted = list(conn.granted_scopes or [])
    if not write:
        if granted and READ_SCOPE not in granted and WRITE_SCOPE not in granted:
            raise Unsupported("the Google Calendar connection was granted no calendar scope; reconnect it",
                              setup_blocked="calendar.read_scope")
    else:
        if not writes_enabled:
            raise Unsupported("calendar writes are off: turn on “Put calls and meetings on the calendar” "
                              "under Settings → Calendar", setup_blocked="calendar.capability")
        if granted and WRITE_SCOPE not in granted:
            raise Unsupported("the Google Calendar connection was granted read access only; reconnect it and "
                              "allow AZKT to create events", setup_blocked="calendar.write_scope")
    if _ADAPTER is not None:
        return _ADAPTER
    from ..core.config import settings
    if settings.ENV == "test":
        # a live client is never constructed in tests: install a fixture with `set_adapter`
        raise Unsupported("live calendar calls are disabled in tests; install a fixture adapter")
    return GoogleCalendarAdapter(db, conn, writes_enabled=writes_enabled)


def default_window(task_type: str, minutes: dict | None = None) -> timedelta:
    """How long an appointment lasts when the task only says when it starts."""
    table = {"call": 30, "meeting": 60, "follow_up": 30}
    table.update({k: int(v) for k, v in (minutes or {}).items() if str(v).isdigit()})
    return timedelta(minutes=table.get(task_type, 30))


_SAFE_SUMMARY = re.compile(r"\s+")


def clean_summary(text: str, limit: int = 250) -> str:
    return _SAFE_SUMMARY.sub(" ", (text or "").strip())[:limit] or "AZKT appointment"
