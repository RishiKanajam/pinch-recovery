"""Simulated clock.

Every time-dependent read in business logic goes through here. Nothing calls
datetime.now() or datetime.utcnow() directly — there is a test that enforces it.

Why: direct debit dishonours arrive days after the debit. A demo cannot wait
three days, and a test suite certainly cannot. Fast-forward collapses that
window without any special-casing in the recovery logic itself.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

_lock = threading.Lock()
_offset = timedelta(0)
_frozen_at: datetime | None = None


def now() -> datetime:
    """Current time, adjusted by any simulated offset. Always timezone-aware UTC."""
    with _lock:
        if _frozen_at is not None:
            return _frozen_at + _offset
        return datetime.now(timezone.utc) + _offset


def fast_forward(seconds: float) -> datetime:
    """Advance the simulated clock. Returns the new now()."""
    global _offset
    with _lock:
        _offset += timedelta(seconds=seconds)
    return now()


def freeze(at: datetime | None = None) -> datetime:
    """Pin the clock. Used in tests for deterministic scheduling assertions."""
    global _frozen_at
    with _lock:
        _frozen_at = at or datetime.now(timezone.utc)
    return now()


def unfreeze() -> None:
    global _frozen_at
    with _lock:
        _frozen_at = None


def reset() -> None:
    """Return to real time with no offset. Called by POST /sim/reset."""
    global _offset, _frozen_at
    with _lock:
        _offset = timedelta(0)
        _frozen_at = None


def offset_seconds() -> float:
    """How far ahead of real time we currently are. Surfaced in the UI."""
    with _lock:
        return _offset.total_seconds()


def to_iso_z(value: datetime | None) -> str | None:
    """Serialise as 2026-07-25T04:12:00Z, the wire format docs/CONTRACT.md specifies.

    A tz-aware datetime read back from Postgres carries whatever offset the
    session's TimeZone setting is (not necessarily +00:00) even though the
    stored instant is correct — same moment, different string. Normalise to
    UTC before formatting so the "Z" suffix is never a lie.
    """
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
