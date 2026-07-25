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

from app.api.schemas import PinchWebhookEvent
from app.core import clock
from app.core.db import get_db
from app.models import Customer, Payment, WebhookEvent

router = APIRouter(prefix="/api/v1", tags=["webhooks"])

EVENT_DISHONOURED = "payment.dishonoured"


def _error(status: int, code: str, message: str) -> JSONResponse:
    """Error shape from docs/CONTRACT.md."""
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}
    )


def _record_dishonour(db: Session, event: PinchWebhookEvent) -> str:
    """Create or update the failed payment. Returns the payment id."""
    data = event.data

    # The FK requires a customer. A dishonour for someone we have never seen
    # is still real money we are about to lose, so it is recorded against a
    # placeholder rather than dropped. The simulator seeds customers first, so
    # this is the exception path, not the normal one.
    customer = db.get(Customer, data.customer_id)
    if customer is None:
        db.add(Customer(id=data.customer_id, name="Unknown customer"))
        db.flush()

    payment = db.get(Payment, data.payment_id)
    if payment is None:
        payment = Payment(id=data.payment_id, customer_id=data.customer_id)
        db.add(payment)

    payment.amount_cents = data.amount_cents
    payment.currency = data.currency
    payment.status = "failed"
    # Pinch's code, preserved verbatim. failure_class stays NULL — deriving it
    # is Person B's classifier, and a guess written here would be
    # indistinguishable from a real classification.
    payment.raw_code = data.dishonour_code
    # TODO(live): failed_at is ingest time (clock.now), not the envelope's created_at.
    # Correct for mock — reading created_at would reintroduce wall-clock coupling that
    # fast-forward exists to sever. Revisit at the mock→live switch, same checkpoint as
    # the webhook envelope verification.
    payment.failed_at = clock.now()

    return payment.id


# response_model=None because this returns either a plain dict or a
# JSONResponse carrying the contract's error shape, which FastAPI cannot
# express as a single response model.
@router.post("/webhooks/pinch", response_model=None)
def ingest_pinch_webhook(
    payload: dict = Body(...), db: Session = Depends(get_db)
) -> JSONResponse | dict:
    """Ingest a Pinch webhook. Idempotent on event_id."""
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

    payment_id: str | None = None
    if event.event_type == EVENT_DISHONOURED:
        payment_id = _record_dishonour(db, event)

    # TODO: payment.succeeded should mark a prior failure recovered
    # (status="recovered", recovered_at). Recorded in webhook_events either
    # way, so replaying it later loses nothing.

    db.commit()
    return {
        "status": "accepted",
        "event_id": event.event_id,
        "payment_id": payment_id,
    }
