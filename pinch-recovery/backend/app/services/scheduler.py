"""Scheduling helpers and the due-action poller.

Every time value here comes from app.core.clock.now(). That is what makes
POST /sim/fast-forward collapse a three-day settlement window into three
seconds without the recovery logic knowing anything special is happening — and
a single direct wall-clock read in this file would silently break the demo for
every code path that touches it. tests/test_clock_discipline.py enforces it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta, timezone

from app.core import clock
from app.models.enums import AttemptStatus
from app.models.schemas import Attempt, Payment

logger = logging.getLogger(__name__)

# The business is Australian and payday is a local-calendar concept, so weekday
# arithmetic has to happen in local time or a Friday retry lands on Thursday for
# half the year. Fixed +10:00 rather than a real tz database: the hackathon is
# entirely inside AEST, and pulling in zoneinfo/DST handling buys nothing the
# judge can see. If this ever ships, this constant becomes a per-customer tz.
AEST = timezone(timedelta(hours=10))

# Retries are submitted at 08:00 local — after overnight payroll has landed but
# early enough that a same-day dishonour still leaves the day to react.
# 08:00 AEST is 22:00 UTC the previous day, which is why scheduled_for values in
# docs/CONTRACT.md look like "...T22:00:00Z".
PAYDAY_RETRY_HOUR_AEST = 8

# How far ahead to search for a payday before giving up. Two weeks covers any
# combination of weekdays; if nothing matches in that span the weekday list is
# empty or nonsense and we fall back to the unaligned time.
_PAYDAY_SEARCH_DAYS = 15


def next_payday(after: datetime, weekdays: Sequence[int]) -> datetime:
    """The next 08:00 AEST that falls on one of `weekdays` (Mon=0), in UTC.

    Strictly after `after`, so an action already scheduled on a payday morning
    does not collapse to itself and stack two retries on the same instant.
    """
    if not weekdays:
        return after

    valid = {int(d) % 7 for d in weekdays}
    local = after.astimezone(AEST)

    for offset in range(_PAYDAY_SEARCH_DAYS):
        candidate = (local + timedelta(days=offset)).replace(
            hour=PAYDAY_RETRY_HOUR_AEST, minute=0, second=0, microsecond=0
        )
        if candidate.weekday() in valid and candidate > local:
            return candidate.astimezone(timezone.utc)

    logger.warning("No payday found within %d days of %s", _PAYDAY_SEARCH_DAYS, after)
    return after


def hours_from(base: datetime, hours: int) -> datetime:
    """`base` plus `hours`, always timezone-aware UTC."""
    return (base + timedelta(hours=hours)).astimezone(timezone.utc)


def due_attempts(payments: Iterable[Payment], now: datetime | None = None) -> list[Attempt]:
    """Scheduled attempts whose time has arrived, oldest first.

    `now` defaults to the simulated clock, so fast-forward makes a fortnight of
    scheduled work become due at once — which is exactly the demo moment.
    """
    at = now or clock.now()
    due: list[Attempt] = []
    for payment in payments:
        for attempt in payment.attempts:
            if attempt.status is not AttemptStatus.SCHEDULED:
                continue
            if attempt.scheduled_for is not None and attempt.scheduled_for <= at:
                due.append(attempt)
    due.sort(key=lambda a: (a.scheduled_for or at, a.attempt_number))
    return due


class DueActionPoller:
    """Runs due actions on a timer.

    Deliberately a plain thread-timer rather than an APScheduler job store: the
    only thing being scheduled is "call this function every N seconds", and the
    simulated clock means wall-clock cron semantics would actively get in the
    way. Fast-forward advances clock.now() instantly, so the next tick picks up
    everything that just became due.
    """

    def __init__(self, execute: Callable[[], int], interval_seconds: float = 2.0) -> None:
        self._execute = execute
        self._interval = interval_seconds
        self._timer = None
        self._stopped = True

    def start(self) -> None:
        if not self._stopped:
            return
        self._stopped = False
        self._schedule()

    def stop(self) -> None:
        self._stopped = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _schedule(self) -> None:
        import threading

        if self._stopped:
            return
        self._timer = threading.Timer(self._interval, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def _tick(self) -> None:
        try:
            executed = self._execute()
            if executed:
                logger.info("Executed %d due action(s)", executed)
        except Exception:
            # A poller that dies on one bad payment stops the whole demo.
            logger.exception("Due-action tick failed; continuing")
        finally:
            self._schedule()
