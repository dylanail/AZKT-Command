"""Time helpers. Store UTC instants; display in IANA zones (spec §5.3)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

PHOENIX = "America/Phoenix"
TOKYO = "Asia/Tokyo"
UTC = timezone.utc

REMINDER_OFFSETS = {"at": 0, "15m": 15, "1h": 60, "1d": 60 * 24}


def now() -> datetime:
    return datetime.now(UTC)


def ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def to_zone(dt: datetime, tz: str = PHOENIX) -> datetime:
    return ensure_aware(dt).astimezone(ZoneInfo(tz))


def local_to_utc(d: date, t: time, tz: str = PHOENIX) -> datetime:
    return datetime.combine(d, t, tzinfo=ZoneInfo(tz)).astimezone(UTC)


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return ensure_aware(dt)


def reminder_offset_minutes(kind: str | None, custom_minutes: int | None = None) -> int | None:
    """Minutes before the scheduled time a reminder fires; None = no reminder."""
    if not kind:
        return None
    if kind == "custom":
        return int(custom_minutes or 30)
    if kind in REMINDER_OFFSETS:
        return REMINDER_OFFSETS[kind]
    raise ValueError(f"unknown reminder offset {kind!r}")


def reminder_fire_at(due_at: datetime, kind: str | None, custom_minutes: int | None = None) -> datetime | None:
    mins = reminder_offset_minutes(kind, custom_minutes)
    if mins is None:
        return None
    return ensure_aware(due_at) - timedelta(minutes=mins)


def fmt_local(dt: datetime | None, tz: str = PHOENIX, with_zone: bool = True) -> str:
    if dt is None:
        return "Not recorded"
    z = to_zone(dt, tz)
    label = {"America/Phoenix": "AZ", "Asia/Tokyo": "JST", "UTC": "UTC"}.get(tz, z.tzname() or tz)
    s = z.strftime("%a %b %-d · %H:%M")
    return f"{s} {label}" if with_zone else s


def period_bounds(kind: str, tz: str = PHOENIX, ref: datetime | None = None,
                  start: date | None = None, end: date | None = None) -> tuple[datetime, datetime]:
    """Reporting period → [from, to) as UTC instants. kind: month|7d|30d|custom."""
    ref = to_zone(ref or now(), tz)
    zone = ZoneInfo(tz)
    if kind == "month":
        first = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return first.astimezone(UTC), nxt.astimezone(UTC)
    if kind in ("7d", "30d"):
        days = 7 if kind == "7d" else 30
        end_local = ref.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return (end_local - timedelta(days=days)).astimezone(UTC), end_local.astimezone(UTC)
    if kind == "custom" and start and end:
        a = datetime.combine(start, time.min, tzinfo=zone)
        b = datetime.combine(end, time.min, tzinfo=zone) + timedelta(days=1)
        return a.astimezone(UTC), b.astimezone(UTC)
    raise ValueError("period requires kind in month|7d|30d|custom (custom needs start/end)")
