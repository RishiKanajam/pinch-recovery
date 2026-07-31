"""The AU business-day calendar, and the roll-off it drives.

A retry scheduled on a day the banks do not process is not a small
inaccuracy: the debit is presented whenever the bank next opens, so the date
the dashboard shows and the date the money moves stop being the same thing,
and "we retried on your payday" quietly becomes false. These assert the dates
rather than the mechanism — a wrong Easter is the failure mode here, not a
wrong function signature.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.holidays import (
    holiday_name,
    is_business_day,
    next_business_day,
    roll_to_business_day,
)
from app.services.scheduler import AEST, business_day


# --- the calendar itself -------------------------------------------------------


@pytest.mark.parametrize(
    "day,name",
    [
        (date(2026, 1, 1), "New Year's Day"),
        (date(2026, 1, 26), "Australia Day"),
        (date(2026, 4, 3), "Good Friday"),
        (date(2026, 4, 6), "Easter Monday"),
        (date(2026, 4, 25), "Anzac Day"),
        (date(2026, 6, 8), "King's Birthday"),
        # NSW bank holiday: banks shut, most people at work. Precisely the day
        # a fixed-interval retry lands on and loses without anyone noticing.
        (date(2026, 8, 3), "Bank Holiday"),
        (date(2026, 10, 5), "Labour Day"),
        (date(2026, 12, 25), "Christmas Day"),
        # 26 Dec 2026 is a Saturday, so Boxing Day is observed on the Monday.
        (date(2026, 12, 28), "Boxing Day"),
    ],
)
def test_known_2026_holidays(day, name):
    assert holiday_name(day) == name
    assert not is_business_day(day)


def test_easter_is_computed_not_transcribed():
    """A hardcoded table would be wrong the first time the demo runs next year."""
    assert holiday_name(date(2027, 3, 26)) == "Good Friday"
    assert holiday_name(date(2028, 4, 14)) == "Good Friday"


def test_christmas_and_boxing_day_do_not_collide_on_the_same_monday():
    """Both fall on a weekend in 2027; the substitutes must be distinct days."""
    assert holiday_name(date(2027, 12, 27)) == "Christmas Day"
    assert holiday_name(date(2027, 12, 28)) == "Boxing Day"


def test_anzac_day_is_not_substituted():
    """NSW keeps Anzac Day on the weekend; the Monday is a normal business day."""
    assert holiday_name(date(2026, 4, 25)) == "Anzac Day"  # a Saturday
    assert is_business_day(date(2026, 4, 27))


def test_a_normal_weekday_is_a_business_day():
    assert is_business_day(date(2026, 7, 30))
    assert holiday_name(date(2026, 7, 30)) is None


def test_next_business_day_skips_the_easter_run():
    """Good Friday through Easter Monday is four days shut in a row."""
    assert next_business_day(date(2026, 4, 3)) == date(2026, 4, 7)


def test_next_business_day_returns_the_day_itself_when_it_is_open():
    assert next_business_day(date(2026, 7, 30)) == date(2026, 7, 30)


# --- the roll applied to a scheduled time -------------------------------------


def test_roll_keeps_the_time_of_day():
    """08:00 on a Saturday becomes 08:00 on the Monday, not midnight."""
    saturday_8am = datetime(2026, 8, 8, 8, 0, tzinfo=AEST)
    rolled, reason = roll_to_business_day(saturday_8am.astimezone(timezone.utc), AEST)
    local = rolled.astimezone(AEST)
    assert local.date() == date(2026, 8, 10)
    assert (local.hour, local.minute) == (8, 0)
    assert reason == "the weekend"


def test_roll_names_the_holiday_it_moved_off():
    monday_bank_holiday = datetime(2026, 8, 3, 8, 0, tzinfo=AEST)
    rolled, reason = business_day(monday_bank_holiday.astimezone(timezone.utc))
    assert reason == "Bank Holiday"
    assert rolled.astimezone(AEST).date() == date(2026, 8, 4)


def test_roll_is_a_no_op_on_a_business_day():
    thursday = datetime(2026, 8, 6, 8, 0, tzinfo=AEST).astimezone(timezone.utc)
    rolled, reason = business_day(thursday)
    assert rolled == thursday
    assert reason is None


def test_roll_uses_local_dates_not_utc_dates():
    """22:00 UTC Friday is 08:00 Saturday in Sydney — it must still roll.

    The engine schedules payday retries at 08:00 AEST, which is the previous
    day in UTC. A roll that read the UTC date would see a Friday and leave the
    debit sitting on a Saturday.
    """
    saturday_8am_aest = datetime(2026, 8, 8, 8, 0, tzinfo=AEST).astimezone(timezone.utc)
    assert saturday_8am_aest.weekday() == 4  # Friday, in UTC
    rolled, reason = business_day(saturday_8am_aest)
    assert reason == "the weekend"
    assert rolled.astimezone(AEST).date() == date(2026, 8, 10)


def test_roll_never_moves_a_time_backwards():
    at = datetime(2026, 12, 25, 8, 0, tzinfo=AEST).astimezone(timezone.utc)
    rolled, _ = business_day(at)
    assert rolled > at
    assert rolled - at <= timedelta(days=7)
