"""The reasoning string is a product surface, so it gets tested like one.

README non-negotiable 4: a classified payment without reasoning is an
unfinished feature. These tests assert it exists for every class, reads as
prose rather than a label, and actually names the specifics of the payment in
front of it — a generic sentence would pass a "not empty" check and still be
worthless in the demo.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core import clock
from app.models.enums import ActionType, AttemptStatus, FailureClass
from app.models.schemas import Attempt, Payment, PaymentStatus
from app.services.classifier import get_strategy_table
from app.services.scheduler import AEST
from app.services.reasoning import END_STATE_LABELS, build_headline, end_state
from app.services.strategy_engine import CustomerContext, apply_plan, plan

FROZEN = datetime(2026, 7, 27, 3, 0, tzinfo=timezone.utc)

# One representative raw code per class, so a failure names the class involved.
CODE_FOR_CLASS = {
    FailureClass.INSUFFICIENT_FUNDS: "insufficient-funds",
    FailureClass.INVALID_ACCOUNT: "invalid-account",
    FailureClass.AUTHORITY_CANCELLED: "authority-cancelled",
    FailureClass.PAYMENT_STOPPED: "payment-stopped",
    FailureClass.TECHNICAL: "technical-error",
    FailureClass.EXPIRED_CARD: "invalid-card",
    FailureClass.DO_NOT_HONOUR: "blocked-by-bank",
    FailureClass.UNKNOWN: "ZZ99",
}


@pytest.fixture(autouse=True)
def frozen_clock():
    clock.freeze(FROZEN)
    yield
    clock.reset()


@pytest.fixture(scope="module")
def table():
    return get_strategy_table()


def make_payment(raw_code, **kwargs) -> Payment:
    defaults = dict(
        id="pay_01HX0001",
        customer_id="cus_01HX0001",
        customer_name="Brunswick Pilates Studio",
        amount_cents=24900,
        currency="AUD",
        status=PaymentStatus.FAILED,
        raw_code=raw_code,
        failed_at=FROZEN,
    )
    defaults.update(kwargs)
    return Payment(**defaults)


@pytest.mark.parametrize("failure_class,code", list(CODE_FOR_CLASS.items()))
def test_every_class_produces_real_prose(table, failure_class, code):
    result = plan(make_payment(code), table=table)
    assert result.failure_class is failure_class

    reasoning = result.reasoning
    assert reasoning and reasoning.strip()
    # Prose, not a label: several sentences and a real word count.
    assert len(reasoning.split()) >= 20, reasoning
    assert reasoning.count(".") >= 2, reasoning
    assert reasoning[0].isupper() or reasoning[0] == "$"
    assert reasoning.endswith(".")
    # No unrendered template debris.
    for artefact in ("{", "}", "None", "  ", "TODO"):
        assert artefact not in reasoning, f"{artefact!r} leaked into: {reasoning}"


@pytest.mark.parametrize("failure_class,code", list(CODE_FOR_CLASS.items()))
def test_reasoning_names_the_specific_payment(table, failure_class, code):
    """A sentence that would read identically for any payment is not reasoning."""
    result = plan(make_payment(code), table=table)
    assert "Brunswick Pilates Studio" in result.reasoning
    assert "$249.00" in result.reasoning


def test_reasoning_quotes_the_raw_code(table):
    result = plan(make_payment("invalid-account"), table=table)
    assert "invalid-account" in result.reasoning


def test_reasoning_explains_why_hard_failures_are_not_retried(table):
    """The core argument of the product has to be legible in this field."""
    result = plan(make_payment("invalid-account"), table=table)
    lowered = result.reasoning.lower()
    assert "no retries" in lowered
    assert "fee" in lowered or "cannot" in lowered


def test_reasoning_describes_a_retry_when_one_is_scheduled(table):
    result = plan(make_payment("insufficient-funds"), table=table)
    lowered = result.reasoning.lower()
    assert "retr" in lowered
    assert "no retries" not in lowered


def test_reasoning_mentions_payday_history_when_it_was_used(table):
    customer = CustomerContext(customer_id="cus_01HX0001", payday_weekday=1)
    result = plan(make_payment("insufficient-funds"), customer=customer, table=table)
    assert "payment history" in result.reasoning.lower()


def test_reasoning_explains_a_suppressed_retry_budget(table):
    rules = table.global_rules
    customer = CustomerContext(
        customer_id="cus_01HX0001",
        retries_in_window=rules.customer_max_retries_in_window,
    )
    result = plan(make_payment("insufficient-funds"), customer=customer, table=table)
    lowered = result.reasoning.lower()
    assert "budget" in lowered
    assert "relationship problem" in lowered


def test_reasoning_explains_a_delayed_message(table):
    customer = CustomerContext(
        customer_id="cus_01HX0001", last_message_at=FROZEN - timedelta(hours=1)
    )
    result = plan(make_payment("invalid-account"), customer=customer, table=table)
    assert "hours between messages" in result.reasoning.lower()


def test_reasoning_stays_quiet_about_rules_that_did_not_fire(table):
    """Boilerplate about inapplicable rules trains the reader to skip the field."""
    result = plan(make_payment("insufficient-funds"), table=table)
    assert "budget" not in result.reasoning.lower()
    assert "hours between messages" not in result.reasoning.lower()


def test_missing_code_still_reasons(table):
    result = plan(make_payment(None), table=table)
    assert result.reasoning
    assert len(result.reasoning.split()) >= 15
    assert "None" not in result.reasoning


def test_reasoning_for_silent_class_says_so(table):
    result = plan(make_payment("technical-error"), table=table)
    assert "silent" in result.reasoning.lower()


def test_reasoning_for_churn_signal_frames_it_as_churn(table):
    result = plan(make_payment("authority-cancelled"), table=table)
    assert "churn" in result.reasoning.lower()


def test_reasoning_for_dispute_routes_to_a_human(table):
    result = plan(make_payment("payment-stopped"), table=table)
    lowered = result.reasoning.lower()
    assert "human" in lowered
    assert "no retries" in lowered


def test_unknown_class_admits_it_does_not_know(table):
    result = plan(make_payment("ZZ99"), table=table)
    lowered = result.reasoning.lower()
    assert "does not recognise" in lowered or "not recognise" in lowered


def test_money_is_formatted_from_integer_cents(table):
    """No floats in the model; formatting happens at the edge only."""
    result = plan(make_payment("insufficient-funds", amount_cents=1999), table=table)
    assert "$19.99" in result.reasoning

    result = plan(make_payment("insufficient-funds", amount_cents=124900), table=table)
    assert "$1,249.00" in result.reasoning


# --- the one-line version ------------------------------------------------------
#
# The paragraph above is what a judge reads when they stop on a payment. The
# headline is what they read while scanning fifty of them, so it has to carry
# the same argument in about twelve words — and never render as a label.


@pytest.mark.parametrize("failure_class,code", sorted(CODE_FOR_CLASS.items()))
def test_every_class_produces_a_headline(table, failure_class, code):
    payment = apply_plan(make_payment(code), plan(make_payment(code), table=table))
    line = build_headline(payment)

    assert "→" in line, f"{failure_class.value} headline is not a trace: {line!r}"
    assert len(line.split()) >= 5, f"{failure_class.value} headline is a label: {line!r}"
    assert "None" not in line


def test_headline_names_the_date_and_the_position_in_the_ladder(table):
    """The PRD's example line: reason, action, date, and attempt N of M."""
    payment = make_payment("insufficient-funds", amount_cents=9900)
    payment = apply_plan(payment, plan(payment, table=table))
    line = build_headline(payment)

    assert line.startswith("Insufficient funds → retrying ")
    assert "payday" in line
    assert "attempt 1 of" in line


def test_headline_explains_the_date_a_retry_landed_on(table):
    """A date with no reason beside it is a cron job with better typography."""
    # Sunday evening in Sydney: +24h lands on the Monday, which is the NSW
    # bank holiday, so the debit moves to the Tuesday.
    failed_at = datetime(2026, 8, 2, 18, 0, tzinfo=AEST).astimezone(timezone.utc)
    payment = make_payment("technical-error", failed_at=failed_at)
    payment = apply_plan(payment, plan(payment, table=table))
    assert "next business day after Bank Holiday" in build_headline(payment)


def test_headline_for_a_hard_failure_says_it_is_never_retried(table):
    payment = make_payment("invalid-account")
    payment = apply_plan(payment, plan(payment, table=table))
    line = build_headline(payment)
    assert "never retried" in line
    assert "retrying" not in line


def test_headline_for_a_recovered_payment_states_the_amount_and_date(table):
    payment = make_payment(
        "insufficient-funds",
        status=PaymentStatus.RECOVERED,
        recovered_at=FROZEN + timedelta(days=5),
    )
    line = build_headline(apply_plan(payment, plan(payment, table=table)))
    assert "recovered $249.00" in line
    assert "Saturday 1 Aug" in line


def test_headline_for_a_written_off_payment_says_what_was_tried(table):
    payment = make_payment("invalid-account", status=PaymentStatus.WRITTEN_OFF)
    line = build_headline(apply_plan(payment, plan(payment, table=table)))
    assert "written off" in line
    assert "no retry was ever safe to make" in line


def test_headline_offers_the_split_in_dollars(table):
    """Scanning the row should show the actual offer, not the word "split"."""
    payment = make_payment("insufficient-funds", amount_cents=9900)
    payment = apply_plan(payment, plan(payment, table=table))
    # Walk past the retries to the offer itself.
    for attempt in payment.attempts:
        if attempt.action is ActionType.OFFER_SPLIT:
            break
        attempt.status = AttemptStatus.EXECUTED
        attempt.executed_at = FROZEN
    line = build_headline(payment)
    assert "$49.50 + $49.50" in line


# --- end states ----------------------------------------------------------------


def test_end_state_is_one_of_four_with_a_reason(table):
    for code in CODE_FOR_CLASS.values():
        payment = make_payment(code)
        payment = apply_plan(payment, plan(payment, table=table))
        state, reason = end_state(payment)
        assert state in END_STATE_LABELS
        assert len(reason.split()) >= 5, f"{code}: {reason!r}"


def test_a_written_off_payment_says_why_nothing_worked(table):
    payment = make_payment("insufficient-funds", status=PaymentStatus.WRITTEN_OFF)
    payment = apply_plan(payment, plan(payment, table=table))
    state, reason = end_state(payment)
    assert state == "written_off"
    assert "horizon" in reason


def test_a_payment_with_a_human_is_not_reported_as_in_flight(table):
    """Escalated is not a status — it is a payment waiting on a person."""
    payment = make_payment("payment-stopped")
    payment = apply_plan(payment, plan(payment, table=table))
    for attempt in payment.attempts:
        if attempt.action is ActionType.NOTIFY_HUMAN:
            attempt.status = AttemptStatus.EXECUTED
            attempt.executed_at = FROZEN

    state, reason = end_state(payment)
    assert state == "escalated"
    assert "complaint" in reason


def test_a_recovery_after_a_details_fix_says_so(table):
    """The demo's live moment deserves its own sentence, not a generic one."""
    payment = make_payment("invalid-account", status=PaymentStatus.RECOVERED)
    payment = apply_plan(payment, plan(payment, table=table))
    payment.attempts.append(
        Attempt(
            id="att_fix",
            payment_id=payment.id,
            action=ActionType.RETRY,
            status=AttemptStatus.SUCCEEDED,
            attempt_number=99,
            executed_at=FROZEN,
            note="Details corrected by the customer, so a debit succeeded.",
        )
    )
    state, reason = end_state(payment)
    assert state == "recovered"
    assert "details" in reason.lower()
