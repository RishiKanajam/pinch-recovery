"""The Pinch boundary.

Everything that would talk to Pinch goes through PinchClient. Callers get the
same interface and the same result objects in both modes, so no caller ever
branches on PINCH_MODE — that is the whole point of this module. If a caller
ever needs to know which implementation it holds, this abstraction has failed.

Mock is the default and is first-class: the entire system runs end to end
without Pinch credentials.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

import httpx
from sqlalchemy.orm import Session

from app.core import clock
from app.core.config import settings
from app.core.db import SessionLocal
from app.models.customer import Customer
from app.models.payment import Payment

PINCH_DOCS = "https://docs.getpinch.com.au"

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


# --------------------------------------------------------------------------
# Mock
# --------------------------------------------------------------------------


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

        # TODO(simulator): schedule the settlement webhook here once
        # /sim/scenarios lands, so a retry produces a real dishonour or
        # success after the (fast-forwardable) settlement window. Until then
        # the instruction is accepted and nothing settles.
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
            data={"grant_type": "client_credentials"},
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
        # Auth and transport above are ready; only the request body is unknown.
        # Filling this in is a one-line `self._request("POST", ...)` call.
        raise NotImplementedError(
            "LivePinchClient.retry_payment is not wired yet.\n"
            "TODO(live): confirm the payment re-present endpoint and body at\n"
            f"{PINCH_DOCS}/docs/direct-debit-payments — Pinch's vocabulary is\n"
            "Payers/Sources/Payments, so a 'retry' is most likely a new payment\n"
            "against the same source rather than a retry endpoint.\n"
            "Map onto RetryResult(accepted=..., status='pending'). A 200 means\n"
            "the instruction was accepted, NOT that the money moved — the\n"
            "dishonour or success arrives later by webhook."
        )

    def update_payment_method(
        self, customer_id: str, details: dict[str, Any]
    ) -> PaymentMethodResult:
        raise NotImplementedError(
            "LivePinchClient.update_payment_method is not wired yet.\n"
            "TODO(live): confirm the payer/source endpoint at\n"
            f"{PINCH_DOCS}/docs/pinch-payments-api-core-concepts — bank details\n"
            "are a 'Source' attached to a 'Payer'.\n"
            "Preferred flow: the browser tokenises via CaptureJS with\n"
            "PINCH_PUBLISHABLE_KEY and posts us a token, so the raw account\n"
            "number never reaches our server. Persist only the last four\n"
            "digits locally, as MockPinchClient does."
        )


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------


def get_pinch_client() -> PinchClient:
    """Return the client for the configured mode.

    Not cached: PINCH_MODE is read per call so a test can flip it without
    reaching into module state, and constructing either client is cheap.
    """
    if settings.PINCH_MODE == "live":
        return LivePinchClient()
    return MockPinchClient()
