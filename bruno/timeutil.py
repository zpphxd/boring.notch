"""Time helpers. All datetimes in the database are naive UTC."""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def to_local(dt_utc: datetime, tz: str) -> datetime:
    return dt_utc.replace(tzinfo=UTC).astimezone(ZoneInfo(tz))


def local_to_utc(dt_local: datetime) -> datetime:
    return dt_local.astimezone(UTC).replace(tzinfo=None)


_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    m = _TIME_RE.match(value.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def display_time(hhmm: str) -> str:
    """'18:00' -> '6:00', '11:30' -> '11:30' (casual, texting style)."""
    parsed = parse_hhmm(hhmm)
    if not parsed:
        return hhmm
    h, m = parsed
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}"


def round_up_to(dt: datetime, minutes: int) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    remainder = dt.minute % minutes
    if remainder:
        dt += timedelta(minutes=minutes - remainder)
    return dt


def earliest_pickup(now_utc: datetime, tz: str, prep_minutes: int) -> datetime:
    """Earliest realistic local pickup time, rounded up to 15 minutes."""
    return round_up_to(to_local(now_utc, tz) + timedelta(minutes=prep_minutes), 15)


def resolve_pickup(hhmm: str | None, now_utc: datetime, tz: str, prep_minutes: int) -> str:
    """Return a valid 'HH:MM' pickup time no earlier than now + prep time."""
    earliest = earliest_pickup(now_utc, tz, prep_minutes)
    parsed = parse_hhmm(hhmm)
    if parsed:
        wanted = earliest.replace(hour=parsed[0], minute=parsed[1])
        if wanted >= earliest:
            return wanted.strftime("%H:%M")
    return earliest.strftime("%H:%M")
