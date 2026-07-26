"""FastAPI application entrypoint.

Both halves of the build are mounted here:

  * Person A — ingestion and simulation: `/webhooks/pinch`, `/sim/*`, and the
    paged `GET /payments` read seam.
  * Person B — engine and interface: `run-recovery`, `/dashboard/summary`,
    `/outbox`, the update-details flow, and the server-rendered pages.

Router order matters and is not arbitrary. `payments_router` is registered
before `recovery_router` so the contract's paged `GET /api/v1/payments` is the
one that answers; FastAPI matches in registration order, and both halves
originally implemented that path.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.api.payments import router as payments_router
from app.api.webhooks import router as webhooks_router
from app.core import clock
from app.core.config import settings
from app.core.db import SessionLocal
from app.routers.api import contract_error_handler
from app.routers.api import router as recovery_router
from app.routers.web import router as web_router
from app.services.classifier import get_strategy_table
from app.services.repository import Repository
from app.services.scheduler import DueActionPoller
from app.sim.routes import router as sim_router

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_poller: DueActionPoller | None = None


def _execute_due_actions() -> int:
    """Poller tick: run whatever the simulated clock has made due.

    Opens its own session rather than borrowing a request's — there is no
    request. Person A's `/sim/fast-forward` reports due attempts but does not
    execute them, on the grounds that applying retry budgets and max_attempts
    belongs to the engine; this is the other half of that handshake.
    """
    db = SessionLocal()
    try:
        return Repository(db).execute_due()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Validate the strategy table at boot, then start the due-action poller.

    Loading the table here means a malformed strategies.yaml is a failed boot
    rather than a wrong decision discovered mid-demo.
    """
    global _poller

    table = get_strategy_table()
    logger.info(
        "Strategy table loaded: %d classes, %d raw codes mapped",
        len(table.classes),
        len(table.known_codes()),
    )

    if settings.ENABLE_POLLER:
        _poller = DueActionPoller(
            _execute_due_actions, interval_seconds=settings.POLLER_INTERVAL_SECONDS
        )
        _poller.start()
        logger.info(
            "Due-action poller started (%.1fs)", settings.POLLER_INTERVAL_SECONDS
        )

    yield

    if _poller is not None:
        _poller.stop()
        _poller = None


app = FastAPI(title="Pinch Recovery Engine", version="0.1.0", lifespan=lifespan)

# Ingestion and simulation (Person A). payments_router first — see module docstring.
app.include_router(payments_router)
app.include_router(webhooks_router)
app.include_router(sim_router)

# Engine and interface (Person B).
app.include_router(recovery_router)
app.include_router(web_router)

# The contract specifies `{"error": {"code", "message"}}` for every error, so the
# handler is installed for all HTTPExceptions rather than only the custom one —
# otherwise FastAPI's own 404s and 422s would leak `{"detail": ...}`.
app.add_exception_handler(HTTPException, contract_error_handler)


@app.get("/health")
def health() -> dict:
    """Liveness, plus the simulated-clock offset.

    The offset is here so a demo can confirm a fast-forward actually landed
    without reading the database.
    """
    return {
        "status": "ok",
        "pinch_mode": settings.PINCH_MODE,
        "clock_offset_seconds": clock.offset_seconds(),
    }
