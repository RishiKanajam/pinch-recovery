"""Classifier: every code in the table resolves, everything else is `unknown`.

The important assertions here are the negative ones. A classifier that raises
on an unexpected code takes down the ingest path at the worst possible moment —
the first time a real Pinch payload carries a code we guessed wrong about.
"""

from __future__ import annotations

import pytest
import yaml

from app.models.enums import HARD_FAILURE_CLASSES, ActionType, FailureClass
from app.services.classifier import (
    STRATEGIES_PATH,
    StrategyTableError,
    get_strategy_table,
    load_strategy_table,
)


@pytest.fixture(scope="module")
def table():
    return load_strategy_table()


def test_every_failure_class_has_a_strategy(table):
    for failure_class in FailureClass:
        strategy = table.strategy_for(failure_class)
        assert strategy.failure_class is failure_class


def test_every_raw_code_in_the_yaml_maps_to_its_own_class(table):
    """Walk the YAML directly rather than trusting the parsed index."""
    raw = yaml.safe_load(STRATEGIES_PATH.read_text())
    for name, cfg in raw["classes"].items():
        for code in cfg.get("raw_codes") or []:
            assert table.classify(code) is FailureClass(name), (
                f"{code!r} should classify as {name}"
            )


@pytest.mark.parametrize(
    "code,expected",
    [
        ("AM04", FailureClass.INSUFFICIENT_FUNDS),
        ("AC01", FailureClass.INVALID_ACCOUNT),
        ("MD01", FailureClass.AUTHORITY_CANCELLED),
        ("MS02", FailureClass.PAYMENT_STOPPED),
        ("AG01", FailureClass.TECHNICAL),
        ("54", FailureClass.EXPIRED_CARD),
        ("05", FailureClass.DO_NOT_HONOUR),
    ],
)
def test_representative_codes(table, code, expected):
    assert table.classify(code) is expected


@pytest.mark.parametrize("code", ["ac01", "AC01 ", " ac01\t", "Ac01"])
def test_classification_is_case_and_whitespace_insensitive(table, code):
    """Real payloads arrive with inconsistent casing and stray whitespace."""
    assert table.classify(code) is FailureClass.INVALID_ACCOUNT


@pytest.mark.parametrize(
    "code",
    [
        None,
        "",
        "   ",
        "ZZ99",
        "definitely-not-a-code",
        "0000",
        123,
        3.14,
        [],
        {},
        object(),
    ],
)
def test_unrecognised_input_is_unknown_and_never_raises(table, code):
    assert table.classify(code) is FailureClass.UNKNOWN


def test_unknown_class_is_conservative_not_empty(table):
    """`unknown` must still do something and must involve a human."""
    strategy = table.strategy_for(FailureClass.UNKNOWN)
    assert strategy.notify_human is True
    assert strategy.actions, "unknown must not be a no-op"
    assert strategy.reasoning


def test_hard_failures_have_zero_retries_and_no_retry_action(table):
    """README non-negotiable 5. This is the difference from a cron job."""
    for failure_class in HARD_FAILURE_CLASSES:
        strategy = table.strategy_for(failure_class)
        assert strategy.max_attempts == 0, f"{failure_class.value} must never retry"
        assert not [a for a in strategy.actions if a.action is ActionType.RETRY]


def test_every_class_carries_reasoning_and_diagnosis(table):
    for failure_class in FailureClass:
        strategy = table.strategy_for(failure_class)
        assert strategy.reasoning.strip(), f"{failure_class.value} has no reasoning"
        # Reasoning must read as prose, not a label — the judge reads this.
        assert len(strategy.reasoning.split()) >= 8


def test_no_code_maps_to_two_classes(table):
    """A duplicate code would make classification order-dependent."""
    index = table.known_codes()
    assert len(index) == len(set(index))


def test_global_rules_loaded(table):
    rules = table.global_rules
    assert rules.customer_max_retries_in_window > 0
    assert rules.min_hours_between_customer_messages > 0
    assert rules.write_off_after_days > 0
    assert rules.default_payday_weekdays
    assert all(0 <= d <= 6 for d in rules.default_payday_weekdays)


def test_cached_table_is_reused():
    assert get_strategy_table() is get_strategy_table()
    assert get_strategy_table(reload=True) is get_strategy_table()


# --- loader validation: these all fail the build rather than a payment --------


def _write(tmp_path, doc: dict):
    path = tmp_path / "strategies.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


def _minimal_classes() -> dict:
    """A valid table for every class, used as a base for mutation."""
    classes = {}
    for failure_class in FailureClass:
        classes[failure_class.value] = {
            "raw_codes": [f"code-{failure_class.value}"],
            "diagnosis": "d",
            "max_attempts": 0 if failure_class in HARD_FAILURE_CLASSES else 1,
            "notify_human": False,
            "actions": [{"action": "notify_human", "delay_hours": 0}],
            "reasoning": "This sentence is long enough to pass the prose check easily.",
        }
    return classes


def test_loader_accepts_the_minimal_valid_table(tmp_path):
    load_strategy_table(_write(tmp_path, {"classes": _minimal_classes()}))


def test_loader_rejects_hard_failure_with_retries(tmp_path):
    classes = _minimal_classes()
    classes["invalid_account"]["max_attempts"] = 2
    with pytest.raises(StrategyTableError, match="hard failure"):
        load_strategy_table(_write(tmp_path, {"classes": classes}))


def test_loader_rejects_hard_failure_with_retry_action(tmp_path):
    classes = _minimal_classes()
    classes["payment_stopped"]["actions"] = [{"action": "retry", "delay_hours": 24}]
    with pytest.raises(StrategyTableError, match="must not list a `retry` action"):
        load_strategy_table(_write(tmp_path, {"classes": classes}))


def test_loader_rejects_missing_reasoning(tmp_path):
    classes = _minimal_classes()
    classes["technical"]["reasoning"] = ""
    with pytest.raises(StrategyTableError, match="no `reasoning`"):
        load_strategy_table(_write(tmp_path, {"classes": classes}))


def test_loader_rejects_missing_class(tmp_path):
    classes = _minimal_classes()
    del classes["expired_card"]
    with pytest.raises(StrategyTableError, match="missing these classes"):
        load_strategy_table(_write(tmp_path, {"classes": classes}))


def test_loader_rejects_unknown_class_name(tmp_path):
    classes = _minimal_classes()
    classes["not_a_real_class"] = classes["technical"]
    with pytest.raises(StrategyTableError, match="is not a FailureClass"):
        load_strategy_table(_write(tmp_path, {"classes": classes}))


def test_loader_rejects_duplicate_code_across_classes(tmp_path):
    classes = _minimal_classes()
    classes["technical"]["raw_codes"] = ["AC01"]
    classes["invalid_account"]["raw_codes"] = ["AC01"]
    with pytest.raises(StrategyTableError, match="mapped to both"):
        load_strategy_table(_write(tmp_path, {"classes": classes}))


def test_loader_rejects_bad_action_name(tmp_path):
    classes = _minimal_classes()
    classes["technical"]["actions"] = [{"action": "teleport", "delay_hours": 1}]
    with pytest.raises(StrategyTableError, match="is invalid"):
        load_strategy_table(_write(tmp_path, {"classes": classes}))


def test_loader_rejects_negative_delay(tmp_path):
    classes = _minimal_classes()
    classes["technical"]["actions"] = [{"action": "retry", "delay_hours": -5}]
    with pytest.raises(StrategyTableError, match="is invalid"):
        load_strategy_table(_write(tmp_path, {"classes": classes}))


def test_loader_rejects_missing_classes_key(tmp_path):
    with pytest.raises(StrategyTableError, match="top-level `classes` key"):
        load_strategy_table(_write(tmp_path, {"global_rules": {}}))


def test_loader_rejects_missing_file(tmp_path):
    with pytest.raises(StrategyTableError, match="not found"):
        load_strategy_table(tmp_path / "nope.yaml")
