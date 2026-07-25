"""GET /api/v1/payments — the read seam Person B builds against.

Serves the ledger in exactly the shape docs/CONTRACT.md specifies, so their
dashboard binds without translation. Fields Person B owns (failure_class,
reasoning, attempts) are present in the shape from day one and simply NULL or
empty until their engine fills them — a field that appears later is a field
their code has to defend against twice.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.schemas import PaymentList, PaymentOut
from app.core.db import get_db
from app.models import Payment

router = APIRouter(prefix="/api/v1", tags=["payments"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# Cursor is "<failed_at iso>|<id>". Keyset rather than OFFSET: seeding and the
# simulator insert rows between requests, and OFFSET would silently skip or
# repeat rows as the set shifts underneath a paging dashboard.
CURSOR_SEPARATOR = "|"


def _serialise(payment: Payment) -> PaymentOut:
    return PaymentOut(
        id=payment.id,
        customer_id=payment.customer_id,
        customer_name=payment.customer.name if payment.customer else "",
        amount_cents=payment.amount_cents,
        currency=payment.currency,
        status=payment.status,
        raw_code=payment.raw_code,
        failure_class=payment.failure_class,
        failed_at=payment.failed_at,
        recovered_at=payment.recovered_at,
        attempts=payment.attempts,
        reasoning=payment.reasoning,
    )


def _stamp(payment: Payment) -> str:
    """Cursor timestamp for a row. Empty string means the row has no failed_at."""
    if payment.failed_at is None:
        return ""
    return payment.failed_at.isoformat().replace("+00:00", "Z")


def _bad_cursor(cursor: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": "invalid_cursor",
                "message": (
                    f"Cursor {cursor!r} is not one this endpoint issued. Pass "
                    "back a next_cursor verbatim; it is opaque and must not be "
                    "constructed by hand."
                ),
            }
        },
    )


@router.get("/payments", response_model=None)
def list_payments(
    status: str | None = Query(default=None),
    failure_class: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """List payments, newest failure first.

    Ordering is two regions: rows that have failed, newest first, then rows
    with no failed_at at all (a pending payment). Postgres sorts NULL *first*
    under DESC, which would put unfailed payments above real failures and —
    because a keyset predicate on `failed_at < marker` never matches NULL —
    make them unreachable after page one. nulls_last plus an explicit NULL
    branch in the cursor keeps every row on exactly one page.
    """
    stmt = (
        select(Payment)
        .options(selectinload(Payment.attempts), selectinload(Payment.customer))
        .order_by(Payment.failed_at.desc().nulls_last(), Payment.id.desc())
    )

    if status:
        stmt = stmt.where(Payment.status == status)
    if failure_class:
        stmt = stmt.where(Payment.failure_class == failure_class)

    if cursor:
        stamp, separator, last_id = cursor.partition(CURSOR_SEPARATOR)
        if not separator or not last_id:
            return _bad_cursor(cursor)

        if stamp == "":
            # Already inside the undated region; only undated rows remain.
            stmt = stmt.where(Payment.failed_at.is_(None), Payment.id < last_id)
        else:
            try:
                marker = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                # Previously this silently fell through and returned page one
                # again, which reads as a pagination loop rather than an error.
                return _bad_cursor(cursor)
            stmt = stmt.where(
                or_(
                    # Undated rows sort after every dated one, so they are
                    # still ahead of us.
                    Payment.failed_at.is_(None),
                    Payment.failed_at < marker,
                    and_(Payment.failed_at == marker, Payment.id < last_id),
                )
            )

    # One extra row tells us whether another page exists without a count query.
    rows = db.execute(stmt.limit(limit + 1)).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = f"{_stamp(last)}{CURSOR_SEPARATOR}{last.id}"

    return PaymentList(data=[_serialise(p) for p in rows], next_cursor=next_cursor)


@router.get("/payments/{payment_id}", response_model=None)
def get_payment(payment_id: str, db: Session = Depends(get_db)):
    """One payment, with its attempts and reasoning."""
    payment = db.execute(
        select(Payment)
        .options(selectinload(Payment.attempts), selectinload(Payment.customer))
        .where(Payment.id == payment_id)
    ).scalar_one_or_none()

    if payment is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "code": "payment_not_found",
                    "message": f"No payment {payment_id}.",
                }
            },
        )

    return _serialise(payment)
