"""Simulator mechanics: queue webhooks, deliver the due ones, reset.

A due-check rather than APScheduler. Everything the demo needs happens when
/sim/fast-forward is called, and a background scheduler polling real seconds
would be a second source of truth about time — precisely what clock.now()
exists to prevent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.webhooks import ingest_pinch_webhook
from app.core import clock
from app.core.clock import to_iso_z
from app.core.ids import new_id
from app.models import Attempt, Base, Customer, SimulatedWebhook

# Same envelope docs/CONTRACT.md specifies for inbound Pinch events, so mock
# and live ingest through identical code.
EVENT_TYPES = {
    "dishonour": "payment.dishonoured",
    "success": "payment.succeeded",
}


def _build_envelope(
    event_id: str,
    event_type: str,
    payment_id: str,
    customer_id: str,
    amount_cents: int,
    raw_code: str | None,
    created_at: datetime,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "created_at": to_iso_z(created_at),
        "data": {
            "payment_id": payment_id,
            "customer_id": customer_id,
            "amount_cents": amount_cents,
            "currency": "AUD",
            "dishonour_code": raw_code,
        },
    }


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
        event_type=EVENT_TYPES[req.outcome],
        payment_id=payment_id,
        customer_id=customer_id,
        amount_cents=req.amount_cents,
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
                "payment_id": item.payload["data"]["payment_id"],
                # "duplicate" here is a success: the ledger refused to
                # double-count a redelivery.
                "result": status,
            }
        )

    return delivered


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
