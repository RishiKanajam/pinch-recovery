"""Webhook ingest, and the idempotency guarantee it rests on.

CLAUDE.md rule 3: the same event delivered twice leaves exactly one payment
row. Pinch retries webhooks; a duplicate that double-counts would inflate
at_risk_cents on the dashboard and schedule a second set of recovery actions
against a customer who only failed once.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.core import clock
from app.models import Customer, Payment, WebhookEvent

WEBHOOK_URL = "/api/v1/webhooks/pinch"

# Frozen so failed_at is exact rather than "roughly now".
FROZEN_AT = datetime(2026, 7, 25, 4, 12, 0, tzinfo=timezone.utc)

CUSTOMER_ID = "cus_01HXTESTCUSTOMER0000000001"
PAYMENT_ID = "pay_01HXTESTPAYMENT00000000001"
EVENT_ID = "evt_01HXTESTEVENT000000000001"


@pytest.fixture
def frozen_clock():
    """Pin the simulated clock for the duration of a test."""
    clock.freeze(FROZEN_AT)
    yield FROZEN_AT
    clock.reset()


@pytest.fixture
def customer(db_session):
    """A seeded customer, the normal case — the simulator creates these first."""
    existing = Customer(id=CUSTOMER_ID, name="Marina Auto Detailing")
    db_session.add(existing)
    db_session.commit()
    return existing


def dishonour_event(
    event_id: str = EVENT_ID,
    payment_id: str = PAYMENT_ID,
    customer_id: str = CUSTOMER_ID,
    dishonour_code: str = "insufficient-funds",
    amount_cents: int = 24900,
) -> dict:
    """The envelope from docs/CONTRACT.md, Ingestion-internal — Pinch's real
    `bank-results` shape, verified 2026-07-26."""
    return {
        "Id": event_id,
        "Type": "bank-results",
        "EventDate": "2026-07-25T04:12:00Z",
        "Data": {
            "Payments": [
                {
                    "Id": payment_id,
                    "Status": "dishonoured",
                    "Amount": amount_cents,
                    "Dishonour": {"Type": dishonour_code, "Description": dishonour_code},
                    "Payer": {"Id": customer_id},
                }
            ]
        },
    }


def _count(session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


# --------------------------------------------------------------------------
# First delivery
# --------------------------------------------------------------------------


def test_dishonour_creates_one_payment_and_one_event(
    client, db_session, customer, frozen_clock
):
    response = client.post(WEBHOOK_URL, json=dishonour_event())
    assert response.status_code == 200

    assert _count(db_session, Payment) == 1
    assert _count(db_session, WebhookEvent) == 1


def test_payment_fields_match_the_contract(client, db_session, customer, frozen_clock):
    client.post(WEBHOOK_URL, json=dishonour_event())

    payment = db_session.execute(select(Payment)).scalar_one()
    assert payment.id == PAYMENT_ID
    assert payment.customer_id == CUSTOMER_ID
    assert payment.amount_cents == 24900
    assert payment.currency == "AUD"
    assert payment.status == "failed"
    # Pinch's code is preserved verbatim; the class is derived from it later.
    assert payment.raw_code == "insufficient-funds"


def test_failed_at_comes_from_the_simulated_clock(
    client, db_session, customer, frozen_clock
):
    """Deterministic because the clock is frozen. A wall-clock read here would
    also break fast-forward, which is what makes the demo possible."""
    client.post(WEBHOOK_URL, json=dishonour_event())

    payment = db_session.execute(select(Payment)).scalar_one()
    assert payment.failed_at == FROZEN_AT


def test_classifier_fields_are_left_for_person_b(
    client, db_session, customer, frozen_clock
):
    """Ingest must not guess a failure_class — that is Person B's classifier,
    and a wrong guess written here would be indistinguishable from a real one."""
    client.post(WEBHOOK_URL, json=dishonour_event())

    payment = db_session.execute(select(Payment)).scalar_one()
    assert payment.failure_class is None
    assert payment.reasoning is None


# --------------------------------------------------------------------------
# Duplicate delivery — the guarantee
# --------------------------------------------------------------------------


def test_duplicate_event_leaves_exactly_one_of_each(
    client, db_session, customer, frozen_clock
):
    first = client.post(WEBHOOK_URL, json=dishonour_event())
    second = client.post(WEBHOOK_URL, json=dishonour_event())

    # Both are accepted: a webhook sender must never be told to retry a
    # duplicate, or it will keep redelivering it.
    assert first.status_code == 200
    assert second.status_code == 200

    assert _count(db_session, Payment) == 1
    assert _count(db_session, WebhookEvent) == 1


def test_duplicate_does_not_mutate_the_existing_payment(
    client, db_session, customer, frozen_clock
):
    """A redelivery must be inert, not merely non-duplicating. If the second
    delivery rewrote failed_at, a fast-forwarded retry schedule would silently
    shift under Person B's engine."""
    client.post(WEBHOOK_URL, json=dishonour_event())

    payment = db_session.execute(select(Payment)).scalar_one()
    original_failed_at = payment.failed_at

    # Someone has since classified it — a redelivery must not wipe that.
    payment.failure_class = "insufficient_funds"
    payment.reasoning = "Timing problem, not intent."
    db_session.commit()

    clock.fast_forward(86_400)
    client.post(WEBHOOK_URL, json=dishonour_event())

    db_session.expire_all()
    payment = db_session.execute(select(Payment)).scalar_one()
    assert payment.failed_at == original_failed_at
    assert payment.failure_class == "insufficient_funds"
    assert payment.reasoning == "Timing problem, not intent."


def test_three_deliveries_still_leave_one_row(
    client, db_session, customer, frozen_clock
):
    for _ in range(3):
        client.post(WEBHOOK_URL, json=dishonour_event())

    assert _count(db_session, Payment) == 1
    assert _count(db_session, WebhookEvent) == 1


def test_distinct_events_for_the_same_payment_are_both_recorded(
    client, db_session, customer, frozen_clock
):
    """Idempotency keys on event_id, not payment_id. A second, genuinely
    different event about the same payment is real and must be stored."""
    client.post(WEBHOOK_URL, json=dishonour_event(event_id="evt_one"))
    client.post(WEBHOOK_URL, json=dishonour_event(event_id="evt_two"))

    assert _count(db_session, WebhookEvent) == 2
    assert _count(db_session, Payment) == 1


def test_unknown_customer_is_recorded_against_a_placeholder(
    client, db_session, frozen_clock
):
    """No `customer` fixture here — the customer does not exist.

    A dishonour for someone we have never seen is still money about to be
    lost. Dropping it (or 500ing on the foreign key) would lose the payment
    entirely, and Pinch would redeliver forever.
    """
    response = client.post(
        WEBHOOK_URL, json=dishonour_event(customer_id="cus_neverseen")
    )
    assert response.status_code == 200

    assert _count(db_session, Payment) == 1
    customer = db_session.execute(select(Customer)).scalar_one()
    assert customer.id == "cus_neverseen"
    assert customer.name == "Unknown customer"


def test_malformed_payload_is_rejected_in_the_contract_error_shape(client, db_session):
    """Missing event_id: there is no idempotency key, so this cannot be
    stored safely and must not be silently accepted."""
    response = client.post(WEBHOOK_URL, json={"event_type": "payment.dishonoured"})

    assert response.status_code == 400
    assert set(response.json()["error"]) == {"code", "message"}
    assert _count(db_session, WebhookEvent) == 0
    assert _count(db_session, Payment) == 0


def test_raw_payload_is_stored_verbatim(client, db_session, customer, frozen_clock):
    """Kept so a mis-parse can be diagnosed without asking Pinch to resend."""
    event = dishonour_event()
    client.post(WEBHOOK_URL, json=event)

    stored = db_session.execute(select(WebhookEvent)).scalar_one()
    assert stored.event_id == EVENT_ID
    assert stored.payload == event
