"""Application settings, read from the environment or a local .env file.

Mock mode is the default deliberately: nothing here should require real Pinch
credentials to run. Variable names match .env.example exactly.

Pinch auth is OAuth2 client credentials, not a static API key:
Application ID + Secret Key are exchanged at PINCH_AUTH_BASE for a bearer
token. See https://docs.getpinch.com.au/docs/application-authentication.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # .env.local is read second and therefore wins. Real credentials live
        # there; .env stays shareable.
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # "live" is opted into once, late, deliberately — see docs/CONTRACT.md.
    # Typos fail loudly here rather than silently falling back to mock.
    PINCH_MODE: Literal["mock", "live"] = "mock"

    # --- Pinch credentials (only consulted when PINCH_MODE == "live") -------
    # One credential set covers both environments; PINCH_API_BASE alone
    # decides which one you are hitting.

    # OAuth2 client_id.
    PINCH_APPLICATION_ID: str = ""
    # OAuth2 client_secret. Server-side only — never send this to a browser.
    PINCH_SECRET_KEY: str = ""
    # Client-side key for CaptureJS tokenisation (pk_test_...). Safe to expose;
    # Person B's update-details page needs it to tokenise bank details so the
    # account number never reaches our server.
    PINCH_PUBLISHABLE_KEY: str = ""

    PINCH_AUTH_BASE: str = "https://auth.getpinch.com.au"

    # ---------------------------------------------------------------------
    # /test/ and /live/ take the SAME credentials. This path segment is the
    # only thing separating a demo from real money moving against real bank
    # accounts. For the hackathon this stays on /test/ — "live" in PINCH_MODE
    # means "really talk to Pinch", not "really move money".
    # ---------------------------------------------------------------------
    PINCH_API_BASE: str = "https://api.getpinch.com.au/test/"

    # Required on every Pinch request.
    PINCH_VERSION: str = "2020.1"

    DATABASE_URL: str = (
        "postgresql+psycopg://postgres:postgres@localhost:5432/pinch_recovery"
    )

    # The due-action poller runs on a background thread with its own session,
    # so it does not see a test's transaction or its dependency override — it
    # would write to the development database while the suite runs. Tests turn
    # it off; every other environment leaves it on.
    ENABLE_POLLER: bool = True
    POLLER_INTERVAL_SECONDS: float = 2.0

    # How often that poller asks Pinch for new events when PINCH_MODE=live.
    # Slower than the tick because it is an outbound HTTP call, not a database
    # read — a dishonour appearing within fifteen seconds is instant to a
    # judge, and polling every two would be rude to somebody else's API.
    PINCH_POLL_SECONDS: float = 15.0

    @property
    def targets_live_money(self) -> bool:
        """True when PINCH_API_BASE points at the real-money environment."""
        return "/live" in self.PINCH_API_BASE

    @property
    def pinch_credentials_present(self) -> bool:
        """Whether a live call could authenticate at all.

        Checked before any live request so an unset credential surfaces as one
        legible message rather than a stack trace on every poller tick.
        """
        return bool(self.PINCH_APPLICATION_ID and self.PINCH_SECRET_KEY)


settings = Settings()
