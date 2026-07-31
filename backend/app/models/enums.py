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
    # Offer to break the outstanding amount into instalments. Reached only
    # after repeated insufficient-funds dishonours, where the evidence says the
    # customer wants to pay and cannot clear the full amount in one debit —
    # presenting the same figure a fourth time is the definition of a cron job.
    OFFER_SPLIT = "offer_split"
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


# Classes where a retry is guaranteed to fail or actively harmful: the account
# does not exist, the mandate was revoked, or the customer stopped the payment.
# The rule that separates this product from a cron job — hard failures are
# never retried. Used by the classifier; values agreed with Person A's enums.
HARD_FAILURE_CLASSES: frozenset[FailureClass] = frozenset(
    {
        FailureClass.INVALID_ACCOUNT,
        FailureClass.AUTHORITY_CANCELLED,
        FailureClass.PAYMENT_STOPPED,
        # blocked-by-bank. Pinch's docs are explicit that the bank will reject
        # all future attempts, so a retry cannot succeed and only earns another
        # dishonour fee. Previously modelled as a soft "ambiguous decline" and
        # retried once; that disagreed with the processor's own documentation.
        FailureClass.DO_NOT_HONOUR,
    }
)
