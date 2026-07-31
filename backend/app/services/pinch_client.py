"""The Pinch boundary.

Everything that would talk to Pinch goes through PinchClient. Callers get the
same interface and the same result objects in both modes, so no caller ever
branches on PINCH_MODE — that is the whole point of this module. If a caller
ever needs to know which implementation it holds, this abstraction has failed.

Mock is the default and is first-class: the entire system runs end to end
without Pinch credentials.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

import httpx
from sqlalchemy.orm import Session

from app.core import clock
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.ids import new_id
from app.models.customer import Customer
from app.models.payment import Payment

logger = logging.getLogger(__name__)

PINCH_DOCS = "https://docs.getpinch.com.au"
PINCH_TEST_BASE = "https://api.getpinch.com.au/test/"

# Documented test bank account — see the Test and Live Mode guide. Any BSB and
# account number are accepted in test mode; using the ones the docs name keeps
# a failure attributable to credentials or environment rather than to data.
TEST_BSB = "000-000"
TEST_ACCOUNT_NUMBER = "0000000000"

# Refresh a token this long before it actually expires, so one never dies
# between the check and the request.
_TOKEN_EXPIRY_SKEW_SECONDS = 60


# --------------------------------------------------------------------------
# Result objects
#
# Deliberately plain and mode-agnostic. A direct debit retry is not resolved
# when the API returns — the bank's answer arrives days later as a webhook.
# So `accepted` means "Pinch took the instruction", never "the money moved".
# Anything that treats acceptance as success is the bug this shape prevents.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryResult:
    payment_id: str
    accepted: bool
    # Settlement outcome always arrives by webhook, so this is "pending" on
    # the happy path. Never "succeeded" — that word belongs to the webhook.
    status: str = "pending"
    # Populated only when Pinch rejects the instruction outright.
    error_code: str | None = None
    message: str = ""


@dataclass(frozen=True)
class PaymentMethodResult:
    customer_id: str
    updated: bool
    # Last four digits only — see the note in update_payment_method.
    account_last4: str | None = None
    error_code: str | None = None
    message: str = ""


@dataclass(frozen=True)
class TestPaymentResult:
    """A payment created in Pinch's test environment to force a dishonour.

    `forced_code` is the code the sandbox was asked to fail with. Like a
    retry, the failure itself does not come back from this call — it arrives on
    the next `bank-results` event, which is the whole point: the loop under
    demonstration is the real one, not a shortcut that writes the dishonour
    straight into our own database.
    """

    accepted: bool
    forced_code: str | None = None
    pinch_payment_id: str | None = None
    pinch_payer_id: str | None = None
    customer_id: str | None = None
    error_code: str | None = None
    message: str = ""


@dataclass(frozen=True)
class EventSummary:
    """One row of `GET /events` — enough to decide whether to fetch the rest."""

    id: str
    type: str
    event_date: str | None = None


@dataclass(frozen=True)
class OutboundCall:
    """A recorded mock call. Lets tests and the simulator assert what was sent."""

    method: str
    args: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------


class PinchClient(ABC):
    """What Person B codes against. Neither method may reveal the mode."""

    @abstractmethod
    def retry_payment(self, payment_id: str) -> RetryResult:
        """Re-present a failed debit.

        Returns once the instruction is accepted. The eventual success or
        dishonour arrives separately at POST /webhooks/pinch.
        """

    @abstractmethod
    def update_payment_method(
        self, customer_id: str, details: dict[str, Any]
    ) -> PaymentMethodResult:
        """Replace a customer's bank details.

        `details` carries `account_name`, `bsb`, and `account_number`.
        """

    @abstractmethod
    def create_test_payment(
        self,
        *,
        amount_cents: int,
        raw_code: str | None,
        customer_id: str | None = None,
        customer_name: str | None = None,
        description: str | None = None,
    ) -> TestPaymentResult:
        """Present a debit that will fail with `raw_code`.

        The demo's entry point. In mock mode the simulator produces the
        dishonour; in live mode Pinch's test environment does, triggered by the
        code embedded in the description. Either way the failure comes back
        through ingest as an event, never as a return value.
        """

    @abstractmethod
    def list_events(
        self, event_type: str | None = None, page: int = 1, page_size: int = 50
    ) -> list[EventSummary]:
        """Recent events, newest first. The poll half of ingest."""

    @abstractmethod
    def get_event(self, event_id: str) -> dict[str, Any] | None:
        """One event's full envelope, in the shape `/webhooks/pinch` ingests.

        `GET /events` returns summaries only — `metadata`, not `data` — so the
        payments in a batch are not visible until the event is fetched by id.
        """


# --------------------------------------------------------------------------
# Mock
# --------------------------------------------------------------------------


# How often a re-presented debit clears, by the class of the original failure.
# Two figures each: when the account is likely to hold funds, and when it is
# not. The gap between the columns is what makes payday alignment worth doing —
# an engine that retries on the customer's payday earns the left-hand number,
# one that retries blindly earns the right.
#
# The hard classes are absent deliberately. A closed account, a revoked
# mandate, and a stopped payment never clear on re-presentation no matter when
# you try, which is why the strategy table schedules zero retries for them —
# see README "Non-negotiables" rule 5. Anything not listed here clears never.
_CLEARS_IN_FUNDS = {
    "insufficient_funds": 0.85,
    "temporary_problem": 0.75,
    "technical": 0.80,
    # A bank refusal without a stated reason. Sometimes a soft decline that
    # clears later, more often something the customer has to resolve.
    "do_not_honour": 0.30,
}
_CLEARS_OUT_OF_FUNDS = {
    "insufficient_funds": 0.20,
    "temporary_problem": 0.55,
    # A transient fault on Pinch's or the bank's side has nothing to do with
    # the customer's balance, so timing barely moves it.
    "technical": 0.75,
    "do_not_honour": 0.15,
}

# Funds are assumed present on the customer's payday and the two days after it.
_FUNDS_WINDOW_DAYS = 3


def _unit_interval(*parts: object) -> float:
    """A stable pseudo-random float in [0, 1) from the given parts.

    sha256 rather than hash(): PYTHONHASHSEED randomises str hashing per
    process, which would make two runs of the same seeded demo diverge. The
    README requires /sim/reset to reproduce the same dashboard, so the outcome
    of a given presentation has to be a pure function of its inputs.
    """
    import hashlib

    key = "|".join(str(p) for p in parts).encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big") / 2**64


def _executed_retry_count(session: Session, payment_id: str) -> int:
    """Retries already presented for this payment."""
    from sqlalchemy import func, select as sa_select

    from app.models.attempt import Attempt

    return int(
        session.execute(
            sa_select(func.count())
            .select_from(Attempt)
            .where(
                Attempt.payment_id == payment_id,
                Attempt.action == "retry",
                Attempt.executed_at.is_not(None),
            )
        ).scalar_one()
    )


def _payday_weekday(customer: Customer | None) -> int:
    """The weekday this customer is paid, Mon=0.

    Falls back to a stable per-customer weekday rather than a fixed default:
    every customer sharing one payday would make the engine's alignment look
    better than it is, because a single well-timed retry would clear the whole
    book at once.
    """
    if customer is None:
        return 3
    if customer.observed_payday_weekday is not None:
        return int(customer.observed_payday_weekday)
    return int(_unit_interval("payday", customer.id) * 7)


def _has_funds(customer: Customer | None, at: datetime) -> bool:
    """Whether the account is likely to hold funds on `at`."""
    days_since_payday = (at.weekday() - _payday_weekday(customer)) % 7
    return days_since_payday < _FUNDS_WINDOW_DAYS


def _presentation_clears(
    *,
    payment_id: str,
    failure_class: str | None,
    attempt_number: int,
    customer: Customer | None,
    at: datetime,
) -> bool:
    """Decide whether a re-presented debit clears. Deterministic.

    `failure_class` is read rather than `raw_code` because the mapping from
    Pinch's code to our class already exists and this only needs the coarse
    behaviour. An unclassified payment clears never — the engine has not
    decided anything about it yet, so a retry here would be a coin toss
    dressed up as a decision.
    """
    if failure_class is None:
        return False

    table = _CLEARS_IN_FUNDS if _has_funds(customer, at) else _CLEARS_OUT_OF_FUNDS
    probability = table.get(failure_class, 0.0)
    if probability <= 0.0:
        return False

    # Later presentations against the same unchanged cause are less likely to
    # clear, so a four-retry ladder does not read as four independent coin
    # flips at the same odds.
    probability *= 0.8 ** max(attempt_number, 0)

    return _unit_interval("clears", payment_id, attempt_number) < probability


class MockPinchClient(PinchClient):
    """Resolves against local database state. Never opens a socket.

    This is the default, and the demo runs on it.
    """

    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self._session_factory = session_factory
        # Every call, in order. The simulator reads this to decide what to
        # settle, and tests assert against it without a database round trip.
        self.calls: list[OutboundCall] = []

    def retry_payment(self, payment_id: str) -> RetryResult:
        self.calls.append(OutboundCall("retry_payment", {"payment_id": payment_id}))

        with self._session_factory() as session:
            payment = session.get(Payment, payment_id)

            # A real API rejects an unknown id rather than silently succeeding,
            # so the mock does too — otherwise a typo'd id passes in mock and
            # fails only in the live demo.
            if payment is None:
                return RetryResult(
                    payment_id=payment_id,
                    accepted=False,
                    status="rejected",
                    error_code="payment_not_found",
                    message=f"No payment {payment_id}.",
                )

            if payment.status == "recovered":
                return RetryResult(
                    payment_id=payment_id,
                    accepted=False,
                    status="rejected",
                    error_code="already_recovered",
                    message="Payment already recovered; retry refused.",
                )

        # Stand in for the bank: decide whether this presentation clears, then
        # queue the settlement webhook that reports it. The caller never learns
        # the answer here — it arrives at /webhooks/pinch after the
        # (fast-forwardable) settlement window, exactly as in production.
        from app.sim.service import schedule_settlement

        with self._session_factory() as session:
            payment = session.get(Payment, payment_id)
            customer = session.get(Customer, payment.customer_id)
            succeeded = _presentation_clears(
                payment_id=payment_id,
                failure_class=payment.failure_class,
                attempt_number=_executed_retry_count(session, payment_id),
                customer=customer,
                at=clock.now(),
            )
            schedule_settlement(
                session,
                payment_id=payment_id,
                customer_id=payment.customer_id,
                amount_cents=payment.amount_cents,
                succeeded=succeeded,
                raw_code=payment.raw_code,
            )
            session.commit()

        return RetryResult(
            payment_id=payment_id,
            accepted=True,
            status="pending",
            message="Retry accepted; awaiting settlement webhook.",
        )

    def update_payment_method(
        self, customer_id: str, details: dict[str, Any]
    ) -> PaymentMethodResult:
        # Log the call without the account number in it.
        self.calls.append(
            OutboundCall(
                "update_payment_method",
                {"customer_id": customer_id, "bsb": details.get("bsb")},
            )
        )

        account_number = str(details.get("account_number") or "")
        last4 = account_number[-4:] if len(account_number) >= 4 else None

        with self._session_factory() as session:
            customer = session.get(Customer, customer_id)
            if customer is None:
                return PaymentMethodResult(
                    customer_id=customer_id,
                    updated=False,
                    error_code="customer_not_found",
                    message=f"No customer {customer_id}.",
                )

            customer.bank_account_name = details.get("account_name")
            customer.bank_bsb = details.get("bsb")
            # Only the last four are persisted. The full number is used to
            # talk to Pinch and is never written to our database.
            customer.bank_account_last4 = last4
            customer.payment_method_updated_at = clock.now()
            session.commit()

        return PaymentMethodResult(
            customer_id=customer_id,
            updated=True,
            account_last4=last4,
            message="Payment method updated.",
        )

    def create_test_payment(
        self,
        *,
        amount_cents: int,
        raw_code: str | None,
        customer_id: str | None = None,
        customer_name: str | None = None,
        description: str | None = None,
    ) -> TestPaymentResult:
        """Queue a simulated dishonour, settling after the usual delay.

        Deliberately not a shortcut: this writes a pending `bank-results`
        webhook rather than a failed payment row, so the payment appears only
        once ingest has processed the event — the same order of operations the
        live path has.
        """
        self.calls.append(
            OutboundCall(
                "create_test_payment",
                {"amount_cents": amount_cents, "raw_code": raw_code},
            )
        )

        from app.sim import service
        from app.sim.schemas import ScenarioRequest

        with self._session_factory() as session:
            created = service.create_scenario(
                session,
                ScenarioRequest(
                    customer_id=customer_id,
                    customer_name=customer_name,
                    amount_cents=amount_cents,
                    outcome="dishonour" if raw_code else "success",
                    raw_code=raw_code,
                    delay_seconds=int(service.SETTLEMENT_SECONDS),
                ),
            )

        return TestPaymentResult(
            accepted=True,
            forced_code=raw_code,
            pinch_payment_id=created["payment_id"],
            customer_id=created["customer_id"],
            message=(
                "Simulated debit queued; the dishonour arrives on the next "
                "bank-results event once the settlement window has passed."
            ),
        )

    def list_events(
        self, event_type: str | None = None, page: int = 1, page_size: int = 50
    ) -> list[EventSummary]:
        """The simulator's queue, as event summaries.

        Only events whose delivery time has arrived are listed, so a poll
        behaves like Pinch's — a settlement three days out is not visible until
        the clock (or a fast-forward) reaches it.
        """
        from sqlalchemy import select as sa_select

        from app.models.sim_webhook import SimulatedWebhook

        with self._session_factory() as session:
            rows = (
                session.execute(
                    sa_select(SimulatedWebhook)
                    .where(SimulatedWebhook.deliver_at <= clock.now())
                    .order_by(SimulatedWebhook.deliver_at.desc())
                    .limit(page_size * page)
                )
                .scalars()
                .all()
            )

        seen: set[str] = set()
        summaries: list[EventSummary] = []
        for row in rows:
            # Duplicate deliveries share one event_id; the list endpoint
            # reports events, not deliveries.
            if row.event_id in seen:
                continue
            seen.add(row.event_id)
            payload = row.payload or {}
            row_type = payload.get("Type") or payload.get("type") or ""
            if event_type is not None and row_type != event_type:
                continue
            summaries.append(
                EventSummary(
                    id=row.event_id,
                    type=row_type,
                    event_date=payload.get("EventDate") or payload.get("eventDate"),
                )
            )
        return summaries[(page - 1) * page_size : page * page_size]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        from sqlalchemy import select as sa_select

        from app.models.sim_webhook import SimulatedWebhook

        with self._session_factory() as session:
            row = (
                session.execute(
                    sa_select(SimulatedWebhook)
                    .where(SimulatedWebhook.event_id == event_id)
                    .order_by(SimulatedWebhook.delivery_number)
                    .limit(1)
                )
                .scalars()
                .first()
            )
        return row.payload if row is not None else None


# --------------------------------------------------------------------------
# Live
# --------------------------------------------------------------------------


class LivePinchClient(PinchClient):
    """Hits the real Pinch API.

    Auth, transport, versioning, and token caching are wired and real. The
    request *bodies* are not: filling them in is the deliberate, one-time
    mock->live switch, and guessing them now would produce code that looks
    finished and isn't.

    Auth is OAuth2 client credentials, not a static API key: Application ID
    and Secret Key are Basic-auth'd against the token endpoint, and the
    resulting bearer token is cached. See
    https://docs.getpinch.com.au/docs/application-authentication.
    """

    def __init__(
        self,
        api_base: str | None = None,
        auth_base: str | None = None,
        application_id: str | None = None,
        secret_key: str | None = None,
        pinch_version: str | None = None,
        timeout: float = 10.0,
        session_factory: Callable[[], Session] = SessionLocal,
    ) -> None:
        # `is not None`, not `or`: an explicitly-passed empty string means
        # "no credential", and must not silently fall back to the configured
        # one. With `or`, a test or caller asking for an unconfigured client
        # would quietly get the real credentials instead.
        self.api_base = api_base if api_base is not None else settings.PINCH_API_BASE
        self.auth_base = (
            auth_base if auth_base is not None else settings.PINCH_AUTH_BASE
        )
        self.application_id = (
            application_id
            if application_id is not None
            else settings.PINCH_APPLICATION_ID
        )
        self.secret_key = (
            secret_key if secret_key is not None else settings.PINCH_SECRET_KEY
        )
        self.pinch_version = (
            pinch_version if pinch_version is not None else settings.PINCH_VERSION
        )
        self._timeout = timeout
        # Only for looking up/caching pinch_payer_id, pinch_source_id, and
        # pinch_payment_id locally. Every actual payment decision — did the
        # retry clear, did the source verify — comes from Pinch over the
        # socket below, never from this session.
        self._session_factory = session_factory
        self._client: httpx.Client | None = None
        self._token: str | None = None
        self._token_expires_at: datetime | None = None

    # -- auth --------------------------------------------------------------

    def _get_token(self) -> str:
        """Return a cached bearer token, fetching one only when needed.

        The docs are explicit that tokens last an hour and must be cached
        rather than re-fetched per call.
        """
        if (
            self._token is not None
            and self._token_expires_at is not None
            and clock.now() < self._token_expires_at
        ):
            return self._token

        if not self.application_id or not self.secret_key:
            raise RuntimeError(
                "PINCH_APPLICATION_ID and PINCH_SECRET_KEY are required when "
                "PINCH_MODE=live. Set them in .env.local, or switch back to "
                "PINCH_MODE=mock."
            )

        response = httpx.post(
            f"{self.auth_base}/connect/token",
            auth=httpx.BasicAuth(self.application_id, self.secret_key),
            # `scope` is required — Pinch's token endpoint 400s without it.
            # Also form-data, not JSON: the docs are explicit that a JSON body
            # here is silently rejected.
            data={"grant_type": "client_credentials", "scope": "api1"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        payload = response.json()

        self._token = payload["access_token"]
        # Expire early so a token never dies mid-request. Measured on the
        # simulated clock, so a fast-forward correctly forces a refresh.
        lifetime = int(payload.get("expires_in", 3600)) - _TOKEN_EXPIRY_SKEW_SECONDS
        self._token_expires_at = clock.now() + timedelta(seconds=max(lifetime, 0))
        return self._token

    # -- transport ---------------------------------------------------------

    def _http(self) -> httpx.Client:
        """Lazily built, so constructing the client opens no connection."""
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.api_base,
                timeout=self._timeout,
                headers={
                    "pinch-version": self.pinch_version,
                    "Content-Type": "application/json",
                },
            )
        return self._client

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Authenticated request with the version header already applied."""
        headers: dict[str, str] = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self._get_token()}"

        # Pinch's test environment honours a Time-Travel header, which is its
        # own version of our fast-forward. Passing our offset through keeps a
        # live-test run on the same simulated clock as a mock run. Live money
        # has no such header, so this is scoped to /test/.
        offset = clock.offset_seconds()
        if offset and not settings.targets_live_money:
            travelled = clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
            headers["Time-Travel"] = travelled

        response = self._http().request(method, path, headers=headers, **kwargs)
        response.raise_for_status()
        return response

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        self._token = None
        self._token_expires_at = None

    def retry_payment(self, payment_id: str) -> RetryResult:
        """Re-present a failed debit as a new Pinch payment.

        Verified against https://docs.getpinch.com.au/reference/save-payment
        on 2026-07-26. Pinch has no "retry" endpoint — a retry is a fresh
        `POST /payments` against the same payer (and source, if we have one),
        scheduled for the next business day. A 200/201 means Pinch accepted
        the instruction, NOT that the money moved: the dishonour or success
        for *this* presentation arrives later on a `bank-results` webhook,
        matched back to this local row via `pinch_payment_id`.
        """
        with self._session_factory() as session:
            payment = session.get(Payment, payment_id)

            if payment is None:
                return RetryResult(
                    payment_id=payment_id,
                    accepted=False,
                    status="rejected",
                    error_code="payment_not_found",
                    message=f"No payment {payment_id}.",
                )

            if payment.status == "recovered":
                return RetryResult(
                    payment_id=payment_id,
                    accepted=False,
                    status="rejected",
                    error_code="already_recovered",
                    message="Payment already recovered; retry refused.",
                )

            customer = session.get(Customer, payment.customer_id)
            if customer is None or not customer.pinch_payer_id:
                # No Payer to charge — update_payment_method creates one.
                # Guessing an id here would silently retry against nothing.
                return RetryResult(
                    payment_id=payment_id,
                    accepted=False,
                    status="rejected",
                    error_code="no_pinch_payer",
                    message=(
                        "Customer has no Pinch payer id yet; "
                        "update_payment_method must run before the first retry."
                    ),
                )

            # Distinguishes each successive retry so a network-level retry of
            # *this* HTTP call reuses the same nonce (Pinch dedupes on it),
            # while the next scheduled retry gets a new one and is not
            # mistaken for a replay.
            attempt_number = _executed_retry_count(session, payment_id)
            body: dict[str, Any] = {
                "payerId": customer.pinch_payer_id,
                "amount": payment.amount_cents,
                # Direct debit re-presentations settle overnight regardless
                # of when submitted, so next-business-day is the earliest
                # meaningful date; Pinch's own processing schedule handles
                # weekends.
                "transactionDate": (clock.now().date() + timedelta(days=1)).isoformat(),
                "description": f"Retry of {payment.id}",
                "nonce": [f"{payment.id}:retry:{attempt_number}"],
            }
            if customer.pinch_source_id:
                body["sourceId"] = customer.pinch_source_id

            try:
                response = self._request("POST", "/payments", json=body)
            except httpx.HTTPStatusError as exc:
                return RetryResult(
                    payment_id=payment_id,
                    accepted=False,
                    status="rejected",
                    error_code=f"http_{exc.response.status_code}",
                    message=exc.response.text,
                )

            pinch_payment = response.json()
            # Overwritten, not appended: this local row tracks the most
            # recent presentation, so the next bank-results webhook resolves
            # back to it (see Payment.pinch_payment_id).
            payment.pinch_payment_id = pinch_payment.get("id")
            session.commit()

        return RetryResult(
            payment_id=payment_id,
            accepted=True,
            status="pending",
            message="Retry accepted; awaiting settlement webhook.",
        )

    def update_payment_method(
        self, customer_id: str, details: dict[str, Any]
    ) -> PaymentMethodResult:
        """Replace a customer's bank details in Pinch.

        Verified against https://docs.getpinch.com.au/reference/save-payer
        and https://docs.getpinch.com.au/reference/create-payment-source on
        2026-07-26. A bank-account Source takes plain BSB/account fields
        directly — no CaptureJS token is required for this source type (only
        credit-card sources need one), so `details` (account_name, bsb,
        account_number) maps straight onto the request.

        Creates the Payer on first use for this customer, then attaches (or
        replaces) a bank-account Source. Only the last four digits of the
        account number are ever persisted locally, same as the mock.
        """
        account_number = str(details.get("account_number") or "")
        last4 = account_number[-4:] if len(account_number) >= 4 else None

        with self._session_factory() as session:
            customer = session.get(Customer, customer_id)
            if customer is None:
                return PaymentMethodResult(
                    customer_id=customer_id,
                    updated=False,
                    error_code="customer_not_found",
                    message=f"No customer {customer_id}.",
                )

            try:
                if not customer.pinch_payer_id:
                    payer_response = self._request(
                        "POST",
                        "/payers",
                        json={
                            "firstName": customer.name or "Customer",
                            "emailAddress": (
                                customer.email or f"{customer.id}@example.invalid"
                            ),
                        },
                    ).json()
                    customer.pinch_payer_id = payer_response["id"]

                source_response = self._request(
                    "POST",
                    f"/payers/{customer.pinch_payer_id}/sources",
                    json={
                        "sourceType": "bank-account",
                        "bankAccountName": details.get("account_name"),
                        "bankAccountBsb": details.get("bsb"),
                        "bankAccountNumber": account_number,
                    },
                ).json()
            except httpx.HTTPStatusError as exc:
                return PaymentMethodResult(
                    customer_id=customer_id,
                    updated=False,
                    error_code=f"http_{exc.response.status_code}",
                    message=exc.response.text,
                )

            customer.pinch_source_id = source_response["id"]
            customer.bank_account_name = details.get("account_name")
            customer.bank_bsb = details.get("bsb")
            customer.bank_account_last4 = last4
            customer.payment_method_updated_at = clock.now()
            session.commit()

        return PaymentMethodResult(
            customer_id=customer_id,
            updated=True,
            account_last4=last4,
            message="Payment method updated.",
        )

    # -- test payments -----------------------------------------------------

    def create_test_payment(
        self,
        *,
        amount_cents: int,
        raw_code: str | None,
        customer_id: str | None = None,
        customer_name: str | None = None,
        description: str | None = None,
    ) -> TestPaymentResult:
        """Present a real debit in Pinch's test environment, forced to fail.

        Verified against https://docs.getpinch.com.au/docs/test-and-live-mode
        on 2026-07-31: the sandbox dishonours a payment with a specific code
        when that code appears anywhere in the `description` (or the payer's
        `firstName`) prefixed with `#`. Nothing about the request is otherwise
        special — it is an ordinary `POST /payments`, and the dishonour comes
        back on a `bank-results` event like any other.

        Refused outright against the live base URL. There is no such thing as
        a forced dishonour in production: the same call would debit a real
        person, and one environment variable is all that separates the two.
        """
        if settings.targets_live_money:
            return TestPaymentResult(
                accepted=False,
                error_code="live_environment",
                message=(
                    "PINCH_API_BASE points at /live/. Forcing a dishonour there "
                    "would present a real debit against a real account. Point at "
                    f"{PINCH_TEST_BASE} first."
                ),
            )

        note = description or "Recovery engine test payment"
        if raw_code:
            # The `#code` is the whole mechanism. It stays at the end so the
            # human-readable part of the description reads first in the portal.
            note = f"{note} #{raw_code}"

        with self._session_factory() as session:
            customer = (
                session.get(Customer, customer_id) if customer_id else None
            )
            if customer is None:
                customer = Customer(
                    id=customer_id or new_id("cus"),
                    name=customer_name or "Pinch sandbox customer",
                    email=f"{new_id('sandbox')}@mailinator.com",
                )
                session.add(customer)
                session.flush()

            try:
                if not customer.pinch_payer_id:
                    payer = self._request(
                        "POST",
                        "/payers",
                        json={
                            "firstName": customer.name or "Customer",
                            "emailAddress": customer.email
                            or f"{customer.id}@example.invalid",
                        },
                    ).json()
                    customer.pinch_payer_id = payer["id"]

                if not customer.pinch_source_id:
                    # Documented test bank account. Any BSB/account is accepted
                    # in test mode; these are the values the docs name, so a
                    # failure here is a credential or environment problem
                    # rather than a data one.
                    source = self._request(
                        "POST",
                        f"/payers/{customer.pinch_payer_id}/sources",
                        json={
                            "sourceType": "bank-account",
                            "bankAccountName": customer.name or "Test Account",
                            "bankAccountBsb": TEST_BSB,
                            "bankAccountNumber": TEST_ACCOUNT_NUMBER,
                        },
                    ).json()
                    customer.pinch_source_id = source["id"]
                    customer.bank_account_name = customer.name
                    customer.bank_bsb = TEST_BSB
                    customer.bank_account_last4 = TEST_ACCOUNT_NUMBER[-4:]

                body: dict[str, Any] = {
                    "payerId": customer.pinch_payer_id,
                    "sourceId": customer.pinch_source_id,
                    "amount": amount_cents,
                    "description": note,
                    # Today, so a Time-Travel'd poll past tonight's processing
                    # run picks the result up rather than waiting a day.
                    "transactionDate": clock.now().date().isoformat(),
                }
                created = self._request("POST", "/payments", json=body).json()
            except httpx.HTTPStatusError as exc:
                session.rollback()
                return TestPaymentResult(
                    accepted=False,
                    forced_code=raw_code,
                    error_code=f"http_{exc.response.status_code}",
                    message=exc.response.text,
                )

            result = TestPaymentResult(
                accepted=True,
                forced_code=raw_code,
                pinch_payment_id=created.get("id"),
                pinch_payer_id=customer.pinch_payer_id,
                customer_id=customer.id,
                message=(
                    "Debit submitted to the Pinch test environment. The forced "
                    "dishonour arrives on a bank-results event after the "
                    "processing run — poll for it, or Time-Travel past tonight."
                ),
            )
            session.commit()

        return result

    # -- events ------------------------------------------------------------

    def list_events(
        self, event_type: str | None = None, page: int = 1, page_size: int = 50
    ) -> list[EventSummary]:
        """`GET /events`. Newest first, summaries only.

        Verified against https://docs.getpinch.com.au/reference/list-all-events
        on 2026-07-31: `page`, `pageSize` (max 500), and a single `eventType`
        filter. Entries carry `metadata` — a quick-reference summary — and not
        `data`, so the payments in a batch need `get_event`.
        """
        params: dict[str, Any] = {"page": page, "pageSize": min(page_size, 500)}
        if event_type:
            params["eventType"] = event_type

        try:
            payload = self._request("GET", "/events", params=params).json()
        except httpx.HTTPStatusError:
            # A poll that raises takes the ingest loop down; one that returns
            # nothing is retried on the next tick.
            logger.exception("Listing Pinch events failed")
            return []

        return [
            EventSummary(
                id=str(item.get("id")),
                type=str(item.get("type") or ""),
                event_date=item.get("eventDate"),
            )
            for item in payload.get("data") or []
            if item.get("id")
        ]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        """`GET /events/{id}` — the full envelope, `data` included."""
        try:
            return self._request("GET", f"/events/{event_id}").json()
        except httpx.HTTPStatusError:
            logger.exception("Fetching Pinch event %s failed", event_id)
            return None


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------


def get_pinch_client(
    session_factory: Callable[[], Session] | None = None,
) -> PinchClient:
    """Return the client for the configured mode.

    Not cached: PINCH_MODE is read per call so a test can flip it without
    reaching into module state, and constructing either client is cheap.

    `session_factory` affects both clients, though for different reasons. The
    mock resolves *every* decision against local tables — it has no socket.
    The live client has a socket for the actual payment decision, but still
    needs a session to cache pinch_payer_id/pinch_source_id/pinch_payment_id
    locally. Callers already holding a session pass a factory bound to the
    same database — otherwise either client reaches for the module-level
    SessionLocal and a test would read/write the development database instead
    of its own.
    """
    if settings.PINCH_MODE == "live":
        if session_factory is None:
            return LivePinchClient()
        return LivePinchClient(session_factory=session_factory)
    if session_factory is None:
        return MockPinchClient()
    return MockPinchClient(session_factory=session_factory)
