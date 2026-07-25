"""JSON API for the endpoints docs/CONTRACT.md assigns to Person B.

Only the read/decide endpoints are here. `/webhooks/pinch` and the `/sim/*`
family belong to Person A and are deliberately absent — see dev.py for the
temporary stand-ins the stub store needs.

Errors are shaped `{"error": {"code", "message"}}` per the contract, which is
why this module installs its own handler rather than letting FastAPI's default
`{"detail": ...}` leak into responses the frontend has to parse.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.models.enums import FailureClass, PaymentStatus
from app.models.schemas import (
    DashboardSummary,
    ErrorResponse,
    OutboxMessage,
    Payment,
    PaymentMethod,
    PaymentMethodUpdate,
)
from app.services.store import get_store

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


@router.get("/payments", response_model=list[Payment])
def list_payments(
    status: PaymentStatus | None = None,
    failure_class: FailureClass | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> list[Payment]:
    return get_store().list_payments(
        status=status, failure_class=failure_class, limit=limit
    )


@router.get("/payments/{payment_id}", response_model=Payment)
def get_payment(payment_id: str) -> Payment:
    payment = get_store().get_payment(payment_id)
    if payment is None:
        raise ContractError(404, "payment_not_found", f"No payment {payment_id}")
    return payment


@router.post("/payments/{payment_id}/run-recovery", response_model=Payment)
def run_recovery(payment_id: str) -> Payment:
    """Force the engine to classify and schedule. Idempotent by construction:
    planning is pure and replaces the attempt list rather than appending to it.
    """
    payment = get_store().run_recovery(payment_id)
    if payment is None:
        raise ContractError(404, "payment_not_found", f"No payment {payment_id}")
    return payment


@router.get("/dashboard/summary", response_model=DashboardSummary)
def dashboard_summary() -> DashboardSummary:
    return get_store().summary()


@router.get("/outbox", response_model=list[OutboxMessage])
def outbox(limit: int = Query(default=200, ge=1, le=500)) -> list[OutboxMessage]:
    return get_store().outbox(limit=limit)


@router.get("/customers/{customer_id}/payment-method", response_model=PaymentMethod)
def get_payment_method(customer_id: str) -> PaymentMethod:
    method = get_store().payment_method(customer_id)
    if method is None:
        raise ContractError(404, "customer_not_found", f"No customer {customer_id}")
    return method


@router.post("/customers/{customer_id}/payment-method")
def update_payment_method(
    customer_id: str,
    update: PaymentMethodUpdate,
    payment_id: str | None = None,
) -> dict:
    """Submit new details. Triggers an immediate retry of the customer's open
    failures and reports which ones recovered.
    """
    store = get_store()
    if store.payment_method(customer_id) is None:
        raise ContractError(404, "customer_not_found", f"No customer {customer_id}")

    recovered = store.update_payment_method(customer_id, update, payment_id=payment_id)
    return {
        "recovered_count": len(recovered),
        "recovered_cents": sum(p.amount_cents for p in recovered),
        "recovered_payment_ids": [p.id for p in recovered],
    }
