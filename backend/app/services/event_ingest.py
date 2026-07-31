"""Polling ingest: pull events from Pinch instead of waiting to be pushed.

A webhook needs a public URL. On a laptop behind NAT that means a tunnel, and
a tunnel that dies mid-demo looks exactly like the product not working — so
the ingest path that has to be reliable is the one we control. `GET /events`
returns everything that happened; this asks for it and replays each event
through the same handler `POST /webhooks/pinch` calls.

Two properties make that safe to run on a timer:

1. **Idempotency is shared.** Both paths insert into `webhook_events` first and
   let the unique index reject the duplicate, so an event that arrives by
   webhook *and* by poll is processed exactly once. That is also what makes it
   safe to leave both enabled at the same time.

2. **Listing is cheap, fetching is not.** `GET /events` returns summaries
   without the payments in them, so an event's contents cost a second request.
   Ids already in the ledger are filtered out before that request is made,
   which keeps a poll over an unchanged account down to one call.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import EVENT_BANK_RESULTS
from app.api.webhooks import ingest_pinch_webhook
from app.models import WebhookEvent
from app.services.pinch_client import PinchClient, get_pinch_client

logger = logging.getLogger(__name__)


def _already_ingested(db: Session, event_ids: list[str]) -> set[str]:
    if not event_ids:
        return set()
    return set(
        db.execute(
            select(WebhookEvent.event_id).where(WebhookEvent.event_id.in_(event_ids))
        )
        .scalars()
        .all()
    )


def poll_events(
    db: Session,
    client: PinchClient | None = None,
    *,
    event_type: str | None = EVENT_BANK_RESULTS,
    page_size: int = 50,
    pages: int = 1,
) -> dict[str, Any]:
    """Fetch recent events and ingest the ones not seen before.

    Filtered to `bank-results` by default: it is the only event type that
    carries a settlement outcome, and every other type would be fetched, parsed
    and discarded. Pass `event_type=None` to poll everything.

    Returns a summary rather than raising, so a poller tick that hits a bad
    event keeps the rest of the batch.
    """
    if client is None:
        from sqlalchemy.orm import Session as _Session

        bind = db.get_bind()
        client = get_pinch_client(session_factory=lambda: _Session(bind=bind))

    listed: list[str] = []
    ingested: list[str] = []
    payment_ids: list[str] = []
    skipped = 0
    failed = 0

    for page in range(1, pages + 1):
        summaries = client.list_events(
            event_type=event_type, page=page, page_size=page_size
        )
        if not summaries:
            break

        listed.extend(s.id for s in summaries)
        seen = _already_ingested(db, [s.id for s in summaries])

        for summary in summaries:
            if summary.id in seen:
                skipped += 1
                continue

            envelope = client.get_event(summary.id)
            if envelope is None:
                failed += 1
                continue

            result = ingest_pinch_webhook(payload=envelope, db=db)
            if not isinstance(result, dict):
                # The handler returns the contract's error shape as a
                # JSONResponse for a malformed envelope. Counted, not raised:
                # one unparseable event must not stop the poll.
                failed += 1
                logger.warning("Event %s was rejected by ingest", summary.id)
                continue

            if result.get("status") == "duplicate":
                skipped += 1
                continue

            ingested.append(summary.id)
            payment_ids.extend(result.get("payment_ids") or [])

    if ingested:
        logger.info(
            "Polled Pinch: ingested %d new event(s) covering %d payment(s)",
            len(ingested),
            len(payment_ids),
        )

    return {
        "listed": len(listed),
        "ingested": ingested,
        # Events already in the ledger — the normal steady state, and proof the
        # poll is not double-counting anything the webhook already delivered.
        "skipped": skipped,
        "failed": failed,
        "payment_ids": payment_ids,
    }
