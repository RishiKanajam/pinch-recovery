"""The demo dataset.

This is the one the judge sees. A seed that quietly drifts — a missing class, a
float amount, fifty identical timestamps — is a demo that looks wrong on screen
with no error anywhere to explain it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.core import clock
from app.models import Customer, Payment, WebhookEvent
from app.sim.seed import EXCLUDED_FROM_SEED, SEED_PLAN, TOTAL_SEEDED

SEED_DEMO = "/api/v1/sim/seed-demo"

# Six, not seven: expired_card is a card-scheme failure and cannot occur on a
# direct debit. See EXCLUDED_FROM_SEED in app/sim/seed.py.
EXPECTED_CLASSES = {
    "insufficient_funds",
    "invalid_account",
    "authority_cancelled",
    "payment_stopped",
    "technical",
    "do_not_honour",
}


@pytest.fixture
def frozen_clock():
    clock.freeze(datetime(2026, 7, 25, 4, 12, 0, tzinfo=timezone.utc))
    yield
    clock.reset()


def _count(session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def test_seeds_about_fifty_payments(client, db_session):
    body = client.post(SEED_DEMO).json()

    assert body["seeded"] == TOTAL_SEEDED
    assert 45 <= body["seeded"] <= 55, "definition of done says ~50"
    assert _count(db_session, Payment) == TOTAL_SEEDED


def test_covers_every_direct_debit_failure_class(client, db_session):
    """Branching on the code is the product's whole argument, so every class
    that can occur on a direct debit has to be on screen. A missing one is a
    bug nobody notices until the demo."""
    body = client.post(SEED_DEMO).json()

    seeded_classes = {entry["failure_class"] for entry in body["by_class"]}
    assert seeded_classes == EXPECTED_CLASSES


def test_every_payment_is_failed_and_unclassified(client, db_session):
    """Ingest must leave failure_class and reasoning for Person B, even at
    scale. Fifty pre-classified rows would mask a broken classifier."""
    client.post(SEED_DEMO)

    payments = db_session.execute(select(Payment)).scalars().all()
    assert all(p.status == "failed" for p in payments)
    assert all(p.failure_class is None for p in payments)
    assert all(p.reasoning is None for p in payments)
    assert all(p.raw_code for p in payments)


def test_raw_codes_come_from_the_seed_plan(client, db_session):
    """Every raw_code must be one the classifier will recognise. A typo here
    classifies as `unknown` and shows one strategy instead of seven."""
    client.post(SEED_DEMO)

    allowed = {code for plan in SEED_PLAN for code in plan.raw_codes}
    codes = set(
        db_session.execute(select(Payment.raw_code)).scalars().all()
    )
    assert codes <= allowed
    # Hyphenated lowercase, as Pinch emits — not AM04/AC01.
    assert all("-" in code and code.islower() for code in codes)


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------


def test_amounts_are_integer_cents_and_plausible(client, db_session):
    client.post(SEED_DEMO)

    amounts = db_session.execute(select(Payment.amount_cents)).scalars().all()
    assert all(isinstance(a, int) for a in amounts)
    # $10 to $2,500 — service-business invoice territory.
    assert all(1_000 <= a <= 250_000 for a in amounts)


def test_at_risk_cents_equals_the_sum_of_seeded_payments(client, db_session):
    """The headline number on the dashboard. If the summary and the ledger
    disagree, one of them is lying to the judge."""
    body = client.post(SEED_DEMO).json()

    ledger_total = db_session.execute(
        select(func.sum(Payment.amount_cents))
    ).scalar_one()
    assert body["at_risk_cents"] == ledger_total
    assert sum(e["amount_cents"] for e in body["by_class"]) == ledger_total


def test_by_class_counts_sum_to_the_total(client, db_session):
    body = client.post(SEED_DEMO).json()
    assert sum(e["count"] for e in body["by_class"]) == body["seeded"]


# --------------------------------------------------------------------------
# Realism
# --------------------------------------------------------------------------


def test_failures_are_spread_over_time(client, db_session, frozen_clock):
    """Fifty failures at the same instant is obviously synthetic, and gives
    the write-off horizon nothing to act on."""
    client.post(SEED_DEMO)

    failed_at = db_session.execute(select(Payment.failed_at)).scalars().all()
    assert len(set(failed_at)) > 20, "timestamps should differ, not cluster"

    span_days = (max(failed_at) - min(failed_at)).total_seconds() / 86_400
    assert span_days > 5


def test_several_customers_with_repeat_failures(client, db_session):
    """Retry budget is per customer, so the demo needs customers who failed
    more than once — otherwise that rule never fires."""
    client.post(SEED_DEMO)

    assert _count(db_session, Customer) > 1
    per_customer = (
        db_session.execute(
            select(func.count())
            .select_from(Payment)
            .group_by(Payment.customer_id)
        )
        .scalars()
        .all()
    )
    assert max(per_customer) > 1


def test_every_payment_has_a_webhook_event_behind_it(client, db_session):
    """Seeded through real ingest, not written directly — so the demo data
    exercises the same path a live dishonour takes."""
    body = client.post(SEED_DEMO).json()
    assert _count(db_session, WebhookEvent) == body["seeded"]


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_seeding_is_deterministic(client, db_session):
    """A rehearsal and the live run must produce the same numbers. Row ids are
    ULIDs and will differ; every count and total must not."""
    first = client.post(SEED_DEMO).json()
    db_session.rollback()  # release read locks before the reset inside seeding
    second = client.post(SEED_DEMO).json()

    assert first["seeded"] == second["seeded"]
    assert first["at_risk_cents"] == second["at_risk_cents"]
    assert first["by_class"] == second["by_class"]


def test_seeding_resets_first(client, db_session):
    """Two runs must not accumulate. The contract says seed-demo resets."""
    client.post(SEED_DEMO)
    db_session.rollback()
    client.post(SEED_DEMO)

    assert _count(db_session, Payment) == TOTAL_SEEDED


def test_card_classes_are_not_seeded(client, db_session):
    """A card cannot expire on a direct debit. Seeding invalid-card would put a
    class on screen that invites "why is there a card expiry in a direct debit
    product?" — a credibility question, not a feature."""
    body = client.post(SEED_DEMO).json()

    seeded = {entry["failure_class"] for entry in body["by_class"]}
    assert seeded.isdisjoint(EXCLUDED_FROM_SEED)

    codes = set(db_session.execute(select(Payment.raw_code)).scalars().all())
    assert "invalid-card" not in codes
    assert "unsupported-card" not in codes
