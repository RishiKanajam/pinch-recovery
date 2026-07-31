"""The live Pinch loop: force a dishonour, poll for it, classify it.

These two endpoints are the demo path when `PINCH_MODE=live`, and they are
deliberately mode-agnostic: in mock mode the same calls drive the simulator, so
the sequence rehearsed on a laptop is the sequence run against the sandbox. The
only difference is which side of `PinchClient` answers.

`POST /api/v1/pinch/test-payments` is the entry point Pinch's test environment
gives us — a real payment whose description carries `#insufficient-funds`, which
the sandbox then dishonours with exactly that code. `POST /api/v1/pinch/poll`
is the ingest half: `GET /events`, fetch what is new, run it through the same
handler the webhook uses.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.schemas import EVENT_BANK_RESULTS
from app.core.config import settings
from app.core.db import get_db
from app.services import event_ingest
from app.services.pinch_client import get_pinch_client
from app.services.repository import Repository

router = APIRouter(prefix="/api/v1/pinch", tags=["pinch"])


class TestPaymentRequest(BaseModel):
    amount_cents: int = Field(gt=0)
    # Pinch's own code, verbatim: "insufficient-funds", "invalid-account",
    # "technical-error"… The sandbox reads it out of the description. Null
    # creates a payment expected to succeed.
    raw_code: str | None = None
    customer_id: str | None = None
    customer_name: str | None = None
    description: str | None = None


class PollRequest(BaseModel):
    # Only bank-results carries a settlement outcome; everything else would be
    # fetched and discarded. Null polls every type.
    event_type: str | None = EVENT_BANK_RESULTS
    page_size: int = Field(default=50, ge=1, le=500)
    pages: int = Field(default=1, ge=1, le=20)
    # Classify and act on whatever the poll ingested, in the same call. On by
    # default because a dishonour sitting unclassified in the ledger is not
    # what anybody polled for.
    run_recovery: bool = True


def _unconfigured() -> JSONResponse | None:
    """A legible 400 when live mode has no credentials to use.

    Without this the first outbound call raises out of the endpoint and the
    demo shows a 500 — the least diagnosable possible symptom of a missing
    environment variable.
    """
    if settings.PINCH_MODE != "live" or settings.pinch_credentials_present:
        return None
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": "pinch_not_configured",
                "message": (
                    "PINCH_MODE=live but PINCH_APPLICATION_ID / PINCH_SECRET_KEY "
                    "are unset. Set them in backend/.env.local, or switch back "
                    "to PINCH_MODE=mock."
                ),
            }
        },
    )


def _client(db: Session):
    """A client bound to this request's database.

    Both implementations need it: the mock resolves everything locally, and the
    live one caches Pinch's payer/source/payment ids against our rows.
    """
    from sqlalchemy.orm import Session as _Session

    bind = db.get_bind()
    return get_pinch_client(session_factory=lambda: _Session(bind=bind))


@router.post("/test-payments", response_model=None)
def create_test_payment(
    req: TestPaymentRequest, db: Session = Depends(get_db)
) -> JSONResponse | dict[str, Any]:
    """Present a debit that will fail with a specific dishonour code.

    Returns once Pinch (or the simulator) has accepted the instruction. The
    dishonour itself arrives later as an event — poll for it.
    """
    misconfigured = _unconfigured()
    if misconfigured is not None:
        return misconfigured

    result = _client(db).create_test_payment(
        amount_cents=req.amount_cents,
        raw_code=req.raw_code,
        customer_id=req.customer_id,
        customer_name=req.customer_name,
        description=req.description,
    )

    if not result.accepted:
        return JSONResponse(
            status_code=502 if result.error_code and result.error_code.startswith("http") else 400,
            content={
                "error": {
                    "code": result.error_code or "test_payment_rejected",
                    "message": result.message,
                }
            },
        )

    return {
        "status": "submitted",
        "mode": settings.PINCH_MODE,
        "forced_code": result.forced_code,
        "pinch_payment_id": result.pinch_payment_id,
        "customer_id": result.customer_id,
        "message": result.message,
    }


@router.post("/poll", response_model=None)
def poll(
    req: PollRequest | None = None, db: Session = Depends(get_db)
) -> JSONResponse | dict[str, Any]:
    """Pull new events from Pinch and ingest them. Idempotent by construction.

    Safe to run alongside the webhook endpoint: both insert into
    `webhook_events` first, so an event delivered twice by two routes still
    produces one ledger row and one payment.
    """
    misconfigured = _unconfigured()
    if misconfigured is not None:
        return misconfigured

    req = req or PollRequest()
    result = event_ingest.poll_events(
        db,
        event_type=req.event_type,
        page_size=req.page_size,
        pages=req.pages,
    )

    if req.run_recovery:
        store = Repository(db)
        result["classified"] = store.classify_unclassified()
        result["actions_executed"] = store.execute_due()

    result["mode"] = settings.PINCH_MODE
    return result
