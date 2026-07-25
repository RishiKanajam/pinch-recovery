"""The mode switch, and the seam Person B builds against.

The load-bearing assertion here is not "mock is the default" — it is that a
caller holding a PinchClient cannot tell which implementation it has. If the
two ever diverge in interface, the mock->live switch breaks at the worst
possible moment.
"""

from __future__ import annotations

import inspect

import httpx
import pytest

from app.core import clock
from app.core.config import settings
from app.services.pinch_client import (
    LivePinchClient,
    MockPinchClient,
    PinchClient,
    get_pinch_client,
)


def test_default_mode_is_mock():
    """PINCH_MODE defaults to mock — no credentials needed to develop."""
    assert settings.PINCH_MODE == "mock"


def test_get_pinch_client_returns_mock_when_mode_is_mock(monkeypatch):
    monkeypatch.setattr(settings, "PINCH_MODE", "mock")
    client = get_pinch_client()
    assert isinstance(client, MockPinchClient)
    assert isinstance(client, PinchClient)


def test_get_pinch_client_returns_live_when_mode_is_live(monkeypatch):
    """The switch actually flips — a factory stuck on mock would pass the
    test above while making PINCH_MODE=live a no-op."""
    monkeypatch.setattr(settings, "PINCH_MODE", "live")
    assert isinstance(get_pinch_client(), LivePinchClient)


def test_constructing_live_client_opens_no_connection(monkeypatch):
    """Importing or building the live client must not need a key or a socket,
    or merely setting PINCH_MODE=live would fail at import time."""
    monkeypatch.setattr(settings, "PINCH_MODE", "live")
    client = get_pinch_client()
    assert client._client is None


@pytest.mark.parametrize("method", ["retry_payment", "update_payment_method"])
def test_implementations_share_one_signature(method):
    """Person B must not be able to tell the implementations apart."""
    mock_sig = inspect.signature(getattr(MockPinchClient, method))
    live_sig = inspect.signature(getattr(LivePinchClient, method))
    assert mock_sig == live_sig

    base_sig = inspect.signature(getattr(PinchClient, method))
    assert mock_sig == base_sig


def test_pinch_client_cannot_be_instantiated_directly():
    """The ABC is an interface, not a usable default."""
    with pytest.raises(TypeError):
        PinchClient()  # type: ignore[abstract]


# --------------------------------------------------------------------------
# OAuth2 token handling
# --------------------------------------------------------------------------


class _FakeTokenResponse:
    def __init__(self, token: str = "tok_abc", expires_in: int = 3600) -> None:
        self._payload = {
            "access_token": token,
            "expires_in": expires_in,
            "token_type": "Bearer",
        }

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_token_is_fetched_once_and_cached(monkeypatch):
    """The docs say tokens last an hour and must be cached, not re-fetched
    per call. A client that re-authenticates every request would still pass
    a naive 'does it work' check while burning a round trip each time."""
    calls: list[str] = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return _FakeTokenResponse()

    monkeypatch.setattr(httpx, "post", fake_post)

    client = LivePinchClient(application_id="app_1", secret_key="sec_1")
    assert client._get_token() == "tok_abc"
    assert client._get_token() == "tok_abc"
    assert client._get_token() == "tok_abc"

    assert len(calls) == 1
    assert calls[0] == "https://auth.getpinch.com.au/connect/token"


def test_token_uses_basic_auth_with_app_id_and_secret(monkeypatch):
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return _FakeTokenResponse()

    monkeypatch.setattr(httpx, "post", fake_post)

    LivePinchClient(application_id="app_1", secret_key="sec_1")._get_token()

    assert isinstance(captured["auth"], httpx.BasicAuth)
    assert captured["data"] == {"grant_type": "client_credentials"}


def test_token_refreshes_after_the_simulated_clock_advances(monkeypatch):
    """Expiry is measured on clock.now(), so a fast-forward past the token
    lifetime must force a refresh rather than reusing a dead token."""
    calls: list[str] = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return _FakeTokenResponse(expires_in=3600)

    monkeypatch.setattr(httpx, "post", fake_post)

    client = LivePinchClient(application_id="app_1", secret_key="sec_1")
    client._get_token()
    assert len(calls) == 1

    try:
        clock.fast_forward(7200)  # two hours: past the one-hour lifetime
        client._get_token()
    finally:
        clock.reset()

    assert len(calls) == 2


def test_missing_credentials_fail_loudly(monkeypatch):
    """An empty credential must raise, not send an unauthenticated request."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("called"))

    client = LivePinchClient(application_id="", secret_key="")
    with pytest.raises(RuntimeError, match="PINCH_APPLICATION_ID"):
        client._get_token()


def test_default_api_base_is_the_test_environment():
    """/test/ and /live/ take the same credentials — this path segment is the
    only thing standing between a demo and real money."""
    assert settings.PINCH_API_BASE.rstrip("/").endswith("/test")
    assert settings.targets_live_money is False
