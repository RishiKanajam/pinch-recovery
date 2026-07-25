"""The contract enums.

These are the five enums in docs/CONTRACT.md, verbatim. FailureClass is ours,
not Pinch's — Pinch's raw string is preserved separately in `raw_code`, so a
wrong guess about their code strings costs one strategies.yaml row rather than
a refactor.

Values are lowercase snake_case strings on the wire. Subclassing str keeps them
JSON-serialisable and comparable to the raw strings coming out of the YAML
without any conversion layer.
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


# The three classes where a retry cannot succeed and costs a dishonour fee every
# time it is attempted. strategies.yaml sets max_attempts: 0 for each, but that
# is data and data can be edited by mistake; the engine also asserts against
# this set so the rule survives a bad YAML edit. See docs/CONTRACT.md and
# README "Non-negotiables" rule 5.
HARD_FAILURE_CLASSES: frozenset[FailureClass] = frozenset(
    {
        FailureClass.INVALID_ACCOUNT,
        FailureClass.AUTHORITY_CANCELLED,
        FailureClass.PAYMENT_STOPPED,
    }
)
