"""Time helpers. Everything internal is timezone-aware UTC.

Broker server time and display time are separate zones — convert explicitly,
never assume (ARCHITECTURE.md §10.2).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def to_utc(dt: datetime, assume_tz: str | None = None) -> datetime:
    """Return ``dt`` as aware UTC.

    A naive ``dt`` is interpreted in ``assume_tz`` (e.g. the broker server zone)
    when given, otherwise treated as already-UTC.
    """
    if dt.tzinfo is None:
        tz = ZoneInfo(assume_tz) if assume_tz else UTC
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC)


def to_iso(dt: datetime | None) -> str | None:
    """Serialise an aware datetime to an ISO8601 UTC string for storage."""
    if dt is None:
        return None
    return to_utc(dt).isoformat()


def from_iso(value: str | None) -> datetime | None:
    """Parse an ISO8601 string back into an aware UTC datetime."""
    if not value:
        return None
    return to_utc(datetime.fromisoformat(value))


def to_display(dt: datetime, display_tz: str) -> datetime:
    """Convert an aware UTC datetime into the configured display zone."""
    return to_utc(dt).astimezone(ZoneInfo(display_tz))
