"""Dev-only ASGI entrypoint for Person B's slice. REPLACE AT INTEGRATION.

`app/main.py` is shared ground and Person A owns the real application factory
(DB session lifecycle, Alembic check, Pinch client mode switch, the `/sim/*` and
`/webhooks/pinch` routers). This file exists only so that the engine, dashboard,
outbox and update-details flow can be run and demoed before that lands.

At integration:
  1. Person A's `app/main.py` mounts `app.routers.api:router` and
     `app.routers.web:router` — the two lines below.
  2. `app.routers.dev` is deleted; the dashboard's clock buttons repoint at the
     real `/api/v1/sim/fast-forward` and `/api/v1/sim/reset`.
  3. `app.services.store` is replaced by a repository over Person A's tables.
     Everything above the store reads through its methods, so nothing else moves.
  4. This file is deleted.

Run it with:
    uvicorn app.dev_main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.routers import api, dev, web
from app.routers.api import contract_error_handler
from app.services.classifier import get_strategy_table
from app.services.scheduler import DueActionPoller
from app.services.store import get_store

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_poller: DueActionPoller | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Load the strategy table and start the due-action poller.

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

    store = get_store()
    logger.info("Store seeded with %d payments", len(store.list_payments(limit=999)))

    _poller = DueActionPoller(store.execute_due, interval_seconds=2.0)
    _poller.start()

    yield

    _poller.stop()


app = FastAPI(
    title="Pinch Recovery Engine — Person B slice (dev)",
    description=(
        "Engine and interface running against the in-memory stub store. "
        "Ingestion and simulation endpoints belong to Person A and are not here."
    ),
    version="0.1.0-dev",
    lifespan=lifespan,
)

app.include_router(api.router)
app.include_router(dev.router)
app.include_router(web.router)

# The contract specifies `{"error": {"code", "message"}}` for every error, so the
# handler is installed for all HTTPExceptions rather than only the custom one —
# otherwise FastAPI's own 404s and 422s would leak `{"detail": ...}`.
app.add_exception_handler(HTTPException, contract_error_handler)

@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True}
