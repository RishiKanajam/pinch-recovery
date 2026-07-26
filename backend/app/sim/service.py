"""Simulator mechanics: queue webhooks, deliver the due ones, reset.

A due-check rather than APScheduler. Everything the demo needs happens when
/sim/fast-forward is called, and a background scheduler polling real seconds
would be a second source of truth about time — precisely what clock.now()
exists to prevent.
"""

from __future__ import annotations

import logging

from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.schemas import EVENT_BANK_RESULTS, STATUS_APPROVED, STATUS_DISHONOURED
from app.api.webhooks import ingest_pinch_webhook
from app.core import clock
from app.core.clock import to_iso_z
from app.core.ids import new_id
from app.models import Attempt, Base, Customer, SimulatedWebhook

# Same envelope Pinch's real `bank-results` webhook uses (see
# app/api/schemas.py), so mock and live ingest through identical code. Both
# outcomes arrive as one event type; the per-payment result is `Status`.
STATUS_FOR_OUTCOME = {
    "dishonour": STATUS_DISHONOURED,
    "success": STATUS_APPROVED,
}


def _build_envelope(
    event_id: str,
    payment_id: str,
    customer_id: str,
    amount_cents: int,
    status: str,
    raw_code: str | None,
    created_at: datetime,
) -> dict[str, Any]:
    payment_entry: dict[str, Any] = {
        "Id": payment_id,
        "Status": status,
        "Amount": amount_cents,
        "Payer": {"Id": customer_id},
    }
    # A success carries no dishonour code; a repeat failure carries the
    # original one, because the underlying cause has not changed.
    if raw_code:
        payment_entry["Dishonour"] = {"Type": raw_code, "Description": raw_code}

    return {
        "Id": event_id,
        "Type": EVENT_BANK_RESULTS,
        "EventDate": to_iso_z(created_at),
        "Data": {"Payments": [payment_entry]},
    }


# A re-presented direct debit settles in roughly two business days — the same
# fast-forwardable window as the original dishonour, so `+3d` in the UI covers
# a retry's settlement as well as the first failure.
SETTLEMENT_SECONDS = 172_800


def schedule_settlement(
    db: Session,
    *,
    payment_id: str,
    customer_id: str,
    amount_cents: int,
    succeeded: bool,
    raw_code: str | None = None,
    delay_seconds: float = SETTLEMENT_SECONDS,
) -> str:
    """Queue the webhook reporting how a re-presented debit settled.

    A retry does not resolve when it is submitted — the bank answers days
    later, and Pinch reports that answer on the next `bank-results` batch.
    Routing the outcome back through the same ingest path means a recovery is
    recorded by exactly the code that will record it in production, rather
    than the engine marking its own homework.
    """
    event_id = new_id("evt")
    deliver_at = clock.now() + _seconds(delay_seconds)

    envelope = _build_envelope(
        event_id=event_id,
        payment_id=payment_id,
        customer_id=customer_id,
        amount_cents=amount_cents,
        status=STATUS_FOR_OUTCOME["success" if succeeded else "dishonour"],
        # A success carries no dishonour code; a repeat failure carries the
        # original one, because the underlying cause has not changed.
        raw_code=None if succeeded else raw_code,
        created_at=deliver_at,
    )

    db.add(
        SimulatedWebhook(
            event_id=event_id,
            payload=envelope,
            deliver_at=deliver_at,
            delivery_number=1,
        )
    )
    return event_id


def create_scenario(db: Session, req) -> dict[str, Any]:
    """Create the customer if needed and queue the webhook delivery."""
    customer_id = req.customer_id
    if customer_id is None:
        customer = Customer(
            name=req.customer_name or "Simulated Customer",
            email="sim@example.com",
        )
        db.add(customer)
        db.flush()
        customer_id = customer.id
    elif db.get(Customer, customer_id) is None:
        # An explicit id that does not exist yet — create it under that id so
        # the caller's reference stays valid.
        db.add(
            Customer(id=customer_id, name=req.customer_name or "Simulated Customer")
        )
        db.flush()

    payment_id = new_id("pay")
    event_id = new_id("evt")
    deliver_at = clock.now() + _seconds(req.delay_seconds)

    envelope = _build_envelope(
        event_id=event_id,
        payment_id=payment_id,
        customer_id=customer_id,
        amount_cents=req.amount_cents,
        status=STATUS_FOR_OUTCOME[req.outcome],
        raw_code=req.raw_code,
        created_at=deliver_at,
    )

    # One row per delivery, all sharing one event_id. The ledger absorbs the
    # duplicates; that is the property being exercised.
    for n in range(1, req.webhook_deliveries + 1):
        db.add(
            SimulatedWebhook(
                event_id=event_id,
                payload=envelope,
                deliver_at=deliver_at,
                delivery_number=n,
            )
        )

    db.commit()

    return {
        "payment_id": payment_id,
        "customer_id": customer_id,
        "event_id": event_id,
        "scheduled_for": to_iso_z(deliver_at),
        "webhook_deliveries": req.webhook_deliveries,
        "delivered": False,
    }


def _seconds(value: float):
    from datetime import timedelta

    return timedelta(seconds=value)


def deliver_due(db: Session) -> list[dict[str, Any]]:
    """Deliver every queued webhook whose time has come.

    Calls the real ingest handler rather than issuing an HTTP request to
    ourselves: same code path, same idempotency, no dependency on the server's
    own address.
    """
    now = clock.now()
    pending = (
        db.execute(
            select(SimulatedWebhook)
            .where(
                SimulatedWebhook.delivered_at.is_(None),
                SimulatedWebhook.deliver_at <= now,
            )
            .order_by(
                SimulatedWebhook.deliver_at, SimulatedWebhook.delivery_number
            )
        )
        .scalars()
        .all()
    )

    delivered: list[dict[str, Any]] = []
    for item in pending:
        result = ingest_pinch_webhook(payload=item.payload, db=db)
        status = result.get("status") if isinstance(result, dict) else "error"

        # Set after the handler runs: a duplicate rolls the session back, and
        # a delivered_at written beforehand would be discarded with it.
        item.delivered_at = clock.now()
        db.commit()

        delivered.append(
            {
                "event_id": item.event_id,
                "delivery_number": item.delivery_number,
                "payment_id": item.payload["Data"]["Payments"][0]["Id"],
                # "duplicate" here is a success: the ledger refused to
                # double-count a redelivery.
                "result": status,
            }
        )

    return delivered


def drain_due(db: Session, max_rounds: int = 25) -> dict[str, Any]:
    """Run everything the clock has made due, until nothing is left.

    One pass is not enough, because the two halves feed each other: delivering
    a webhook creates a payment, executing an attempt presents a retry whose
    settlement arrives as another webhook, and that settlement can schedule the
    next attempt. Looping until both report zero is what makes the ledger
    stable by the time the response is written.

    Without this the work still happens — the background poller picks it up a
    second or two later — but the numbers on the dashboard depend on how long
    you wait before looking, which makes a rehearsal unreproducible and a
    screenshot a matter of timing.

    Executing attempts is Person B's `Repository.execute_due`, called here
    rather than reimplemented: retry budgets, max_attempts and the write-off
    horizon all live on their side, and this only decides *when* to ask.
    """
    # Imported here rather than at module scope: app.services.repository pulls
    # in the strategy engine and classifier, and the simulator must not become
    # a load-order dependency of the engine.
    from app.services.repository import Repository

    delivered: list[dict[str, Any]] = []
    executed = 0
    rounds = 0

    for rounds in range(1, max_rounds + 1):
        batch = deliver_due(db)
        delivered.extend(batch)
        ran = Repository(db).execute_due()
        executed += ran
        if not batch and not ran:
            break
    else:
        logger.warning(
            "drain_due hit its %d-round cap with work still pending; the "
            "ledger may still be settling.",
            max_rounds,
        )

    return {"delivered": delivered, "executed": executed, "rounds": rounds}


def due_attempts(db: Session) -> list[dict[str, Any]]:
    """Attempts whose scheduled_for has passed.

    Reported, not executed. Executing an attempt means applying retry budgets
    and max_attempts — Person B's strategy engine, on the far side of the
    seam. Surfacing them here lets their poller pick them up and lets a demo
    show that fast-forward made them due.
    """
    now = clock.now()
    rows = (
        db.execute(
            select(Attempt)
            .where(
                Attempt.status == "scheduled",
                Attempt.scheduled_for.is_not(None),
                Attempt.scheduled_for <= now,
            )
            .order_by(Attempt.scheduled_for)
        )
        .scalars()
        .all()
    )
    return [
        {
            "attempt_id": a.id,
            "payment_id": a.payment_id,
            "action": a.action,
            "scheduled_for": to_iso_z(a.scheduled_for),
        }
        for a in rows
    ]


# TRUNCATE needs ACCESS EXCLUSIVE. If anything else holds an open transaction
# on these tables, the default behaviour is to wait forever — and a hang is the
# worst possible live failure, because it is indistinguishable from a crash and
# gives you nothing to react to. Two seconds converts that into an immediate,
# legible error you can see and re-run. If the lock is not free quickly it will
# not become free by waiting.
RESET_LOCK_TIMEOUT = "2s"


def reset_all(db: Session) -> dict[str, Any]:
    """Truncate every table and return the clock to real time.

    Raises OperationalError (lock_not_available) rather than blocking if the
    tables are locked; the route turns that into a 503.
    """
    tables = [t.name for t in Base.metadata.sorted_tables]
    db.execute(text(f"SET LOCAL lock_timeout = '{RESET_LOCK_TIMEOUT}'"))
    db.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    db.commit()
    clock.reset()
    return {"status": "ok", "tables_truncated": sorted(tables)}
