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
from app.models.enums import FailureClass
from app.models.schemas import Payment, PaymentStatus
from app.services.classifier import get_strategy_table
from app.services.strategy_engine import CustomerContext, plan

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
