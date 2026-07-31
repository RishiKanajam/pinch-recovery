"""The live loop: force a dishonour at Pinch, poll for it, ingest it.

Nothing here opens a socket. The live client is driven through an httpx
MockTransport standing in for the Pinch sandbox, which is what lets these
assert the *requests* — the `#insufficient-funds` in the description, the
`eventType` filter, the second call for the event body — rather than only that
the code runs. Those requests are the part that has to be right before anyone
points this at a real sandbox with an audience watching.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import sessionmaker

from app.api.schemas import EVENT_BANK_RESULTS, PinchWebhookEvent
from app.core import clock
from app.core.config import settings
from app.models import Customer, Payment, WebhookEvent
from app.services import event_ingest
from app.services.pinch_client import (
    TEST_ACCOUNT_NUMBER,
    TEST_BSB,
    LivePinchClient,
    MockPinchClient,
)


# --------------------------------------------------------------------------
# A stand-in Pinch sandbox
# --------------------------------------------------------------------------


class FakePinch:
    """Routes the handful of endpoints the live loop touches."""

    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.events = events or []
        self.payments: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        body = json.loads(request.content) if request.content else {}

        if path.endswith("/payers"):
            return httpx.Response(201, json={"id": "pyr_fake001"})
        if path.endswith("/sources"):
            return httpx.Response(201, json={"id": "src_fake001"})
        if path.endswith("/payments") and request.method == "POST":
            self.payments.append(body)
            return httpx.Response(201, json={"id": "pmt_fake001", **body})
        if path.endswith("/events"):
            return httpx.Response(
                200,
                json={
                    "page": 1,
                    "pageSize": 50,
                    "totalPages": 1,
                    "totalItems": len(self.events),
                    "data": [
                        {
                            "id": e["id"],
                            "type": e["type"],
                            "eventDate": e.get("eventDate"),
                            "metadata": {"dishonourCount": 1},
                        }
                        for e in self.events
                    ],
                },
            )
        if "/events/" in path:
            event_id = path.rsplit("/", 1)[-1]
            for event in self.events:
                if event["id"] == event_id:
                    return httpx.Response(200, json=event)
            return httpx.Response(404, json={})

        return httpx.Response(404, json={})

    def bodies_for(self, path_suffix: str) -> list[dict[str, Any]]:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.url.path.endswith(path_suffix) and r.content
        ]


def live_client(engine, pinch: FakePinch) -> LivePinchClient:
    """A live client wired to the fake sandbox and the test database."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    client = LivePinchClient(
        application_id="app_1",
        secret_key="sec_1",
        session_factory=factory,
    )
    client._token = "tok_test"
    # Far enough ahead that no test's fast-forward triggers a token refresh
    # against a transport that has no token endpoint.
    client._token_expires_at = clock.now().replace(year=clock.now().year + 1)
    client._client = httpx.Client(
        transport=httpx.MockTransport(pinch.handler),
        base_url=settings.PINCH_API_BASE,
    )
    return client


def bank_results_event(
    event_id: str = "evt_test001",
    payment_id: str = "pmt_test001",
    payer_id: str = "pyr_test001",
    code: str | None = "insufficient-funds",
    amount: int = 24900,
) -> dict[str, Any]:
    """A `bank-results` event in the camelCase shape the Events API returns."""
    entry: dict[str, Any] = {
        "id": payment_id,
        "status": "dishonoured" if code else "approved",
        "amount": amount,
        "payer": {"id": payer_id, "email": "sandbox@example.com"},
    }
    if code:
        entry["dishonour"] = {"type": code, "description": "Refer to drawer"}
    return {
        "id": event_id,
        "type": EVENT_BANK_RESULTS,
        "eventDate": "2026-08-01T08:00:00.000Z",
        "data": {"payments": [entry]},
    }


# --------------------------------------------------------------------------
# Both key spellings parse
# --------------------------------------------------------------------------


def test_camel_case_event_parses_the_same_as_pascal_case():
    """Webhooks arrive PascalCase, the Events API answers camelCase.

    Both reach the same ingest path, so a parser that only understood one
    would drop every event from the other — and look exactly like Pinch
    sending nothing.
    """
    camel = PinchWebhookEvent.model_validate(bank_results_event())
    pascal = PinchWebhookEvent.model_validate(
        {
            "Id": "evt_test001",
            "Type": EVENT_BANK_RESULTS,
            "EventDate": "2026-08-01T08:00:00.000Z",
            "Data": {
                "Payments": [
                    {
                        "Id": "pmt_test001",
                        "Status": "dishonoured",
                        "Amount": 24900,
                        "Payer": {"Id": "pyr_test001", "Email": "sandbox@example.com"},
                        "Dishonour": {
                            "Type": "insufficient-funds",
                            "Description": "Refer to drawer",
                        },
                    }
                ]
            },
        }
    )

    assert camel.event_id == pascal.event_id
    assert camel.data.payments[0].dishonour.type == pascal.data.payments[0].dishonour.type
    assert camel.data.payments[0].payer.id == pascal.data.payments[0].payer.id


def test_unknown_keys_survive_parsing():
    """Pinch adding a field must not cost us the fields we do understand."""
    raw = bank_results_event()
    raw["someNewField"] = {"whatever": 1}
    event = PinchWebhookEvent.model_validate(raw)
    assert event.event_id == "evt_test001"


# --------------------------------------------------------------------------
# Forcing a dishonour code
# --------------------------------------------------------------------------


def test_forced_code_is_embedded_in_the_description(engine, db_session):
    """The `#code` in the description IS the mechanism — see the docs.

    Assert the request body, not the return value: a payment created without
    the hash lands in the sandbox as an ordinary successful debit, and the
    demo silently shows nothing failing.
    """
    pinch = FakePinch()
    result = live_client(engine, pinch).create_test_payment(
        amount_cents=24900,
        raw_code="insufficient-funds",
        customer_name="Marina Auto Detailing",
    )

    assert result.accepted
    assert result.forced_code == "insufficient-funds"
    assert result.pinch_payment_id == "pmt_fake001"

    body = pinch.payments[0]
    assert "#insufficient-funds" in body["description"]
    assert body["amount"] == 24900
    assert body["payerId"] == "pyr_fake001"


def test_test_payment_creates_a_payer_and_a_bank_source(engine, db_session):
    pinch = FakePinch()
    result = live_client(engine, pinch).create_test_payment(
        amount_cents=9900, raw_code="invalid-account"
    )

    source_body = pinch.bodies_for("/sources")[0]
    assert source_body["sourceType"] == "bank-account"
    assert source_body["bankAccountBsb"] == TEST_BSB
    assert source_body["bankAccountNumber"] == TEST_ACCOUNT_NUMBER

    customer = db_session.get(Customer, result.customer_id)
    assert customer is not None
    assert customer.pinch_payer_id == "pyr_fake001"
    assert customer.pinch_source_id == "src_fake001"
    # Only the tail of the account number is ever persisted.
    assert customer.bank_account_last4 == TEST_ACCOUNT_NUMBER[-4:]


def test_existing_pinch_payer_is_reused(engine, db_session):
    """A second test payment must not create a second payer for one customer."""
    db_session.add(
        Customer(
            id="cus_reuse",
            name="Bondi Physio",
            email="hi@example.com",
            pinch_payer_id="pyr_existing",
            pinch_source_id="src_existing",
        )
    )
    db_session.commit()

    pinch = FakePinch()
    live_client(engine, pinch).create_test_payment(
        amount_cents=15900, raw_code="insufficient-funds", customer_id="cus_reuse"
    )

    assert pinch.bodies_for("/payers") == []
    assert pinch.payments[0]["payerId"] == "pyr_existing"


def test_no_code_creates_a_payment_expected_to_succeed(engine, db_session):
    pinch = FakePinch()
    live_client(engine, pinch).create_test_payment(amount_cents=4900, raw_code=None)
    assert "#" not in pinch.payments[0]["description"]


def test_forcing_a_dishonour_is_refused_against_live_money(engine, db_session, monkeypatch):
    """There is no forced dishonour in production — only a real debit."""
    monkeypatch.setattr(
        settings, "PINCH_API_BASE", "https://api.getpinch.com.au/live/"
    )
    pinch = FakePinch()
    result = live_client(engine, pinch).create_test_payment(
        amount_cents=24900, raw_code="insufficient-funds"
    )

    assert not result.accepted
    assert result.error_code == "live_environment"
    assert pinch.requests == [], "nothing may be sent to the live environment"


def test_pinch_rejection_is_reported_not_raised(engine, db_session):
    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "bad amount"}, text="bad amount")

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    client = LivePinchClient(
        application_id="a", secret_key="b", session_factory=factory
    )
    client._token = "tok"
    client._token_expires_at = clock.now().replace(year=clock.now().year + 1)
    client._client = httpx.Client(
        transport=httpx.MockTransport(reject), base_url=settings.PINCH_API_BASE
    )

    result = client.create_test_payment(amount_cents=1, raw_code="insufficient-funds")
    assert not result.accepted
    assert result.error_code == "http_422"


# --------------------------------------------------------------------------
# Reading events back
# --------------------------------------------------------------------------


def test_list_events_filters_by_type_and_page_size(engine, db_session):
    pinch = FakePinch(events=[bank_results_event()])
    summaries = live_client(engine, pinch).list_events(
        event_type=EVENT_BANK_RESULTS, page=2, page_size=25
    )

    request = [r for r in pinch.requests if r.url.path.endswith("/events")][0]
    assert request.url.params["eventType"] == EVENT_BANK_RESULTS
    assert request.url.params["pageSize"] == "25"
    assert request.url.params["page"] == "2"

    assert [s.id for s in summaries] == ["evt_test001"]
    assert summaries[0].type == EVENT_BANK_RESULTS


def test_page_size_is_clamped_to_the_documented_maximum(engine, db_session):
    pinch = FakePinch()
    live_client(engine, pinch).list_events(page_size=5000)
    request = [r for r in pinch.requests if r.url.path.endswith("/events")][0]
    assert request.url.params["pageSize"] == "500"


def test_get_event_returns_the_full_envelope(engine, db_session):
    pinch = FakePinch(events=[bank_results_event()])
    envelope = live_client(engine, pinch).get_event("evt_test001")
    assert envelope["data"]["payments"][0]["dishonour"]["type"] == "insufficient-funds"


def test_a_failed_list_returns_nothing_rather_than_raising(engine, db_session):
    """A poller that raises on a bad response stops ingesting entirely."""

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    client = LivePinchClient(
        application_id="a", secret_key="b", session_factory=factory
    )
    client._token = "tok"
    client._token_expires_at = clock.now().replace(year=clock.now().year + 1)
    client._client = httpx.Client(
        transport=httpx.MockTransport(boom), base_url=settings.PINCH_API_BASE
    )

    assert client.list_events() == []
    assert client.get_event("evt_x") is None


# --------------------------------------------------------------------------
# Polling ingest
# --------------------------------------------------------------------------


def test_poll_ingests_a_dishonour_into_the_ledger(engine, db_session):
    pinch = FakePinch(events=[bank_results_event()])
    result = event_ingest.poll_events(db_session, live_client(engine, pinch))

    assert result["ingested"] == ["evt_test001"]
    assert result["payment_ids"] == ["pmt_test001"]

    payment = db_session.get(Payment, "pmt_test001")
    assert payment is not None
    assert payment.status == "failed"
    assert payment.raw_code == "insufficient-funds"
    # Classification is the engine's job and happens after ingest, exactly as
    # it does for a webhook.
    assert payment.failure_class is None


def test_polling_twice_ingests_once(engine, db_session):
    """Idempotency is the property that makes polling safe on a timer."""
    pinch = FakePinch(events=[bank_results_event()])
    client = live_client(engine, pinch)

    first = event_ingest.poll_events(db_session, client)
    second = event_ingest.poll_events(db_session, client)

    assert first["ingested"] == ["evt_test001"]
    assert second["ingested"] == []
    assert second["skipped"] == 1

    rows = db_session.query(WebhookEvent).filter_by(event_id="evt_test001").all()
    assert len(rows) == 1


def test_an_event_already_delivered_by_webhook_is_not_fetched_again(
    engine, db_session, client
):
    """Both ingest routes may run at once; the second must cost one call.

    The event body is only fetched for ids the ledger has not seen, so a
    webhook that already delivered an event makes the poll skip it before
    spending a request on it.
    """
    envelope = bank_results_event()
    response = client.post("/api/v1/webhooks/pinch", json=envelope)
    assert response.status_code == 200

    pinch = FakePinch(events=[envelope])
    result = event_ingest.poll_events(db_session, live_client(engine, pinch))

    assert result["skipped"] == 1
    assert result["ingested"] == []
    assert not [r for r in pinch.requests if "/events/" in r.url.path]


def test_poll_reports_a_broken_event_without_losing_the_batch(engine, db_session):
    good = bank_results_event(event_id="evt_good", payment_id="pmt_good")
    broken = {"id": "evt_broken", "type": EVENT_BANK_RESULTS}  # no Data at all
    pinch = FakePinch(events=[broken, good])

    result = event_ingest.poll_events(db_session, live_client(engine, pinch))

    assert result["failed"] == 1
    assert result["ingested"] == ["evt_good"]
    assert db_session.get(Payment, "pmt_good") is not None


def test_a_success_event_recovers_the_payment(engine, db_session):
    """The recovery half of the loop: the bank says yes, days later."""
    pinch = FakePinch(events=[bank_results_event()])
    client_ = live_client(engine, pinch)
    event_ingest.poll_events(db_session, client_)

    pinch.events = [
        bank_results_event(event_id="evt_test002", code=None)  # approved
    ]
    event_ingest.poll_events(db_session, client_)

    payment = db_session.get(Payment, "pmt_test001")
    db_session.refresh(payment)
    assert payment.status == "recovered"
    assert payment.recovered_at is not None


# --------------------------------------------------------------------------
# The same loop, in mock mode
# --------------------------------------------------------------------------


def test_mock_test_payment_is_visible_to_a_poll_only_after_settlement(
    engine, db_session
):
    """Mock mode runs the identical sequence, on the simulated clock.

    The dishonour is not visible the instant the debit is submitted — a real
    one takes days — so the poll finds nothing until the settlement window has
    passed. Rehearsing on the mock therefore rehearses the real order of
    events, not a faster version of a different one.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    mock = MockPinchClient(session_factory=factory)

    result = mock.create_test_payment(
        amount_cents=24900,
        raw_code="insufficient-funds",
        customer_name="Marina Auto Detailing",
    )
    assert result.accepted

    try:
        assert event_ingest.poll_events(db_session, mock)["ingested"] == []

        clock.fast_forward(60 * 60 * 24 * 3)
        polled = event_ingest.poll_events(db_session, mock)
    finally:
        clock.reset()

    assert len(polled["ingested"]) == 1
    payment_id = polled["payment_ids"][0]
    payment = db_session.get(Payment, payment_id)
    assert payment.raw_code == "insufficient-funds"
    assert payment.status == "failed"


@pytest.mark.parametrize("mode", ["mock", "live"])
def test_the_endpoints_answer_in_both_modes(client, mode, monkeypatch):
    """The demo sequence is the same two calls whichever mode is configured."""
    monkeypatch.setattr(settings, "PINCH_MODE", mode)
    if mode == "live":
        # Live mode with no credentials must answer with the reason, not a 500.
        # A missing environment variable is the likeliest thing to go wrong on
        # the day, and it should read as a sentence rather than a stack trace.
        response = client.post("/api/v1/pinch/poll", json={})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "pinch_not_configured"
        return

    created = client.post(
        "/api/v1/pinch/test-payments",
        json={"amount_cents": 24900, "raw_code": "insufficient-funds"},
    )
    assert created.status_code == 200
    assert created.json()["forced_code"] == "insufficient-funds"

    polled = client.post("/api/v1/pinch/poll", json={})
    assert polled.status_code == 200
    assert polled.json()["mode"] == "mock"
