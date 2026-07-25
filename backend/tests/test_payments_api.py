"""GET /payments — the read seam, and its pagination.

Person B's dashboard pages through this. A cursor that drops rows or loops
forever is the kind of bug that looks like "the dashboard is missing data"
rather than like a pagination fault, so it is tested directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Customer, Payment

PAYMENTS = "/api/v1/payments"

BASE = datetime(2026, 7, 25, 4, 12, 0, tzinfo=timezone.utc)

CONTRACT_FIELDS = {
    "id", "customer_id", "customer_name", "amount_cents", "currency", "status",
    "raw_code", "failure_class", "failed_at", "recovered_at", "attempts",
    "reasoning",
}


@pytest.fixture
def ledger(db_session):
    """Nine failed payments plus three with no failed_at at all.

    The undated rows are the interesting ones: a `pending` payment has no
    failed_at, and PaymentStatus includes pending.
    """
    customer = Customer(id="cus_pagination", name="Paginated Plumbing")
    db_session.add(customer)
    db_session.flush()

    payments = []
    for index in range(9):
        payments.append(
            Payment(
                id=f"pay_dated_{index:02d}",
                customer_id=customer.id,
                amount_cents=10_000 + index,
                status="failed",
                raw_code="insufficient-funds",
                failed_at=BASE - timedelta(days=index),
            )
        )
    for index in range(3):
        payments.append(
            Payment(
                id=f"pay_undated_{index:02d}",
                customer_id=customer.id,
                amount_cents=500 + index,
                status="pending",
                failed_at=None,
            )
        )

    db_session.add_all(payments)
    db_session.commit()
    return payments


def _walk(client, **params) -> list[str]:
    """Page through the whole list, returning ids in order."""
    ids: list[str] = []
    cursor = None
    for _ in range(50):  # bounded, so a cursor loop fails instead of hanging
        query = dict(params)
        if cursor:
            query["cursor"] = cursor
        body = client.get(PAYMENTS, params=query).json()
        ids.extend(row["id"] for row in body["data"])
        cursor = body["next_cursor"]
        if cursor is None:
            return ids
    raise AssertionError("cursor never terminated — pagination loop")


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def test_response_matches_the_contract_shape(client, ledger):
    body = client.get(PAYMENTS).json()

    assert set(body) == {"data", "next_cursor"}
    assert set(body["data"][0]) == CONTRACT_FIELDS


def test_engine_owned_fields_are_present_but_empty(client, ledger):
    """failure_class/reasoning/attempts must exist from the first response, so
    Person B's code never has to handle a field appearing later."""
    row = client.get(PAYMENTS).json()["data"][0]

    assert row["failure_class"] is None
    assert row["reasoning"] is None
    assert row["attempts"] == []


def test_timestamps_end_in_z(client, ledger):
    rows = client.get(PAYMENTS).json()["data"]
    stamps = [r["failed_at"] for r in rows if r["failed_at"]]
    assert stamps and all(s.endswith("Z") for s in stamps)


# --------------------------------------------------------------------------
# Ordering — the bug
# --------------------------------------------------------------------------


def test_undated_payments_sort_last_not_first(client, ledger):
    """Postgres sorts NULL first under DESC. Unfailed payments appearing above
    real failures would put `pending` rows at the top of the dashboard."""
    rows = client.get(PAYMENTS).json()["data"]

    dated = [i for i, r in enumerate(rows) if r["failed_at"] is not None]
    undated = [i for i, r in enumerate(rows) if r["failed_at"] is None]
    assert dated and undated
    assert max(dated) < min(undated)


def test_dated_payments_are_newest_first(client, ledger):
    rows = client.get(PAYMENTS).json()["data"]
    stamps = [r["failed_at"] for r in rows if r["failed_at"]]
    assert stamps == sorted(stamps, reverse=True)


# --------------------------------------------------------------------------
# Pagination — every row exactly once
# --------------------------------------------------------------------------


def test_paging_yields_every_row_exactly_once(client, ledger):
    """The regression test. Before nulls_last, the three undated rows appeared
    on page one and were then unreachable, and a full page of them looped."""
    everything = _walk(client, limit=100)
    paged = _walk(client, limit=2)

    assert paged == everything
    assert len(paged) == len(ledger)
    assert len(set(paged)) == len(paged), "a row was returned twice"


def test_paging_through_the_undated_region_terminates(client, ledger):
    """A page size smaller than the undated block is what turned the old
    cursor into an infinite loop."""
    assert len(_walk(client, limit=1)) == len(ledger)


def test_next_cursor_is_null_on_the_last_page(client, ledger):
    body = client.get(PAYMENTS, params={"limit": 100}).json()
    assert body["next_cursor"] is None


def test_malformed_cursor_is_rejected_not_ignored(client, ledger):
    """Silently returning page one again reads as a pagination loop."""
    response = client.get(PAYMENTS, params={"cursor": "not-a-cursor"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


def test_status_filter(client, ledger):
    rows = client.get(PAYMENTS, params={"status": "pending"}).json()["data"]
    assert len(rows) == 3
    assert all(r["status"] == "pending" for r in rows)


def test_failure_class_filter_matches_nothing_before_classification(client, ledger):
    rows = client.get(
        PAYMENTS, params={"failure_class": "insufficient_funds"}
    ).json()["data"]
    assert rows == []


def test_single_payment_is_returned_bare(client, ledger):
    """Not wrapped in `data` — the contract's Core objects shape."""
    body = client.get(f"{PAYMENTS}/pay_dated_00").json()
    assert set(body) == CONTRACT_FIELDS
    assert body["id"] == "pay_dated_00"
