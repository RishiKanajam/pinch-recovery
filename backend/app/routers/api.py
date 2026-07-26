"""JSON API for the decide/act endpoints docs/CONTRACT.md assigns to Person B.

`GET /payments` and `GET /payments/{id}` are **not** here. Both halves of the
build implemented them — this one against the in-memory store, returning a bare
list — and the contract specifies the paged `{"data", "next_cursor"}` shape with
keyset pagination that `app/api/payments.py` serves off the database. Two
routers claiming one path is not a conflict git can see: FastAPI resolves it
silently by registration order, so whichever was mounted first would win and the
other would simply never run. The contract-correct one wins, and this module
keeps only the endpoints it alone owns.

`/webhooks/pinch` and the `/sim/*` family belong to Person A and are likewise
absent.

Errors are shaped `{"error": {"code", "message"}}` per the contract, which is
why this module installs its own handler rather than letting FastAPI's default
`{"detail": ...}` leak into responses the frontend has to parse.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.schemas import (
    DashboardSummary,
    ErrorResponse,
    OutboxMessage,
    Payment,
    PaymentMethod,
    PaymentMethodUpdate,
)
from app.services.repository import Repository

router = APIRouter(prefix="/api/v1", tags=["recovery"])


class ContractError(HTTPException):
    """An HTTPException that carries a contract-shaped error code."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.message = message


async def contract_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = getattr(exc, "code", None) or _default_code(exc.status_code)
    message = getattr(exc, "message", None) or str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(error={"code": code, "message": message}).model_dump(),
    )


def _default_code(status_code: int) -> str:
    return {
        400: "bad_request",
        404: "not_found",
        422: "validation_failed",
    }.get(status_code, "error")


def repo(db: Session = Depends(get_db)) -> Repository:
    """One repository per request, sharing the request's session and commit."""
    return Repository(db)


@router.post("/payments/{payment_id}/run-recovery", response_model=Payment)
def run_recovery(payment_id: str, store: Repository = Depends(repo)) -> Payment:
    """Force the engine to classify and schedule. Idempotent.

    Planning is pure and already-executed attempts are preserved rather than
    overwritten, so calling this twice leaves the same ladder and the same
    reasoning string — see Repository.run_recovery.
    """
    payment = store.run_recovery(payment_id)
    if payment is None:
        raise ContractError(404, "payment_not_found", f"No payment {payment_id}")
    return payment


@router.get("/dashboard/summary", response_model=DashboardSummary)
def dashboard_summary(store: Repository = Depends(repo)) -> DashboardSummary:
    return store.summary()


@router.get("/outbox", response_model=list[OutboxMessage])
def outbox(
    limit: int = Query(default=200, ge=1, le=500),
    store: Repository = Depends(repo),
) -> list[OutboxMessage]:
    return store.outbox(limit=limit)


@router.get("/customers/{customer_id}/payment-method", response_model=PaymentMethod)
def get_payment_method(
    customer_id: str, store: Repository = Depends(repo)
) -> PaymentMethod:
    method = store.payment_method(customer_id)
    if method is None:
        raise ContractError(404, "customer_not_found", f"No customer {customer_id}")
    return method


@router.post("/customers/{customer_id}/payment-method")
def update_payment_method(
    customer_id: str,
    update: PaymentMethodUpdate,
    payment_id: str | None = None,
    store: Repository = Depends(repo),
) -> dict:
    """Submit new details. Triggers an immediate retry of the customer's open
    failures and reports which ones recovered.
    """
    if store.payment_method(customer_id) is None:
        raise ContractError(404, "customer_not_found", f"No customer {customer_id}")

    recovered = store.update_payment_method(customer_id, update, payment_id=payment_id)
    return {
        "recovered_count": len(recovered),
        "recovered_cents": sum(p.amount_cents for p in recovered),
        "recovered_payment_ids": [p.id for p in recovered],
    }
