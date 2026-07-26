"""Simulator endpoints. Dev and demo control only — Person B never calls these."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core import clock
from app.core.db import get_db
from app.sim import seed, service
from app.sim.schemas import FastForwardRequest, ScenarioRequest

router = APIRouter(prefix="/api/v1/sim", tags=["simulator"])


@router.post("/scenarios")
def create_scenario(
    req: ScenarioRequest, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Create a simulated failure and queue its webhook."""
    result = service.create_scenario(db, req)

    # A zero delay should land now, not sit waiting for a fast-forward that a
    # caller had no reason to expect.
    if req.delay_seconds == 0:
        delivered = service.deliver_due(db)
        result["delivered"] = bool(delivered)
        result["deliveries"] = delivered

    return result


@router.post("/fast-forward")
def fast_forward(
    req: FastForwardRequest, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Advance the simulated clock, then run everything that made due.

    Drains synchronously rather than leaving the work to the background
    poller. The poller would get there a second or two later, but that makes
    the dashboard depend on how long you wait before looking — a rehearsal
    would not reproduce, and a screenshot would be a matter of timing. By the
    time this returns, the ledger has settled.
    """
    clock.fast_forward(req.seconds)

    drained = service.drain_due(db)
    # Should be empty: anything still due after a drain means the loop hit its
    # round cap, and surfacing it beats a silently half-settled ledger.
    attempts = service.due_attempts(db)

    return {
        "clock_offset_seconds": clock.offset_seconds(),
        "now": clock.to_iso_z(clock.now()),
        "webhooks_delivered": drained["delivered"],
        "attempts_executed": drained["executed"],
        "drain_rounds": drained["rounds"],
        "attempts_due": attempts,
    }


@router.post("/seed-demo")
def seed_demo(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Reset, then seed ~50 realistic failures across every direct debit class.

    Run this before a demo. Dev-only — see the Ingestion-internal section of
    docs/CONTRACT.md.
    """
    return seed.seed_demo(db)


@router.post("/reset", response_model=None)
def reset(db: Session = Depends(get_db)):
    """Wipe to a known state. Run before every demo."""
    try:
        return service.reset_all(db)
    except OperationalError as exc:
        db.rollback()
        # Fail loudly and fast instead of spinning. Re-running usually works,
        # because whatever held the lock was a request that has since finished.
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "reset_locked",
                    "message": (
                        "Could not acquire the table lock within "
                        f"{service.RESET_LOCK_TIMEOUT}; something holds an open "
                        f"transaction. Retry. ({exc.orig})"
                    ),
                }
            },
        )
