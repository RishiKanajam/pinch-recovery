"""Raw dishonour code -> FailureClass, driven by strategies.yaml.

The mapping lives in YAML rather than in code on purpose: the raw code strings
are the one thing in this system we are guessing about until someone checks
Pinch's docs, and a wrong guess should cost a one-line data edit rather than a
deploy. See the header comment in strategies.yaml.

This module never raises on bad input. An unrecognised code classifies as
`unknown`, which has its own conservative strategy (one cautious retry, tell a
human so the table can be extended). A classifier that throws on an unexpected
code would take the ingest path down during a live demo — the exact moment a
novel code is most likely to show up.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import yaml

from app.models.enums import HARD_FAILURE_CLASSES, FailureClass
from app.models.schemas import Strategy, StrategyAction

logger = logging.getLogger(__name__)

STRATEGIES_PATH = Path(__file__).resolve().parent / "strategies.yaml"


class StrategyTableError(RuntimeError):
    """The YAML is malformed in a way that would silently break the product.

    Raised only at load time, never per-payment. Failing loudly at startup is
    correct; failing on a payment mid-demo is not.
    """


class GlobalRules:
    """The cross-cutting rules the engine enforces regardless of class."""

    def __init__(self, raw: dict[str, Any] | None = None) -> None:
        raw = raw or {}
        self.customer_retry_budget_days: int = int(raw.get("customer_retry_budget_days", 30))
        self.customer_max_retries_in_window: int = int(
            raw.get("customer_max_retries_in_window", 5)
        )
        self.min_hours_between_customer_messages: int = int(
            raw.get("min_hours_between_customer_messages", 24)
        )
        self.default_payday_weekdays: list[int] = [
            int(d) for d in raw.get("default_payday_weekdays", [3, 4])
        ]
        self.write_off_after_days: int = int(raw.get("write_off_after_days", 21))


class StrategyTable:
    """Parsed strategies.yaml: code -> class, and class -> Strategy."""

    def __init__(
        self,
        strategies: dict[FailureClass, Strategy],
        codes_by_class: dict[FailureClass, list[str]],
        rules: GlobalRules,
    ) -> None:
        self._strategies = strategies
        self.global_rules = rules
        # Raw codes are held here rather than on Strategy: they are a loader
        # concern, and hanging them off the pydantic model would leak them into
        # every serialised API response.
        self._codes_by_class = codes_by_class
        # Lowercased for lookup so "AC01", "ac01", and " AC01 " all land.
        self._code_index: dict[str, FailureClass] = {}
        for failure_class, codes in codes_by_class.items():
            for code in codes:
                key = str(code).strip().lower()
                if not key:
                    continue
                if key in self._code_index and self._code_index[key] != failure_class:
                    raise StrategyTableError(
                        f"Raw code {code!r} is mapped to both "
                        f"{self._code_index[key].value} and {failure_class.value}. "
                        "A code must resolve to exactly one class."
                    )
                self._code_index[key] = failure_class

    def classify(self, raw_code: str | None) -> FailureClass:
        """Map a raw code to a class. Never raises; unknown codes fall back."""
        if raw_code is None:
            return FailureClass.UNKNOWN
        if not isinstance(raw_code, str):
            logger.warning("Non-string dishonour code %r; classifying as unknown", raw_code)
            return FailureClass.UNKNOWN

        key = raw_code.strip().lower()
        if not key:
            return FailureClass.UNKNOWN

        found = self._code_index.get(key)
        if found is None:
            # Worth a log line: this is the signal that strategies.yaml needs a
            # new row, and the unknown strategy also notifies a human.
            logger.info("Unrecognised dishonour code %r; classifying as unknown", raw_code)
            return FailureClass.UNKNOWN
        return found

    def strategy_for(self, failure_class: FailureClass) -> Strategy:
        """The strategy for a class, guaranteed present for every enum member."""
        strategy = self._strategies.get(failure_class)
        if strategy is None:
            # Only reachable if the YAML is missing a class the enum defines.
            # Fall back to unknown rather than blowing up a request.
            logger.error(
                "strategies.yaml has no entry for %s; falling back to unknown",
                failure_class.value,
            )
            return self._strategies[FailureClass.UNKNOWN]
        return strategy

    def known_codes(self) -> dict[str, FailureClass]:
        return dict(self._code_index)

    def raw_codes_for(self, failure_class: FailureClass) -> list[str]:
        """The raw codes mapped to a class. Shown in the drill-down view."""
        return list(self._codes_by_class.get(failure_class, []))

    @property
    def classes(self) -> dict[FailureClass, Strategy]:
        return dict(self._strategies)


def _parse_actions(raw_actions: Any, failure_class: str) -> list[StrategyAction]:
    if raw_actions is None:
        return []
    if not isinstance(raw_actions, list):
        raise StrategyTableError(f"{failure_class}.actions must be a list")

    actions: list[StrategyAction] = []
    for i, raw in enumerate(raw_actions):
        if not isinstance(raw, dict):
            raise StrategyTableError(f"{failure_class}.actions[{i}] must be a mapping")
        try:
            actions.append(StrategyAction(**raw))
        except Exception as exc:  # pydantic validation, unknown keys, bad enum
            raise StrategyTableError(
                f"{failure_class}.actions[{i}] is invalid: {exc}"
            ) from exc
    return actions


def load_strategy_table(path: Path | None = None) -> StrategyTable:
    """Read and validate strategies.yaml.

    Validation is strict here because every failure this catches is a failure
    that would otherwise show up as a wrong decision on a real payment, which
    is much harder to notice than a stack trace at startup.
    """
    path = path or STRATEGIES_PATH
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise StrategyTableError(f"strategies.yaml not found at {path}") from exc
    except yaml.YAMLError as exc:
        raise StrategyTableError(f"strategies.yaml is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict) or "classes" not in raw:
        raise StrategyTableError("strategies.yaml must have a top-level `classes` key")

    raw_classes = raw["classes"]
    if not isinstance(raw_classes, dict):
        raise StrategyTableError("`classes` must be a mapping of class name -> config")

    strategies: dict[FailureClass, Strategy] = {}
    codes_by_class: dict[FailureClass, list[str]] = {}
    for name, cfg in raw_classes.items():
        try:
            failure_class = FailureClass(name)
        except ValueError as exc:
            raise StrategyTableError(
                f"{name!r} in strategies.yaml is not a FailureClass. "
                f"Valid values: {[c.value for c in FailureClass]}"
            ) from exc

        if not isinstance(cfg, dict):
            raise StrategyTableError(f"classes.{name} must be a mapping")

        actions = _parse_actions(cfg.get("actions"), name)
        max_attempts = int(cfg.get("max_attempts", 0))
        reasoning = str(cfg.get("reasoning", "")).strip()
        diagnosis = str(cfg.get("diagnosis", "")).strip()

        if not reasoning:
            # Rule 4 of the README non-negotiables: no reasoning, no feature.
            raise StrategyTableError(
                f"classes.{name} has no `reasoning`. Every class must explain "
                "itself — the judge reads that field, not the code."
            )

        # Rule 5: hard failures are never retried. The YAML says max_attempts: 0
        # for these today; this check makes an accidental edit fail the build
        # instead of quietly costing the merchant a dishonour fee per retry.
        if failure_class in HARD_FAILURE_CLASSES:
            if max_attempts != 0:
                raise StrategyTableError(
                    f"classes.{name} is a hard failure and must have "
                    f"max_attempts: 0, got {max_attempts}. Retrying an invalid "
                    "account, a cancelled authority, or a stopped payment cannot "
                    "succeed and incurs a fee each time."
                )
            retry_actions = [a for a in actions if a.action.value == "retry"]
            if retry_actions:
                raise StrategyTableError(
                    f"classes.{name} is a hard failure and must not list a "
                    "`retry` action."
                )

        strategies[failure_class] = Strategy(
            failure_class=failure_class,
            actions=actions,
            max_attempts=max_attempts,
            notify_human=bool(cfg.get("notify_human", False)),
            reasoning=" ".join(reasoning.split()),
            diagnosis=" ".join(diagnosis.split()),
        )
        codes_by_class[failure_class] = [str(c) for c in (cfg.get("raw_codes") or [])]

    missing = [c for c in FailureClass if c not in strategies]
    if missing:
        raise StrategyTableError(
            "strategies.yaml is missing these classes: "
            + ", ".join(c.value for c in missing)
        )

    return StrategyTable(strategies, codes_by_class, GlobalRules(raw.get("global_rules")))


_table: StrategyTable | None = None
_table_lock = threading.Lock()


def get_strategy_table(reload: bool = False) -> StrategyTable:
    """Process-wide cached strategy table.

    Cached because the dashboard classifies every payment on every render and
    re-reading the YAML per payment is pointless IO. `reload=True` exists for
    tests and for editing the table during the hackathon without a restart.
    """
    global _table
    with _table_lock:
        if _table is None or reload:
            _table = load_strategy_table()
        return _table


def classify(raw_code: str | None) -> FailureClass:
    """Convenience wrapper over the cached table."""
    return get_strategy_table().classify(raw_code)
