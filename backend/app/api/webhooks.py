"""POST /api/v1/webhooks/pinch — ingest.

The idempotency guarantee (CLAUDE.md rule 3) works by inserting into
webhook_events *first* and letting the unique index on event_id reject the
duplicate. It deliberately does not check "does this event_id exist?" before
inserting: between that check and the insert, a concurrent redelivery can slip
through, and Pinch retries webhooks in exactly the bursts that make that race
real. The constraint is the mechanism; this handler just catches it.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.schemas import (
    STATUS_DISHONOURED,
    STATUS_SUCCEEDED,
    PinchPaymentResult,
    PinchWebhookEvent,
)
from app.core import clock
from app.core.db import get_db
from app.models import Attempt, Customer, Payment, WebhookEvent

router = APIRouter(prefix="/api/v1", tags=["webhooks"])


def _error(status: int, code: str, message: str) -> JSONResponse:
    """Error shape from docs/CONTRACT.md."""
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}
    )


def _resolve_customer(db: Session, item: PinchPaymentResult) -> Customer | None:
    """Find or create the local customer for a payment entry's `Payer`.

    `Payer.Id` is used directly as our own primary key when the customer is
    new — the same "trust the envelope's id" rule the original mock-only
    ingest used for `customer_id`, now applied to Pinch's own payer id. A
    payment with no `Payer` (should not happen on a real `bank-results`
    entry, but `Payer` is optional in the schema so a malformed one does not
    500) is recorded with no customer link at all.
    """
    if item.payer is None:
        return None

    customer_id = item.payer.id
    customer = db.get(Customer, customer_id)
    if customer is None:
        # A dishonour for someone we have never seen is still real money
        # about to be lost, so it is recorded against a placeholder rather
        # than dropped. The simulator seeds customers first, so this is the
        # exception path, not the normal one.
        customer = Customer(
            id=customer_id,
            name="Unknown customer",
            email=item.payer.email,
            pinch_payer_id=customer_id,
        )
        db.add(customer)
        db.flush()
    elif customer.pinch_payer_id is None:
        # A customer created locally (mock seed, or signed up before their
        # first live payment) now has a Pinch payer confirmed for them —
        # cache it so a later retry_payment/update_payment_method knows who
        # to call without creating a second Payer in Pinch.
        customer.pinch_payer_id = customer_id
    return customer


def _resolve_payment(db: Session, item: PinchPaymentResult, customer_id: str | None) -> Payment:
    """Find or create the local payment row for a payment entry.

    `Id` is used directly as our own primary key when the payment is new —
    same rule as `_resolve_customer` above.
    """
    payment = db.get(Payment, item.id)
    if payment is None:
        payment = Payment(id=item.id, customer_id=customer_id, pinch_payment_id=item.id)
        db.add(payment)
    elif payment.pinch_payment_id is None:
        payment.pinch_payment_id = item.id
    return payment


def _record_dishonour(db: Session, item: PinchPaymentResult) -> str:
    """Create or update the failed payment. Returns the payment id."""
    customer = _resolve_customer(db, item)
    payment = _resolve_payment(db, item, customer.id if customer else None)

    # A dishonour for a payment that has already reached a terminal state is
    # the settlement of a presentation the engine has since moved past — the
    # last retry answering after the write-off horizon already fired. Record
    # the event (webhook_events already holds it) but leave the payment alone.
    #
    # Without this the payment is resurrected from `written_off` back to
    # `failed` with no attempts left to rescue it, and sticks there. Worse,
    # whether that happens depends on whether the settlement lands before or
    # after the write-off, which moves with fast-forward granularity — so the
    # same seed told different stories depending on how the demo was driven.
    if payment.status in ("written_off", "recovered"):
        return payment.id

    payment.amount_cents = item.amount
    payment.status = "failed"
    # Pinch's code, preserved verbatim. failure_class stays NULL — deriving it
    # is Person B's classifier, and a guess written here would be
    # indistinguishable from a real classification.
    payment.raw_code = item.dishonour.type if item.dishonour else None
    # failed_at is ingest time (clock.now), not the envelope's EventDate:
    # reading EventDate would reintroduce wall-clock coupling that
    # fast-forward exists to sever.
    payment.failed_at = clock.now()

    return payment.id


def _record_success(db: Session, item: PinchPaymentResult) -> str:
    """Mark a prior failure recovered. Returns the payment id.

    A payment settling on the first presentation (no prior dishonour) has no
    local row yet — `_resolve_payment` creates one, already `recovered`,
    rather than requiring a dishonour to have been seen first.
    """
    customer = _resolve_customer(db, item)
    payment = _resolve_payment(db, item, customer.id if customer else None)

    payment.amount_cents = item.amount
    payment.status = "recovered"
    payment.recovered_at = clock.now()

    return payment.id


# response_model=None because this returns either a plain dict or a
# JSONResponse carrying the contract's error shape, which FastAPI cannot
# express as a single response model.
@router.post("/webhooks/pinch", response_model=None)
def ingest_pinch_webhook(
    payload: dict = Body(...), db: Session = Depends(get_db)
) -> JSONResponse | dict:
    """Ingest a Pinch webhook. Idempotent on event_id.

    `bank-results` is a batch: one event can report several payments'
    outcomes. The idempotency guarantee is still per event_id — a redelivered
    batch is entirely a no-op — but a single successful delivery processes
    every entry in `data.payments`.
    """
    try:
        event = PinchWebhookEvent.model_validate(payload)
    except ValidationError as exc:
        return _error(400, "invalid_payload", f"Malformed Pinch event: {exc.errors()}")

    # Insert the ledger row first, before anything else touches the database.
    db.add(WebhookEvent(event_id=event.event_id, payload=payload))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        # 200, not 409: a webhook sender told to retry will keep redelivering.
        return {"status": "duplicate", "event_id": event.event_id}

    payment_ids: list[str] = []
    for item in event.data.payments:
        if item.status == STATUS_DISHONOURED:
            payment_ids.append(_record_dishonour(db, item))
        elif item.status in STATUS_SUCCEEDED:
            payment_ids.append(_record_success(db, item))
        # Any other status (e.g. "processing", "scheduled") is not a
        # settlement outcome and is intentionally not acted on here.

    db.commit()
    return {
        "status": "accepted",
        "event_id": event.event_id,
        # Kept for the single-payment case every existing caller (the
        # simulator, the tests) relies on; `payment_ids` covers the batch.
        "payment_id": payment_ids[0] if payment_ids else None,
        "payment_ids": payment_ids,
    }
