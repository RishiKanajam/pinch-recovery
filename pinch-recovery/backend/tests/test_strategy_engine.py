"""Engine and scheduler behaviour, with the clock frozen.

Every assertion about a date here would be flaky against a real clock, which is
the whole reason app.core.clock exists. Freeze is per-test via the fixture so a
failure in one test cannot leave the clock pinned for the next.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core import clock
from app.models.enums import (
    HARD_FAILURE_CLASSES,
    ActionType,
    AttemptStatus,
    Channel,
    FailureClass,
    PaymentStatus,
)
from app.models.schemas import Payment
from app.services.classifier import get_strategy_table
from app.services.scheduler import AEST, due_attempts, next_payday
from app.services.strategy_engine import CustomerContext, apply_plan, plan

# A Monday, so weekday arithmetic in the payday tests is easy to reason about.
FROZEN = datetime(2026, 7, 27, 3, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def frozen_clock():
    clock.freeze(FROZEN)
    yield
    clock.reset()


@pytest.fixture(scope="module")
def table():
    return get_strategy_table()


def make_payment(raw_code: str, **kwargs) -> Payment:
    defaults = dict(
        id="pay_01HX0001",
        customer_id="cus_01HX0001",
        customer_name="Marina Auto Detailing",
        amount_cents=24900,
        currency="AUD",
        status=PaymentStatus.FAILED,
        raw_code=raw_code,
        failed_at=FROZEN,
    )
    defaults.update(kwargs)
    return Payment(**defaults)


# --- the rule that matters most ------------------------------------------------


@pytest.mark.parametrize(
    "code,failure_class",
    [
        ("AC01", FailureClass.INVALID_ACCOUNT),
        ("MD01", FailureClass.AUTHORITY_CANCELLED),
        ("MS02", FailureClass.PAYMENT_STOPPED),
    ],
)
def test_hard_failures_schedule_zero_retries(table, code, failure_class):
    """README non-negotiable 5, asserted through the engine rather than the YAML."""
    result = plan(make_payment(code), table=table)
    assert result.failure_class is failure_class
    assert failure_class in HARD_FAILURE_CLASSES
    assert result.retry_count == 0
    assert not [
        a
        for a in result.attempts
        if a.action is ActionType.RETRY and a.status is AttemptStatus.SCHEDULED
    ]


def test_hard_failure_still_does_something(table):
    """Zero retries must not mean zero action, or the payment is just abandoned."""
    result = plan(make_payment("AC01"), table=table)
    actions = {a.action for a in result.scheduled_attempts}
    assert ActionType.REQUEST_DETAILS_UPDATE in actions
    assert ActionType.NOTIFY_HUMAN in actions


def test_soft_failure_does_retry(table):
    result = plan(make_payment("AM04"), table=table)
    assert result.failure_class is FailureClass.INSUFFICIENT_FUNDS
    assert result.retry_count > 0


# --- retry expansion and max_attempts -----------------------------------------


def test_retries_are_capped_by_max_attempts(table):
    result = plan(make_payment("AM04"), table=table)
    strategy = table.strategy_for(FailureClass.INSUFFICIENT_FUNDS)
    assert result.retry_count <= strategy.max_attempts


def test_technical_retries_twice_and_stays_silent(table):
    result = plan(make_payment("AG01"), table=table)
    retries = [a for a in result.scheduled_attempts if a.action is ActionType.RETRY]
    assert len(retries) == 2
    assert all("silent" in (a.note or "").lower() for a in retries)
    # A silent class must not message the customer at all.
    assert not [
        a
        for a in result.scheduled_attempts
        if a.action is ActionType.REQUEST_DETAILS_UPDATE
    ]


def test_expired_card_retries_once_then_asks_for_details(table):
    result = plan(make_payment("54"), table=table)
    assert result.retry_count == 1
    assert ActionType.REQUEST_DETAILS_UPDATE in {
        a.action for a in result.scheduled_attempts
    }


# --- payday alignment ----------------------------------------------------------


def test_next_payday_lands_on_a_requested_weekday():
    # Thursday=3, Friday=4
    result = next_payday(FROZEN, [3, 4])
    local = result.astimezone(AEST)
    assert local.weekday() in (3, 4)
    assert local.hour == 8
    assert result > FROZEN


def test_next_payday_is_strictly_in_the_future():
    """A Thursday 08:00 request must not return that same instant."""
    thursday_8am = datetime(2026, 7, 30, 8, 0, tzinfo=AEST)
    result = next_payday(thursday_8am.astimezone(timezone.utc), [3])
    assert result > thursday_8am.astimezone(timezone.utc)
    assert result.astimezone(AEST).weekday() == 3


def test_next_payday_with_no_weekdays_returns_input():
    assert next_payday(FROZEN, []) == FROZEN


def test_observed_payday_beats_the_default(table):
    """A customer paid on Tuesday should not be retried on Thursday."""
    customer = CustomerContext(customer_id="cus_01HX0001", payday_weekday=1)
    result = plan(make_payment("AM04"), customer=customer, table=table)
    retries = [a for a in result.scheduled_attempts if a.action is ActionType.RETRY]
    assert retries
    for attempt in retries:
        assert attempt.scheduled_for.astimezone(AEST).weekday() == 1
    assert any("observed payday" in (a.note or "") for a in retries)


def test_default_payday_used_without_history(table):
    customer = CustomerContext(customer_id="cus_01HX0001", payday_weekday=None)
    result = plan(make_payment("AM04"), customer=customer, table=table)
    retries = [a for a in result.scheduled_attempts if a.action is ActionType.RETRY]
    for attempt in retries:
        assert attempt.scheduled_for.astimezone(AEST).weekday() in (3, 4)


def test_aligned_retries_land_on_successive_paydays_not_adjacent_days(table):
    """Regression: repeats measured from t0 collapse onto consecutive mornings.

    With four retries at +120h each, computing every repeat as an offset from
    classification put retry 1 on Thursday and retry 2 on the Friday straight
    after — two retries 24h apart, which is exactly the blind-retry behaviour
    this product exists to replace. Repeats chain off the previous retry.
    """
    result = plan(make_payment("AM04"), table=table)
    retries = sorted(
        (a for a in result.scheduled_attempts if a.action is ActionType.RETRY),
        key=lambda a: a.scheduled_for,
    )
    assert len(retries) >= 2
    for earlier, later in zip(retries, retries[1:]):
        gap = later.scheduled_for - earlier.scheduled_for
        assert gap >= timedelta(days=5), (
            f"retries {earlier.attempt_number} and {later.attempt_number} are "
            f"only {gap} apart; payday alignment collapsed the interval"
        )


def test_unaligned_retry_is_not_moved_to_a_payday(table):
    """technical has align_to_payday: false — it must fire on the fixed interval."""
    result = plan(make_payment("AG01"), table=table)
    first = [a for a in result.scheduled_attempts if a.action is ActionType.RETRY][0]
    assert first.scheduled_for == FROZEN + timedelta(hours=24)


# --- global rules --------------------------------------------------------------


def test_retry_budget_exhaustion_suppresses_retries(table):
    rules = table.global_rules
    customer = CustomerContext(
        customer_id="cus_01HX0001",
        retries_in_window=rules.customer_max_retries_in_window,
    )
    result = plan(make_payment("AM04"), customer=customer, table=table)
    assert result.retry_count == 0
    skipped = result.skipped_attempts
    assert skipped, "an exhausted budget should leave a visible skipped attempt"
    assert any("budget" in (a.note or "").lower() or "retries" in (a.note or "").lower() for a in skipped)
    assert any("budget" in line.lower() for line in result.decision_trace)


def test_partial_retry_budget_limits_but_does_not_zero_retries(table):
    rules = table.global_rules
    customer = CustomerContext(
        customer_id="cus_01HX0001",
        retries_in_window=rules.customer_max_retries_in_window - 1,
    )
    result = plan(make_payment("AM04"), customer=customer, table=table)
    assert result.retry_count == 1


def test_message_cap_delays_rather_than_drops(table):
    """A recent contact must push the next message out, not lose it."""
    rules = table.global_rules
    just_messaged = FROZEN - timedelta(hours=1)
    customer = CustomerContext(
        customer_id="cus_01HX0001", last_message_at=just_messaged
    )
    result = plan(make_payment("AC01"), customer=customer, table=table)
    messages = [
        a
        for a in result.scheduled_attempts
        if a.action is ActionType.REQUEST_DETAILS_UPDATE
    ]
    assert messages, "the message must still be scheduled, just later"
    earliest = just_messaged + timedelta(
        hours=rules.min_hours_between_customer_messages
    )
    assert messages[0].scheduled_for >= earliest
    assert any("minimum gap" in (a.note or "") for a in messages)


def test_messages_respect_the_cap_between_each_other(table):
    """invalid_account sends email then SMS; they must not bunch up."""
    rules = table.global_rules
    result = plan(make_payment("AC01"), table=table)
    messages = sorted(
        (
            a
            for a in result.scheduled_attempts
            if a.action is ActionType.REQUEST_DETAILS_UPDATE
        ),
        key=lambda a: a.scheduled_for,
    )
    assert len(messages) >= 2
    for earlier, later in zip(messages, messages[1:]):
        gap = later.scheduled_for - earlier.scheduled_for
        assert gap >= timedelta(hours=rules.min_hours_between_customer_messages)


def test_write_off_is_always_scheduled(table):
    rules = table.global_rules
    result = plan(make_payment("AM04"), table=table)
    write_offs = [
        a for a in result.attempts if a.action is ActionType.WRITE_OFF
    ]
    assert len(write_offs) == 1
    assert write_offs[0].scheduled_for == FROZEN + timedelta(
        days=rules.write_off_after_days
    )


def test_write_off_horizon_measured_from_failure_not_now(table):
    """Re-running recovery on an old payment must not extend its life."""
    rules = table.global_rules
    failed_long_ago = FROZEN - timedelta(days=10)
    result = plan(make_payment("AM04", failed_at=failed_long_ago), table=table)
    assert result.write_off_at == failed_long_ago + timedelta(
        days=rules.write_off_after_days
    )


def test_nothing_is_scheduled_past_the_write_off_horizon(table):
    result = plan(make_payment("AM04"), table=table)
    for attempt in result.scheduled_attempts:
        if attempt.action is ActionType.WRITE_OFF:
            continue
        assert attempt.scheduled_for <= result.write_off_at


def test_actions_beyond_horizon_are_skipped_with_a_reason(table):
    """A payment that failed 20 days ago has almost no runway left."""
    nearly_expired = FROZEN - timedelta(days=20, hours=12)
    result = plan(make_payment("AM04", failed_at=nearly_expired), table=table)
    skipped = [a for a in result.skipped_attempts]
    assert skipped
    assert all(a.note for a in skipped)
    assert any("horizon" in (a.note or "").lower() for a in skipped)


# --- shape and ordering --------------------------------------------------------


def test_attempts_are_ordered_and_numbered_for_display(table):
    result = plan(make_payment("AC01"), table=table)
    numbers = [a.attempt_number for a in result.attempts]
    assert numbers == list(range(1, len(result.attempts) + 1))
    scheduled = [a for a in result.attempts if a.scheduled_for is not None]
    times = [a.scheduled_for for a in scheduled]
    assert times == sorted(times), "timeline must render in chronological order"


def test_attempt_ids_are_unique(table):
    result = plan(make_payment("AC01"), table=table)
    ids = [a.id for a in result.attempts]
    assert len(ids) == len(set(ids))


def test_every_attempt_has_a_note(table):
    """The drill-down renders these; a blank note is a blank row."""
    for code in ["AM04", "AC01", "MD01", "MS02", "AG01", "54", "05", "ZZ99"]:
        result = plan(make_payment(code), table=table)
        for attempt in result.attempts:
            assert (attempt.note or "").strip(), f"{code} attempt {attempt.id} has no note"


def test_decision_trace_is_populated_for_every_class(table):
    for code in ["AM04", "AC01", "MD01", "MS02", "AG01", "54", "05", "ZZ99"]:
        result = plan(make_payment(code), table=table)
        assert result.decision_trace
        assert all(line.strip() for line in result.decision_trace)


def test_unknown_code_plans_conservatively_and_tells_a_human(table):
    result = plan(make_payment("ZZ99"), table=table)
    assert result.failure_class is FailureClass.UNKNOWN
    assert result.retry_count == 1
    assert ActionType.NOTIFY_HUMAN in {a.action for a in result.scheduled_attempts}
    assert any("not in the strategy table" in line for line in result.decision_trace)


def test_missing_raw_code_does_not_crash(table):
    result = plan(make_payment(None), table=table)
    assert result.failure_class is FailureClass.UNKNOWN
    assert result.reasoning


def test_plan_does_not_mutate_the_input_payment(table):
    payment = make_payment("AM04")
    plan(payment, table=table)
    assert payment.attempts == []
    assert payment.reasoning is None
    assert payment.failure_class is None


def test_apply_plan_attaches_output_without_mutating(table):
    payment = make_payment("AM04", status=PaymentStatus.PENDING)
    result = plan(payment, table=table)
    updated = apply_plan(payment, result)

    assert updated is not payment
    assert payment.failure_class is None
    assert updated.failure_class is FailureClass.INSUFFICIENT_FUNDS
    assert updated.reasoning
    assert updated.attempts
    assert updated.status is PaymentStatus.FAILED


def test_apply_plan_does_not_resurrect_terminal_status(table):
    payment = make_payment("AM04", status=PaymentStatus.RECOVERED)
    updated = apply_plan(payment, plan(payment, table=table))
    assert updated.status is PaymentStatus.RECOVERED


# --- due-action polling --------------------------------------------------------


def test_due_attempts_respects_the_simulated_clock(table):
    payment = make_payment("AM04")
    result = plan(payment, table=table)
    payment = apply_plan(payment, result)

    # A delay_hours: 0 message is due the instant it is planned, so the
    # meaningful assertion is about the retry, which is five days out.
    due_now = due_attempts([payment], now=FROZEN)
    assert not [a for a in due_now if a.action is ActionType.RETRY]

    # Fast-forward past the first retry and it becomes due — this is the
    # three-days-in-three-seconds demo moment. 12 days clears the +120h retry
    # after payday alignment pushes it out to the following Thursday.
    clock.fast_forward(seconds=60 * 60 * 24 * 12)
    due = due_attempts([payment])
    assert [a for a in due if a.action is ActionType.RETRY], (
        "fast-forward should make the scheduled retry due"
    )
    assert all(a.status is AttemptStatus.SCHEDULED for a in due)


def test_due_attempts_ignores_skipped_and_executed(table):
    payment = make_payment("AM04")
    payment = apply_plan(payment, plan(payment, table=table))
    for attempt in payment.attempts:
        attempt.status = AttemptStatus.SKIPPED
    clock.fast_forward(seconds=60 * 60 * 24 * 60)
    assert due_attempts([payment]) == []


def test_due_attempts_are_sorted_oldest_first(table):
    payment = make_payment("AC01")
    payment = apply_plan(payment, plan(payment, table=table))
    clock.fast_forward(seconds=60 * 60 * 24 * 60)
    due = due_attempts([payment])
    times = [a.scheduled_for for a in due]
    assert times == sorted(times)
