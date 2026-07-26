"""Person B's slice — engine, API and pages — against the real database.

Was `test_api_and_store.py`, written against the in-memory `Store` and its
hand-written fixtures. The store is gone; these run the same behavioural
assertions through `Repository` over Postgres, seeded by Person A's
`/sim/seed-demo` through the real ingest path. That change is the point: the
rules are now asserted against the data the demo actually shows.

The pure engine keeps its own tests — test_classifier, test_strategy_engine and
test_reasoning need no database and should stay that way.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core import clock
from app.models import Payment as PaymentRow
from app.models.enums import ActionType, AttemptStatus, FailureClass, PaymentStatus
from app.services.repository import Repository
from app.sim import seed

PAYMENTS = "/api/v1/payments"
SUMMARY = "/api/v1/dashboard/summary"
OUTBOX = "/api/v1/outbox"

# Retrying these can only fail and incur a dishonour fee. README non-negotiable
# 5 — the rule that separates this product from a cron job.
HARD_CLASSES = {
    FailureClass.INVALID_ACCOUNT,
    FailureClass.AUTHORITY_CANCELLED,
    FailureClass.PAYMENT_STOPPED,
}

# Six, not seven: expired_card cannot occur on a direct debit. See
# EXCLUDED_FROM_SEED in app/sim/seed.py.
EXPECTED_CLASSES = {
    FailureClass.INSUFFICIENT_FUNDS,
    FailureClass.INVALID_ACCOUNT,
    FailureClass.AUTHORITY_CANCELLED,
    FailureClass.PAYMENT_STOPPED,
    FailureClass.TECHNICAL,
    FailureClass.DO_NOT_HONOUR,
}


@pytest.fixture(autouse=True)
def _real_time():
    """Fast-forward moves a process-global offset; a leak reorders later tests."""
    clock.reset()
    yield
    clock.reset()


@pytest.fixture
def store(db_session) -> Repository:
    """A seeded, classified ledger.

    Seeded through Person A's `/sim/seed-demo` path rather than a fixture file,
    so these tests break if the two halves stop agreeing about shapes — which is
    exactly the failure the old in-memory store could not see.
    """
    seed.seed_demo(db_session)
    repo = Repository(db_session)
    repo.classify_unclassified()
    return repo


def _one_of_class(store: Repository, failure_class: FailureClass):
    for payment in store.list_payments(limit=500):
        if payment.failure_class is failure_class:
            return payment
    pytest.skip(f"seed produced no {failure_class.value} payment")


# --- the seeded, classified dataset -------------------------------------------


def test_seed_covers_every_direct_debit_class(store):
    """The demo has to show the full spread, or the strategy table looks arbitrary."""
    classes = {p.failure_class for p in store.list_payments(limit=500)}
    assert EXPECTED_CLASSES <= classes


def test_every_classified_payment_carries_reasoning(store):
    """README non-negotiable 4, asserted across the whole dataset.

    The judge reads this field, not the code. A classified payment with no
    reasoning is an unfinished feature, so it fails the build rather than
    rendering a blank cell.
    """
    payments = store.list_payments(limit=500)
    assert payments
    for payment in payments:
        assert payment.failure_class is not None, f"{payment.id} never classified"
        assert payment.reasoning, f"{payment.id} has no reasoning"
        assert len(payment.reasoning) > 20, f"{payment.id} reasoning is a stub"


def test_ingest_leaves_classification_to_the_engine(db_session):
    """The seam itself: seeding classifies nothing until the engine runs.

    If ingest ever starts guessing a failure_class this fails — and a guess
    written at ingest is indistinguishable from a real classification.
    """
    seed.seed_demo(db_session)
    rows = db_session.execute(select(PaymentRow)).scalars().all()
    assert rows
    assert all(r.failure_class is None for r in rows)
    assert all(r.reasoning is None for r in rows)

    assert Repository(db_session).classify_unclassified() == len(rows)
    db_session.expire_all()
    rows = db_session.execute(select(PaymentRow)).scalars().all()
    assert all(r.failure_class is not None for r in rows)


def test_money_is_always_integer_cents(store):
    for payment in store.list_payments(limit=500):
        assert isinstance(payment.amount_cents, int)
        assert not isinstance(payment.amount_cents, bool)


def test_hard_failures_are_never_scheduled_a_retry(store):
    """README non-negotiable 5. Zero retries, always — before any details fix."""
    for payment in store.list_payments(limit=500):
        if payment.failure_class not in HARD_CLASSES:
            continue
        retries = [a for a in payment.attempts if a.action is ActionType.RETRY]
        assert not retries, (
            f"{payment.id} ({payment.failure_class.value}) was scheduled "
            f"{len(retries)} retry/retries"
        )


def test_classification_is_idempotent_across_a_second_sweep(store):
    """A second sweep must find nothing left to do and change no reasoning."""
    before = {p.id: p.reasoning for p in store.list_payments(limit=500)}
    assert store.classify_unclassified() == 0
    after = {p.id: p.reasoning for p in store.list_payments(limit=500)}
    assert before == after


# --- dashboard summary --------------------------------------------------------


def test_summary_is_internally_consistent(store):
    summary = store.summary()
    payments = store.list_payments(limit=500)

    def total(status):
        return sum(p.amount_cents for p in payments if p.status is status)

    assert summary.at_risk_cents == total(PaymentStatus.FAILED)
    assert summary.recovered_cents == total(PaymentStatus.RECOVERED)
    assert summary.written_off_cents == total(PaymentStatus.WRITTEN_OFF)
    # Escalated is a subset of at-risk, not a sibling — a payment waiting on a
    # human is still at risk. Presenting them as disjoint would imply the four
    # figures sum to the book, and they never will.
    assert summary.escalated_cents <= summary.at_risk_cents
    assert 0.0 <= summary.recovery_rate <= 1.0


def test_summary_by_class_totals_match_the_book(store):
    summary = store.summary()
    payments = store.list_payments(limit=500)
    assert sum(c.count for c in summary.by_class) == len(payments)
    assert sum(c.amount_cents for c in summary.by_class) == sum(
        p.amount_cents for p in payments
    )


def test_dashboard_summary_endpoint_shape(client, store):
    body = client.get(SUMMARY).json()
    for key in (
        "at_risk_cents",
        "recovered_cents",
        "escalated_cents",
        "written_off_cents",
        "recovery_rate",
        "by_class",
    ):
        assert key in body
    assert all(isinstance(body[k], int) for k in ("at_risk_cents", "recovered_cents"))
    for entry in body["by_class"]:
        assert set(entry) == {
            "failure_class",
            "count",
            "amount_cents",
            "recovered_cents",
        }


# --- the route that both halves used to claim ---------------------------------


def test_payments_endpoint_serves_the_contract_paged_shape(client, store):
    """Regression: this half also implemented GET /payments, returning a bare list.

    Two routers on one path is not a conflict git can see — FastAPI resolves it
    silently by registration order. The contract specifies the paged shape with
    a cursor, so Person A's database-backed version has to be the one answering.
    """
    body = client.get(PAYMENTS).json()
    assert isinstance(body, dict), "a bare list means the wrong router answered"
    assert set(body) == {"data", "next_cursor"}
    assert isinstance(body["data"], list)


def test_payments_endpoint_carries_the_engine_fields(client, store):
    """The two halves meet here: A serves the row, B filled in the meaning."""
    rows = client.get(PAYMENTS, params={"limit": 200}).json()["data"]
    assert rows
    classified = [r for r in rows if r["failure_class"] is not None]
    assert classified, "engine output never reached the read seam"
    for row in classified:
        assert row["reasoning"]
        assert "amount_cents" in row and isinstance(row["amount_cents"], int)


def test_errors_use_the_contract_shape(client):
    body = client.get(f"{PAYMENTS}/pay_nope").json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}


# --- run-recovery -------------------------------------------------------------


def test_run_recovery_is_idempotent(client, store):
    payment = store.list_payments(limit=1)[0]
    first = client.post(f"{PAYMENTS}/{payment.id}/run-recovery").json()
    second = client.post(f"{PAYMENTS}/{payment.id}/run-recovery").json()
    assert first["reasoning"] == second["reasoning"]
    assert first["failure_class"] == second["failure_class"]
    assert len(first["attempts"]) == len(second["attempts"])


def test_run_recovery_does_not_erase_what_already_happened(client, store, db_session):
    """Regression: re-planning used to overwrite the attempt list wholesale.

    You cannot unsend an email. An attempt that has executed is a fact, and a
    second call to a documented-idempotent endpoint must not delete the record
    of it.
    """
    payment = _one_of_class(store, FailureClass.INSUFFICIENT_FUNDS)
    clock.fast_forward(60 * 60 * 24 * 7)
    Repository(db_session).execute_due()

    before = store.get_payment(payment.id)
    done_before = [a for a in before.attempts if a.executed_at is not None]
    if not done_before:
        pytest.skip("nothing became due for this payment")

    client.post(f"{PAYMENTS}/{payment.id}/run-recovery")

    after = store.get_payment(payment.id)
    done_after = {a.id for a in after.attempts if a.executed_at is not None}
    for attempt in done_before:
        assert attempt.id in done_after, "an executed attempt was erased by re-planning"


def test_run_recovery_on_unknown_payment_is_a_contract_404(client):
    response = client.post(f"{PAYMENTS}/pay_nope/run-recovery")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "payment_not_found"


# --- the update-details flow --------------------------------------------------


def test_payment_method_never_exposes_the_full_number(client, store):
    payment = store.list_payments(limit=1)[0]
    body = client.get(f"/api/v1/customers/{payment.customer_id}/payment-method").json()
    assert body["account_number_masked"].startswith("••••")
    assert len(body["account_number_masked"].replace("•", "").strip()) <= 4


def test_updating_details_recovers_an_invalid_account_payment(client, store):
    """The live moment in the demo.

    The customer fixes the cause, so a debit that was futile a second ago now
    succeeds — which is the whole argument for classifying failures at all.
    """
    payment = _one_of_class(store, FailureClass.INVALID_ACCOUNT)
    response = client.post(
        f"/api/v1/customers/{payment.customer_id}/payment-method",
        params={"payment_id": payment.id},
        json={
            "account_name": "Marina Auto Detailing",
            "bsb": "063-000",
            "account_number": "12345678",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["recovered_count"] >= 1
    assert payment.id in body["recovered_payment_ids"]

    recovered = store.get_payment(payment.id)
    assert recovered.status is PaymentStatus.RECOVERED
    assert recovered.recovered_at is not None


def test_recovery_cancels_pending_work(client, store):
    payment = _one_of_class(store, FailureClass.INVALID_ACCOUNT)
    client.post(
        f"/api/v1/customers/{payment.customer_id}/payment-method",
        params={"payment_id": payment.id},
        json={
            "account_name": "Test Business",
            "bsb": "063-000",
            "account_number": "12345678",
        },
    )
    after = store.get_payment(payment.id)
    assert not [a for a in after.attempts if a.status is AttemptStatus.SCHEDULED]


@pytest.mark.parametrize(
    "failure_class",
    [FailureClass.PAYMENT_STOPPED, FailureClass.AUTHORITY_CANCELLED],
)
def test_new_details_do_not_auto_charge_a_revoked_mandate(client, store, failure_class):
    """A stop order is a dispute; a cancelled authority is a revoked mandate.

    New bank details are not permission to debit. Auto-charging here is exactly
    the hostile behaviour the strategy table exists to prevent.
    """
    payment = _one_of_class(store, failure_class)
    body = client.post(
        f"/api/v1/customers/{payment.customer_id}/payment-method",
        params={"payment_id": payment.id},
        json={
            "account_name": "Test Business",
            "bsb": "063-000",
            "account_number": "12345678",
        },
    ).json()

    assert payment.id not in body["recovered_payment_ids"]
    assert store.get_payment(payment.id).status is not PaymentStatus.RECOVERED


def test_bad_bank_details_are_rejected(client, store):
    payment = store.list_payments(limit=1)[0]
    response = client.post(
        f"/api/v1/customers/{payment.customer_id}/payment-method",
        json={"account_name": "X", "bsb": "nope", "account_number": "abc"},
    )
    assert response.status_code == 422


def test_pinch_documented_test_account_number_is_accepted(client, store):
    """1234567890 is the account number Pinch's own test-mode docs tell you to
    use. A 6-9 digit cap rejected it with a 422, so the update-details flow
    would have failed on the exact input anyone follows the docs to supply."""
    payment = store.list_payments(limit=1)[0]
    response = client.post(
        f"/api/v1/customers/{payment.customer_id}/payment-method",
        json={
            "account_name": "Pinch Test Account",
            "bsb": "000-001",
            "account_number": "1234567890",
        },
    )
    assert response.status_code == 200


def test_unknown_customer_is_a_contract_404(client):
    response = client.get("/api/v1/customers/cus_nope/payment-method")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "customer_not_found"


# --- outbox -------------------------------------------------------------------


def test_outbox_messages_are_well_formed(client, store, db_session):
    clock.fast_forward(60 * 60 * 24 * 5)
    Repository(db_session).execute_due()

    messages = client.get(OUTBOX).json()
    if not messages:
        pytest.skip("no message attempt became due in the window")
    for message in messages:
        assert message["subject"]
        assert message["body"]
        assert message["channel"] in {"email", "sms", "in_app", "phone"}
        assert message["update_link"]


def test_outbox_survives_the_session_that_wrote_it(client, store, db_session):
    """Why it is a table and not a list: the demo reloads the page."""
    clock.fast_forward(60 * 60 * 24 * 5)
    written = Repository(db_session).execute_due()
    if not written:
        pytest.skip("nothing became due")

    # A different session entirely — as a browser reload would be.
    assert client.get(OUTBOX).json() == client.get(OUTBOX).json()
    assert client.get(OUTBOX).json(), "messages did not survive the writing session"


# --- the simulated clock ------------------------------------------------------


def test_fast_forward_executes_due_work(client, store, db_session):
    """A three-day settlement window has to collapse into a button press."""

    def scheduled() -> int:
        return sum(
            1
            for p in store.list_payments(limit=500)
            for a in p.attempts
            if a.status is AttemptStatus.SCHEDULED
        )

    before = scheduled()
    clock.fast_forward(60 * 60 * 24 * 6)
    executed = Repository(db_session).execute_due()

    assert executed > 0, "six simulated days made nothing due"
    assert scheduled() < before


def test_execute_due_is_idempotent(client, store, db_session):
    clock.fast_forward(60 * 60 * 24 * 6)
    repo = Repository(db_session)
    assert repo.execute_due() > 0
    # Second call with no further time passing must be a no-op: an attempt
    # leaves `scheduled` exactly once, or the outbox fills with duplicates.
    assert repo.execute_due() == 0


def test_fast_forward_eventually_writes_off_unrecovered_payments(
    client, store, db_session
):
    clock.fast_forward(60 * 60 * 24 * 45)
    Repository(db_session).execute_due()
    statuses = {p.status for p in store.list_payments(limit=500)}
    assert PaymentStatus.WRITTEN_OFF in statuses


# --- the web pages ------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/outbox"])
def test_pages_render(client, store, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_junk_query_params_do_not_500(client, store):
    """A stale bookmark must not break the dashboard mid-demo."""
    assert client.get("/?failure_class=nonsense&status=rubbish").status_code == 200


def test_dashboard_classifies_what_ingest_left_alone(client, db_session):
    """Open the dashboard on a raw seeded database and it shows meaning, not blanks."""
    seed.seed_demo(db_session)
    html = client.get("/").text
    assert "Unclassified" not in html


def test_drill_down_renders_the_reasoning(client, store):
    payment = store.list_payments(limit=1)[0]
    html = client.get(f"/payments/{payment.id}").text
    assert payment.reasoning[:40] in html


def test_customer_names_with_ampersands_are_escaped(client, store, db_session):
    """Guards against a template switching to |safe and opening up injection."""
    payment = store.list_payments(limit=1)[0]
    row = db_session.get(PaymentRow, payment.id)
    row.customer.name = "Smith & Sons <script>alert(1)</script>"
    db_session.commit()

    html = client.get(f"/payments/{payment.id}").text
    assert "<script>alert(1)</script>" not in html
    assert "&amp;" in html or "&lt;script&gt;" in html


def test_update_details_page_renders_on_a_phone(client, store):
    payment = store.list_payments(limit=1)[0]
    html = client.get(f"/update-details/{payment.customer_id}").text
    assert "viewport" in html
    assert "<form" in html


def test_missing_pages_are_404_html(client, store):
    response = client.get("/payments/pay_nope")
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]


def test_ui_clock_controls_drive_the_real_simulator(client, store):
    """The header buttons used to hit /dev/*, which no longer exists."""
    assert client.post("/ui/fast-forward?seconds=259200").status_code == 200
    assert clock.offset_seconds() >= 259200
