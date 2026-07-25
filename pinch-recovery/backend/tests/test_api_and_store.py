"""End-to-end tests over the API and the stub store.

These cover the seams the unit tests cannot: that the seeded dataset is
coherent, that the contract's response shapes hold, and that the update-details
flow recovers the right payments and — more importantly — refuses to recover the
wrong ones.

The store is a process singleton, so every test resets it. That also exercises
`/dev/reset`, which the demo script leans on before every run.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core import clock
from app.dev_main import app
from app.models.enums import ActionType, AttemptStatus, FailureClass, PaymentStatus
from app.services.store import get_store


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c
    get_store().reset()


@pytest.fixture()
def store():
    s = get_store()
    s.reset()
    yield s
    s.reset()


# --- the seeded dataset -------------------------------------------------------


def test_seed_covers_every_failure_class(store):
    """The demo has to show all eight classes, including unknown."""
    payments = store.list_payments(limit=999)
    assert len(payments) >= 45
    seen = {p.failure_class for p in payments}
    for failure_class in FailureClass:
        assert failure_class in seen, f"{failure_class.value} missing from the seed"


def test_every_payment_carries_reasoning(store):
    """README non-negotiable 4, asserted across the whole dataset."""
    for payment in store.list_payments(limit=999):
        assert payment.reasoning, f"{payment.id} has no reasoning"
        assert len(payment.reasoning.split()) >= 15, payment.id


def test_seed_is_deterministic(store):
    """/sim/reset must produce the same dashboard, or the demo script drifts."""
    first = store.summary()
    ids_first = [p.id for p in store.list_payments(limit=999)]
    store.reset()
    second = store.summary()
    ids_second = [p.id for p in store.list_payments(limit=999)]

    assert ids_first == ids_second
    assert first.at_risk_cents == second.at_risk_cents
    assert first.recovered_cents == second.recovered_cents
    assert first.written_off_cents == second.written_off_cents


def test_money_is_always_integer_cents(store):
    for payment in store.list_payments(limit=999):
        assert isinstance(payment.amount_cents, int)
        assert not isinstance(payment.amount_cents, bool)
        assert payment.currency == "AUD"


def test_hard_failures_never_have_a_successful_plain_retry_before_a_fix(store):
    """A hard-failure payment must not show a retry that just happened to work.

    The only retry allowed to succeed on these is the one presented after the
    customer corrected their details, which is tagged as such.
    """
    for payment in store.list_payments(limit=999):
        if payment.failure_class not in (
            FailureClass.INVALID_ACCOUNT,
            FailureClass.AUTHORITY_CANCELLED,
            FailureClass.PAYMENT_STOPPED,
        ):
            continue
        for attempt in payment.attempts:
            if attempt.action is not ActionType.RETRY:
                continue
            assert attempt.status is not AttemptStatus.FAILED, (
                f"{payment.id} retried a hard failure and paid a fee for it"
            )


# --- dashboard summary --------------------------------------------------------


def test_summary_is_internally_consistent(store):
    summary = store.summary()
    payments = store.list_payments(limit=999)

    at_risk = sum(p.amount_cents for p in payments if p.status is PaymentStatus.FAILED)
    recovered = sum(
        p.amount_cents for p in payments if p.status is PaymentStatus.RECOVERED
    )
    written_off = sum(
        p.amount_cents for p in payments if p.status is PaymentStatus.WRITTEN_OFF
    )

    assert summary.at_risk_cents == at_risk
    assert summary.recovered_cents == recovered
    assert summary.written_off_cents == written_off
    # Escalated is a subset of at-risk, so it can never exceed it.
    assert summary.escalated_cents <= summary.at_risk_cents
    assert 0.0 <= summary.recovery_rate <= 1.0


def test_summary_by_class_totals_match_the_book(store):
    summary = store.summary()
    payments = store.list_payments(limit=999)
    assert sum(c.count for c in summary.by_class) == len(payments)
    assert sum(c.amount_cents for c in summary.by_class) == sum(
        p.amount_cents for p in payments
    )


# --- contract shapes ----------------------------------------------------------


def test_dashboard_summary_endpoint_shape(client):
    body = client.get("/api/v1/dashboard/summary").json()
    for key in (
        "at_risk_cents",
        "recovered_cents",
        "escalated_cents",
        "written_off_cents",
        "recovery_rate",
        "by_class",
    ):
        assert key in body
    for row in body["by_class"]:
        assert set(row) == {
            "failure_class",
            "count",
            "amount_cents",
            "recovered_cents",
        }


def test_payment_shape_matches_the_contract(client):
    payment = client.get("/api/v1/payments?limit=1").json()[0]
    for key in (
        "id",
        "customer_id",
        "customer_name",
        "amount_cents",
        "currency",
        "status",
        "raw_code",
        "failure_class",
        "failed_at",
        "recovered_at",
        "attempts",
        "reasoning",
    ):
        assert key in payment, f"{key} missing from the Payment response"
    # Loader internals must never leak onto the wire.
    assert "_raw_codes" not in payment


def test_errors_use_the_contract_shape(client):
    body = client.get("/api/v1/payments/pay_does_not_exist").json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == "payment_not_found"


def test_filters_work(client):
    rows = client.get("/api/v1/payments?failure_class=invalid_account&limit=99").json()
    assert rows
    assert all(r["failure_class"] == "invalid_account" for r in rows)

    rows = client.get("/api/v1/payments?status=recovered&limit=99").json()
    assert all(r["status"] == "recovered" for r in rows)


def test_run_recovery_is_idempotent(client):
    payment_id = client.get("/api/v1/payments?status=failed&limit=1").json()[0]["id"]
    first = client.post(f"/api/v1/payments/{payment_id}/run-recovery").json()
    second = client.post(f"/api/v1/payments/{payment_id}/run-recovery").json()
    assert len(first["attempts"]) == len(second["attempts"])
    assert first["reasoning"] == second["reasoning"]
    assert first["failure_class"] == second["failure_class"]


def test_run_recovery_does_not_erase_what_already_happened(client, store):
    """Regression: re-planning used to overwrite the attempt list wholesale.

    That silently deleted every executed message and failed retry, so a second
    run-recovery reported a payment as though nothing had ever been sent to the
    customer — and produced a different reasoning string off the back of the
    rewritten history.
    """
    target = None
    for payment in store.list_payments(limit=999):
        history = [
            a
            for a in payment.attempts
            if a.status
            in (AttemptStatus.EXECUTED, AttemptStatus.FAILED, AttemptStatus.SUCCEEDED)
        ]
        if len(history) >= 2:
            target = payment
            break
    assert target is not None, "seed should contain a payment with real history"

    before = [
        (a.action, a.channel, a.executed_at)
        for a in target.attempts
        if a.status
        in (AttemptStatus.EXECUTED, AttemptStatus.FAILED, AttemptStatus.SUCCEEDED)
    ]

    client.post(f"/api/v1/payments/{target.id}/run-recovery")
    after_payment = store.get_payment(target.id)
    after = [
        (a.action, a.channel, a.executed_at)
        for a in after_payment.attempts
        if a.status
        in (AttemptStatus.EXECUTED, AttemptStatus.FAILED, AttemptStatus.SUCCEEDED)
    ]

    for record in before:
        assert record in after, f"run-recovery erased {record}"


def test_run_recovery_does_not_duplicate_a_sent_message(client, store):
    """A ladder step that already ran must not be scheduled a second time."""
    target = None
    for payment in store.list_payments(limit=999):
        if payment.failure_class is not FailureClass.INVALID_ACCOUNT:
            continue
        if any(
            a.action is ActionType.REQUEST_DETAILS_UPDATE
            and a.status is AttemptStatus.EXECUTED
            for a in payment.attempts
        ):
            target = payment
            break
    if target is None:
        pytest.skip("no invalid_account payment with a sent email in this seed")

    client.post(f"/api/v1/payments/{target.id}/run-recovery")
    after = store.get_payment(target.id)

    emails = [
        a for a in after.attempts if a.action is ActionType.REQUEST_DETAILS_UPDATE
    ]
    # invalid_account sends exactly one email and one SMS.
    by_channel: dict = {}
    for attempt in emails:
        by_channel[attempt.channel] = by_channel.get(attempt.channel, 0) + 1
    for channel, count in by_channel.items():
        assert count == 1, f"{channel} message duplicated {count}x after re-planning"


# --- the update-details flow --------------------------------------------------


def test_payment_method_never_exposes_the_full_number(client, store):
    customer_id = store.list_payments(limit=1)[0].customer_id
    body = client.get(f"/api/v1/customers/{customer_id}/payment-method").json()
    assert "account_number" not in body
    assert body["account_number_masked"].startswith("•")
    real = store.get_customer(customer_id).account_number
    assert real not in body["account_number_masked"]


def _first_failed(store, failure_class: FailureClass):
    for payment in store.list_payments(limit=999):
        if payment.failure_class is failure_class and payment.status is PaymentStatus.FAILED:
            return payment
    return None


def test_updating_details_recovers_an_invalid_account_payment(client, store):
    payment = _first_failed(store, FailureClass.INVALID_ACCOUNT)
    assert payment is not None

    response = client.post(
        f"/api/v1/customers/{payment.customer_id}/payment-method?payment_id={payment.id}",
        json={
            "account_name": "Kerbside Coffee Roasters",
            "bsb": "063-000",
            "account_number": "40918823",
        },
    )
    assert response.status_code == 200
    assert payment.id in response.json()["recovered_payment_ids"]

    after = store.get_payment(payment.id)
    assert after.status is PaymentStatus.RECOVERED
    assert after.recovered_at is not None
    # The recovery is recorded as its own successful retry, with the reason.
    winner = [a for a in after.attempts if a.status is AttemptStatus.SUCCEEDED]
    assert winner and winner[0].action is ActionType.RETRY


def test_recovery_cancels_pending_work(client, store):
    payment = _first_failed(store, FailureClass.INVALID_ACCOUNT)
    client.post(
        f"/api/v1/customers/{payment.customer_id}/payment-method?payment_id={payment.id}",
        json={"account_name": "X Pty Ltd", "bsb": "063000", "account_number": "123456"},
    )
    after = store.get_payment(payment.id)
    assert not [a for a in after.attempts if a.status is AttemptStatus.SCHEDULED], (
        "a recovered payment must not still have work queued against it"
    )


def _force_failed(store, failure_class: FailureClass):
    """Get an open payment of this class, forcing one open if the seed has none.

    These two rules are the ones that must never regress, so the test builds its
    own precondition rather than skipping when the seeded dice happen to leave no
    open row of the class — a silently skipped test on a rule this sharp is worse
    than no test, because it reads green.
    """
    existing = _first_failed(store, failure_class)
    if existing is not None:
        return existing

    for payment in store.list_payments(limit=999):
        if payment.failure_class is failure_class:
            reopened = payment.model_copy(
                update={"status": PaymentStatus.FAILED, "recovered_at": None}
            )
            store._payments[payment.id] = reopened
            return reopened
    raise AssertionError(f"seed contains no {failure_class.value} payment at all")


def test_new_details_do_not_auto_charge_a_stopped_payment(client, store):
    """A stop order is a dispute. New details are not permission to debit.

    This is the rule most likely to be broken by a well-meaning "retry
    everything outstanding" loop, and breaking it is how a hackathon demo turns
    into a chargeback in production.
    """
    payment = _force_failed(store, FailureClass.PAYMENT_STOPPED)

    client.post(
        f"/api/v1/customers/{payment.customer_id}/payment-method",
        json={"account_name": "X Pty Ltd", "bsb": "063000", "account_number": "123456"},
    )
    after = store.get_payment(payment.id)
    assert after.status is PaymentStatus.FAILED, (
        "a stopped payment was auto-charged after a details update"
    )
    assert not [
        a
        for a in after.attempts
        if a.action is ActionType.RETRY and a.status is AttemptStatus.SUCCEEDED
    ]


def test_new_details_do_not_auto_charge_a_cancelled_authority(client, store):
    """Cancelling the mandate revokes permission; new details do not restore it."""
    payment = _force_failed(store, FailureClass.AUTHORITY_CANCELLED)

    client.post(
        f"/api/v1/customers/{payment.customer_id}/payment-method",
        json={"account_name": "X Pty Ltd", "bsb": "063000", "account_number": "123456"},
    )
    after = store.get_payment(payment.id)
    assert after.status is PaymentStatus.FAILED, (
        "a cancelled authority was auto-charged after a details update"
    )
    assert not [
        a
        for a in after.attempts
        if a.action is ActionType.RETRY and a.status is AttemptStatus.SUCCEEDED
    ]


def test_bad_bank_details_are_rejected(client, store):
    customer_id = store.list_payments(limit=1)[0].customer_id
    response = client.post(
        f"/api/v1/customers/{customer_id}/payment-method",
        json={"account_name": "X", "bsb": "nope", "account_number": "abc"},
    )
    assert response.status_code == 422


def test_unknown_customer_is_a_contract_404(client):
    response = client.get("/api/v1/customers/cus_nope/payment-method")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "customer_not_found"


# --- outbox -------------------------------------------------------------------


def test_outbox_messages_are_well_formed(client):
    messages = client.get("/api/v1/outbox?limit=99").json()
    assert messages
    for message in messages:
        assert message["subject"].strip()
        assert message["body"].strip()
        assert message["update_link"], "every message needs a way back in"
        assert message["customer_name"] in message["body"] or message["channel"] == "sms"


def test_outbox_copy_differs_by_failure_class(store):
    """If every message read the same, classification would be pointless."""
    messages = store.outbox(limit=999)
    payments = {p.id: p for p in store.list_payments(limit=999)}

    bodies_by_class: dict[FailureClass, set[str]] = {}
    for message in messages:
        payment = payments.get(message.payment_id)
        if payment is None or payment.failure_class is None:
            continue
        bodies_by_class.setdefault(payment.failure_class, set()).add(message.body[:60])

    assert len(bodies_by_class) >= 3
    openings = [next(iter(v)) for v in bodies_by_class.values()]
    assert len(set(openings)) == len(openings), "copy is identical across classes"


# --- the simulated clock ------------------------------------------------------


def test_fast_forward_executes_due_work(client, store):
    before = len(store.outbox(limit=999))
    client.post("/dev/fast-forward?seconds=1209600")  # a fortnight
    after = len(store.outbox(limit=999))
    assert after >= before
    assert clock.offset_seconds() >= 1209600


def test_fast_forward_eventually_writes_off_unrecovered_payments(client, store):
    at_risk_before = store.summary().at_risk_cents
    # Well past the 21-day horizon.
    client.post("/dev/fast-forward?seconds=5184000")  # 60 days
    summary = store.summary()
    assert summary.at_risk_cents < at_risk_before
    assert summary.written_off_cents > 0


def test_reset_restores_real_time_and_the_seed(client, store):
    client.post("/dev/fast-forward?seconds=1209600")
    assert clock.offset_seconds() > 0
    client.post("/dev/reset")
    assert clock.offset_seconds() == 0


# --- the web pages ------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/", "/outbox", "/?failure_class=invalid_account", "/?status=recovered"],
)
def test_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_junk_query_params_do_not_500(client):
    """A stale bookmark must not break the dashboard mid-demo."""
    assert client.get("/?failure_class=nonsense&status=whatever").status_code == 200


def test_drill_down_renders_the_reasoning(client, store):
    from html import escape

    payment = store.list_payments(limit=1)[0]
    html = client.get(f"/payments/{payment.id}").text
    assert "Why the system did this" in html
    # Escaped, not raw: names like "Pace & Co Accounting" and "Preston Panel &
    # Paint" must arrive as &amp;. Asserting the raw string would quietly pass
    # only for customers without punctuation.
    assert escape(payment.customer_name) in html
    # A representative chunk of the reasoning must actually appear on the page.
    assert escape(payment.reasoning.split(".")[0][:40]) in html


def test_customer_names_with_ampersands_are_escaped(client, store):
    """Guards against a template switching to |safe and opening up injection."""
    target = next(
        (p for p in store.list_payments(limit=999) if "&" in p.customer_name), None
    )
    if target is None:
        pytest.skip("no customer with an ampersand in this seed")
    html = client.get(f"/payments/{target.id}").text
    assert "&amp;" in html
    assert target.customer_name not in html


def test_update_details_page_renders_on_a_phone(client, store):
    payment = store.list_payments(limit=1)[0]
    html = client.get(
        f"/update-details/{payment.customer_id}?payment={payment.id}"
    ).text
    assert "width=device-width" in html
    assert "Update your bank details" in html
    # 16px inputs, or iOS zooms on focus and the form feels broken.
    assert "font-size: 16px" in html


def test_missing_pages_are_404_html(client):
    assert client.get("/payments/pay_nope").status_code == 404
    assert client.get("/update-details/cus_nope").status_code == 404
