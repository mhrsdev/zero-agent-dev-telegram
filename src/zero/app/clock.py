"""Canonical UTC clock for application services.

Every service used to carry its own private ``_now_utc_iso`` with a
byte-identical body (17 copies). One definition keeps the persisted
timestamp format — millisecond-precision ISO-8601 with a ``Z`` suffix,
which every repository query and ordering comparison depends on — in a
single place.
"""

from __future__ import annotations

from datetime import UTC, datetime

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def now_utc_iso() -> str:
    """Return the current UTC time in the canonical persisted format."""
    return datetime.now(UTC).strftime(_TIMESTAMP_FORMAT)


def to_utc_iso(moment: datetime) -> str:
    """Render an aware datetime in the canonical persisted format."""
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)


__all__ = ["now_utc_iso", "to_utc_iso"]
