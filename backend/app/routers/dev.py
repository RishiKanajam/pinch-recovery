"""Temporary clock controls for the stub store. DELETE AT INTEGRATION.

docs/CONTRACT.md puts fast-forward and reset at `/api/v1/sim/*` and assigns them
to Person A along with the rest of the simulator. These live under `/dev/*`
instead so that this half of the build can demo a fast-forward without claiming
a contract path Person A is about to implement properly — when their `/sim/*`
router lands, the dashboard's two buttons repoint at it and this file goes away.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.core import clock
from app.services.store import get_store

router = APIRouter(prefix="/dev", tags=["dev-only"])


@router.post("/fast-forward")
def fast_forward(seconds: float = Query(default=259200, gt=0)) -> dict:
    """Advance the simulated clock and run whatever that made due.

    Default is 259200s — the three-day settlement window from the contract,
    collapsed into the length of a button press.
    """
    store = get_store()
    new_now = store.fast_forward(seconds)
    return {
        "now": new_now.isoformat(),
        "offset_seconds": clock.offset_seconds(),
    }


@router.post("/reset")
def reset() -> dict:
    """Clock back to real time, dataset back to the known seeded state."""
    store = get_store()
    store.reset()
    return {"now": clock.now().isoformat(), "offset_seconds": clock.offset_seconds()}
