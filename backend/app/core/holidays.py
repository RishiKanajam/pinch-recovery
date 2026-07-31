"""Australian business days, for scheduling debits banks will actually process.

BECS direct debit files are processed on business days only. A retry scheduled
for Anzac Day does not fail — it simply is not presented until the bank next
opens, which means the timing argument the whole engine rests on ("we retry on
your payday") quietly becomes "we retry some time that week". Rolling the date
forward ourselves keeps the scheduled time and the presented time the same
thing, and lets the reasoning string say which holiday moved it.

Scope is national holidays plus the NSW-only ones, because the merchants in the
demo are Sydney service businesses and the rest of the codebase already fixes
the calendar to AEST (see `scheduler.AEST`). Two consequences worth knowing:

- **The August bank holiday is in here.** It is a NSW *bank* holiday, not a
  public one — most people work through it and banks do not settle. It is
  exactly the kind of day a fixed "+5 days" retry lands on and loses a week.
- **State variations are not modelled.** A Queensland customer's King's
  Birthday is in October, not June. Per-customer calendars are the honest fix
  and belong with per-customer timezones; until then this is deliberately the
  NSW calendar rather than a wrong-everywhere average.

Dates are computed, not transcribed, so the table does not expire the moment
the demo runs in a year nobody hardcoded.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

MONDAY = 0
SATURDAY = 5
SUNDAY = 6


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian computus. Good Friday and Easter Monday hang off it."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 — the algorithm's own name
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth `weekday` of a month, e.g. the second Monday in June."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _weekend_substitute(day: date, taken: set[date]) -> date:
    """The Monday (or Tuesday) a weekend holiday is observed on.

    Christmas and Boxing Day collide over a weekend — Boxing Day observed on
    the Monday that Christmas already took shifts to the Tuesday — hence
    `taken` rather than a flat "+1 day if Saturday, +2 if Sunday".
    """
    if day.weekday() < SATURDAY:
        return day
    observed = day + timedelta(days=7 - day.weekday())
    while observed in taken:
        observed += timedelta(days=1)
    return observed


def _holidays(year: int) -> dict[date, str]:
    """Every non-processing day in `year`, keyed by date."""
    easter = _easter_sunday(year)
    table: dict[date, str] = {}

    def add(day: date, name: str, *, substitute: bool = False) -> None:
        if substitute:
            day = _weekend_substitute(day, set(table))
        table.setdefault(day, name)

    # Fixed-date, substituted to the following Monday when they fall on a
    # weekend — the bank is shut on the substitute day, not the actual one.
    add(date(year, 1, 1), "New Year's Day", substitute=True)
    add(date(year, 1, 26), "Australia Day", substitute=True)
    add(date(year, 12, 25), "Christmas Day", substitute=True)
    add(date(year, 12, 26), "Boxing Day", substitute=True)

    # Anzac Day is not substituted in NSW: when it falls on a weekend the
    # holiday stays on the weekend and the Monday is a normal business day.
    add(date(year, 4, 25), "Anzac Day")

    add(easter - timedelta(days=2), "Good Friday")
    add(easter - timedelta(days=1), "Easter Saturday")
    add(easter + timedelta(days=1), "Easter Monday")

    add(_nth_weekday(year, 6, MONDAY, 2), "King's Birthday")
    # NSW only, and banks only — see the module docstring.
    add(_nth_weekday(year, 8, MONDAY, 1), "Bank Holiday")
    add(_nth_weekday(year, 10, MONDAY, 1), "Labour Day")

    return table


# Computed per year on first use. A year is a few dict entries, and the demo
# never spans enough of them for this to be worth an eviction policy.
_cache: dict[int, dict[date, str]] = {}


def holiday_name(day: date) -> str | None:
    """The public holiday falling on `day`, or None."""
    table = _cache.get(day.year)
    if table is None:
        table = _holidays(day.year)
        _cache[day.year] = table
    return table.get(day)


def is_business_day(day: date) -> bool:
    """True when banks process on `day`."""
    return day.weekday() < SATURDAY and holiday_name(day) is None


def reason_not_business_day(day: date) -> str | None:
    """Why `day` is not a business day, phrased for a reasoning string."""
    if day.weekday() == SATURDAY:
        return "the weekend"
    if day.weekday() == SUNDAY:
        return "the weekend"
    return holiday_name(day)


def next_business_day(day: date) -> date:
    """The first business day on or after `day`."""
    # Bounded rather than `while True`: a bug in the holiday table that marked
    # every day a holiday would otherwise hang the scheduler instead of
    # producing one wrong date.
    for offset in range(14):
        candidate = day + timedelta(days=offset)
        if is_business_day(candidate):
            return candidate
    return day + timedelta(days=14)


def roll_to_business_day(at: datetime, tz) -> tuple[datetime, str | None]:
    """Move `at` forward to the next business day in `tz`, keeping the time.

    Returns the (possibly unchanged) datetime and, when it moved, what it moved
    off — "the weekend", "Anzac Day" — so the caller can say so in the note the
    dashboard renders. The timezone is passed in rather than imported to keep
    this module free of any opinion about which calendar the merchant is on;
    `scheduler` owns that.
    """
    local = at.astimezone(tz)
    rolled = next_business_day(local.date())
    if rolled == local.date():
        return at, None

    reason = reason_not_business_day(local.date())
    moved = local.replace(year=rolled.year, month=rolled.month, day=rolled.day)
    return moved.astimezone(at.tzinfo or tz), reason
