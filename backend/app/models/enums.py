"""Enum string values, transcribed from docs/CONTRACT.md.

These are stored as plain strings in the database, not native Postgres enums:
a wrong guess about a Pinch code costs a mapping-table row, and adding a new
FailureClass must never require an ALTER TYPE migration mid-hackathon.

Subclassing str means a member compares equal to its wire value, so
`payment.status == "failed"` works and JSON serialisation is automatic.
"""

from __future__ import annotations

from enum import Enum


class PaymentStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECOVERED = "recovered"
    WRITTEN_OFF = "written_off"


class FailureClass(str, Enum):
    INSUFFICIENT_FUNDS = "insufficient_funds"
    INVALID_ACCOUNT = "invalid_account"
    AUTHORITY_CANCELLED = "authority_cancelled"
    PAYMENT_STOPPED = "payment_stopped"
    TECHNICAL = "technical"
    EXPIRED_CARD = "expired_card"
    DO_NOT_HONOUR = "do_not_honour"
    UNKNOWN = "unknown"


class ActionType(str, Enum):
    RETRY = "retry"
    REQUEST_DETAILS_UPDATE = "request_details_update"
    NOTIFY_HUMAN = "notify_human"
    SAVE_OFFER = "save_offer"
    WRITE_OFF = "write_off"
    NONE = "none"


class Channel(str, Enum):
    EMAIL = "email"
    SMS = "sms"
    IN_APP = "in_app"
    PHONE = "phone"


class AttemptStatus(str, Enum):
    SCHEDULED = "scheduled"
    EXECUTED = "executed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
