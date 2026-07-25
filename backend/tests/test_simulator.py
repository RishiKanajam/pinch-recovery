"""The simulator, and the property the whole demo rests on.

A direct debit dishonour arrives days after the debit. The demo cannot wait
three days and neither can this test — one fast-forward collapses the
settlement window into milliseconds of real time.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.core import clock
from app.models import Customer, Payment, SimulatedWebhook, WebhookEvent

SCENARIOS = "/api/v1/sim/scenarios"
FAST_FORWARD = "/api/v1/sim/fast-forward"
RESET = "/api/v1/sim/reset"

THREE_DAYS = 259_200
FROZEN_AT = datetime(2026, 7, 25, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def frozen_clock():
    clock.freeze(FROZEN_AT)
    yield FROZEN_AT
    clock.reset()


def _count(session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def scenario_body(**overrides) -> dict:
    body = {
        "customer_name": "Marina Auto Detailing",
        "amount_cents": 24900,
        "outcome": "dishonour",
        "raw_code": "insufficient-funds",
        "delay_seconds": THREE_DAYS,
        "webhook_deliveries": 1,
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------
# The settlement window
# --------------------------------------------------------------------------


def test_scenario_does_not_deliver_before_its_time(client, db_session, frozen_clock):
    """The dishonour is in the future. Nothing should have landed yet — if it
    had, the fast-forward below would prove nothing."""
    response = client.post(SCENARIOS, json=scenario_body())
    assert response.status_code == 200

    body = response.json()
    assert body["delivered"] is False
    assert body["payment_id"].startswith("pay_")

    assert _count(db_session, Payment) == 0
    assert _count(db_session, WebhookEvent) == 0
    # The customer exists immediately; only the failure is in the future.
    assert _count(db_session, Customer) == 1
    assert _count(db_session, SimulatedWebhook) == 1


def test_three_day_delay_lands_after_one_fast_forward(
    client, db_session, frozen_clock
):
    """The property the demo depends on: three days of settlement, collapsed
    into one call, in milliseconds of real time."""
    created = client.post(SCENARIOS, json=scenario_body()).json()
    payment_id = created["payment_id"]

    started = time.monotonic()
    response = client.post(FAST_FORWARD, json={"seconds": THREE_DAYS})
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    body = response.json()
    assert body["clock_offset_seconds"] == THREE_DAYS
    assert len(body["webhooks_delivered"]) == 1
    assert body["webhooks_delivered"][0]["result"] == "accepted"

    payment = db_session.execute(select(Payment)).scalar_one()
    assert payment.id == payment_id
    assert payment.status == "failed"
    assert payment.raw_code == "insufficient-funds"
    assert payment.amount_cents == 24900

    # Three days of simulated time in well under a second of real time.
    assert elapsed < 2.0


def test_failed_at_reflects_the_fast_forwarded_clock(
    client, db_session, frozen_clock
):
    """The dishonour is recorded as happening three days later, not at the
    moment the scenario was created."""
    client.post(SCENARIOS, json=scenario_body())
    client.post(FAST_FORWARD, json={"seconds": THREE_DAYS})

    payment = db_session.execute(select(Payment)).scalar_one()
    assert (payment.failed_at - FROZEN_AT).total_seconds() == THREE_DAYS


def test_partial_fast_forward_does_not_deliver(client, db_session, frozen_clock):
    """Two days is not three. An off-by-one here would make every scheduled
    action fire early during the demo."""
    client.post(SCENARIOS, json=scenario_body())
    response = client.post(FAST_FORWARD, json={"seconds": THREE_DAYS - 1})

    assert response.json()["webhooks_delivered"] == []
    assert _count(db_session, Payment) == 0


def test_zero_delay_delivers_immediately(client, db_session, frozen_clock):
    """No fast-forward needed when there is no settlement window to wait for."""
    response = client.post(SCENARIOS, json=scenario_body(delay_seconds=0))

    assert response.json()["delivered"] is True
    assert _count(db_session, Payment) == 1


# --------------------------------------------------------------------------
# Idempotency through the simulator
# --------------------------------------------------------------------------


def test_two_deliveries_leave_one_payment(client, db_session, frozen_clock):
    """webhook_deliveries=2 fires the identical event twice. The ledger must
    absorb it — one payment, one webhook_events row."""
    client.post(SCENARIOS, json=scenario_body(webhook_deliveries=2))
    response = client.post(FAST_FORWARD, json={"seconds": THREE_DAYS})

    deliveries = response.json()["webhooks_delivered"]
    assert len(deliveries) == 2
    assert [d["result"] for d in deliveries] == ["accepted", "duplicate"]
    assert deliveries[0]["event_id"] == deliveries[1]["event_id"]

    assert _count(db_session, Payment) == 1
    assert _count(db_session, WebhookEvent) == 1


def test_delivery_is_not_repeated_on_a_second_fast_forward(
    client, db_session, frozen_clock
):
    """A delivered webhook is done. Re-firing it on every subsequent
    fast-forward would resend the same event all demo long."""
    client.post(SCENARIOS, json=scenario_body())
    client.post(FAST_FORWARD, json={"seconds": THREE_DAYS})
    second = client.post(FAST_FORWARD, json={"seconds": THREE_DAYS})

    assert second.json()["webhooks_delivered"] == []
    assert _count(db_session, Payment) == 1


# --------------------------------------------------------------------------
# Reset
# --------------------------------------------------------------------------


def test_reset_clears_tables_and_the_clock(client, db_session, frozen_clock):
    client.post(SCENARIOS, json=scenario_body(delay_seconds=0))
    client.post(FAST_FORWARD, json={"seconds": THREE_DAYS})
    assert _count(db_session, Payment) == 1

    # Release this session's read locks before /sim/reset runs. The assertion
    # above leaves it idle-in-transaction holding ACCESS SHARE on payments,
    # and TRUNCATE needs ACCESS EXCLUSIVE — the two deadlock, and the test
    # hangs rather than failing. An artifact of holding a long-lived session
    # alongside the app's; real requests open and close one per call.
    db_session.rollback()

    response = client.post(RESET)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    assert _count(db_session, Payment) == 0
    assert _count(db_session, Customer) == 0
    assert _count(db_session, WebhookEvent) == 0
    assert _count(db_session, SimulatedWebhook) == 0
    assert clock.offset_seconds() == 0


def test_existing_customer_is_reused_not_duplicated(client, db_session, frozen_clock):
    """Seeding many failures for one customer must not create many customers,
    or the retry budget (which is per customer) would never trigger."""
    first = client.post(SCENARIOS, json=scenario_body(delay_seconds=0)).json()
    client.post(
        SCENARIOS,
        json=scenario_body(delay_seconds=0, customer_id=first["customer_id"]),
    )

    assert _count(db_session, Customer) == 1
    assert _count(db_session, Payment) == 2


def test_reset_fails_fast_when_tables_are_locked(client, db_session, frozen_clock):
    """A blocked reset must error, not hang.

    Before the lock_timeout this test would never finish: TRUNCATE waits
    forever on ACCESS EXCLUSIVE while another transaction holds ACCESS SHARE.
    A hang in front of an audience is worse than a failure, because it looks
    identical to a crash and offers nothing to react to.
    """
    # Deliberately leave this session idle-in-transaction holding a read lock.
    db_session.execute(select(func.count()).select_from(Payment)).scalar_one()

    started = time.monotonic()
    response = client.post(RESET)
    elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "reset_locked"
    # Bounded by the 2s timeout, not unbounded.
    assert elapsed < 10.0

    db_session.rollback()
