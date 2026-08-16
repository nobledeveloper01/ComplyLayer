"""The functions a rule may call.

This set is the widest part of the security boundary that a non-engineer can
reach, so it stays small and every addition is a reviewed change with a
corresponding entry in the escape corpus.

Phase 1 declared the names and signatures; phase 2 adds the implementations.
They are bound to a context rather than imported, because the velocity family
needs a data source — an in-memory one in tests, a Redis-backed one from phase 3
— and a function that reaches for a global is a function that cannot be tested
or swapped.

Every one of them is integer in, integer out. D6 took division out of the
grammar so a rule cannot produce a float; these would put one straight back if
they returned an average or a percentage as a real number.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from complylayer.dsl import errors


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    summary: str
    positional: int = 0
    keywords: frozenset[str] = field(default_factory=frozenset)


# `percent_of` is integer-only and floors, per D6 in docs/plan-architecture.md:
# division leaves the DSL entirely, because 100% reproducibility and floats do
# not sit comfortably together in a system that has to explain a decision made
# six months ago.
SPECS: dict[str, FunctionSpec] = {
    "velocity_count": FunctionSpec(
        name="velocity_count",
        summary="How many transactions fall in a rolling window, optionally filtered by amount.",
        keywords=frozenset({"window", "min_amount_minor", "max_amount_minor", "transaction_type"}),
    ),
    "velocity_sum": FunctionSpec(
        name="velocity_sum",
        summary="Total value of transactions in a rolling window, in minor units.",
        keywords=frozenset({"window", "min_amount_minor", "max_amount_minor", "transaction_type"}),
    ),
    "days_since": FunctionSpec(
        name="days_since",
        summary="Whole days between a timestamp fact and now.",
        positional=1,
    ),
    "in_list": FunctionSpec(
        name="in_list",
        summary="Whether a value appears in a named list, such as high_risk_countries.",
        positional=2,
    ),
    "hour_of_day": FunctionSpec(
        name="hour_of_day",
        summary="Hour of the transaction, 0 to 23, in the tenant's timezone.",
    ),
    "abs": FunctionSpec(name="abs", summary="Absolute value.", positional=1),
    "min": FunctionSpec(name="min", summary="The smaller of two values.", positional=2),
    "max": FunctionSpec(name="max", summary="The larger of two values.", positional=2),
    "percent_of": FunctionSpec(
        name="percent_of",
        summary="A whole percentage of an amount, rounded down. percent_of(1000, 90) is 900.",
        positional=2,
    ),
}

ALLOWED_FUNCTIONS = frozenset(SPECS)


def reference() -> str:
    """A plain-language function list, for the rule builder and the docs."""
    width = max(len(name) for name in SPECS)
    return "\n".join(f"{spec.name.ljust(width)}  {spec.summary}" for spec in SPECS.values())


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------

WINDOW_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3_600,
    "6h": 21_600,
    "24h": 86_400,
    "7d": 604_800,
    "30d": 2_592_000,
}


class VelocityProvider(Protocol):  # pragma: no cover - an interface, not code
    """Rolling-window counters over one customer's recent transactions.

    Phase 3 implements this over Redis sorted sets, gathering every window in a
    single pipelined round trip. The interpreter only ever sees this interface,
    so phase 2 can be tested against an in-memory implementation and phase 3 can
    swap in the real one without touching a rule.
    """

    def count(
        self,
        window_seconds: int,
        min_amount_minor: int | None = None,
        max_amount_minor: int | None = None,
        transaction_type: str | None = None,
    ) -> int: ...

    def total(
        self,
        window_seconds: int,
        min_amount_minor: int | None = None,
        max_amount_minor: int | None = None,
        transaction_type: str | None = None,
    ) -> int: ...


def _window_seconds(window: Any) -> int:
    if not isinstance(window, str) or window not in WINDOW_SECONDS:
        supported = ", ".join(WINDOW_SECONDS)
        raise errors.RuleEvaluationError(f"{window!r} is not a window. Use one of: {supported}")
    return WINDOW_SECONDS[window]


def _whole_number(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise errors.RuleEvaluationError(f"{label} must be a whole number")
    return value


def build(velocity: VelocityProvider, now: datetime) -> Mapping[str, Callable[..., Any]]:
    """Bind the allowlisted functions to one transaction's data sources.

    ``now`` is passed in rather than read from the clock inside each function.
    Two calls to ``days_since`` within one evaluation must agree, and a decision
    replayed for an audit has to reproduce against the instant it was originally
    made — reading the clock twice would make both of those untrue.
    """

    def velocity_count(
        window: str,
        min_amount_minor: int | None = None,
        max_amount_minor: int | None = None,
        transaction_type: str | None = None,
    ) -> int:
        return velocity.count(
            _window_seconds(window),
            min_amount_minor=min_amount_minor,
            max_amount_minor=max_amount_minor,
            transaction_type=transaction_type,
        )

    def velocity_sum(
        window: str,
        min_amount_minor: int | None = None,
        max_amount_minor: int | None = None,
        transaction_type: str | None = None,
    ) -> int:
        return velocity.total(
            _window_seconds(window),
            min_amount_minor=min_amount_minor,
            max_amount_minor=max_amount_minor,
            transaction_type=transaction_type,
        )

    def days_since(timestamp: Any) -> int:
        """Whole days, rounded down, from an ISO-8601 timestamp fact."""
        if isinstance(timestamp, str):
            try:
                moment = datetime.fromisoformat(timestamp)
            except ValueError:
                raise errors.RuleEvaluationError(f"{timestamp!r} is not a date") from None
        elif isinstance(timestamp, datetime):
            moment = timestamp
        else:
            raise errors.RuleEvaluationError("days_since needs a date")

        if moment.tzinfo is None:
            # A naive timestamp is ambiguous by definition. Assuming UTC is a
            # choice, so it is made here in the open rather than by whichever
            # server happened to run the decision.
            moment = moment.replace(tzinfo=UTC)

        return (now - moment).days

    def in_list(value: Any, candidates: Any) -> bool:
        if not isinstance(candidates, tuple | str):
            raise errors.RuleEvaluationError("in_list needs a list to look in")
        return value in candidates

    def hour_of_day() -> int:
        return now.hour

    def percent_of(amount: Any, percent: Any) -> int:
        """Integer percentage, rounded down. See D6.

        Rounding down is stated in the function reference because somebody will
        eventually ask which way the last kobo goes, and the answer needs to
        have been decided rather than discovered.
        """
        return _whole_number(amount, "amount") * _whole_number(percent, "percent") // 100

    return {
        "velocity_count": velocity_count,
        "velocity_sum": velocity_sum,
        "days_since": days_since,
        "in_list": in_list,
        "hour_of_day": hour_of_day,
        "abs": lambda value: abs(_whole_number(value, "value")),
        "min": lambda a, b: min(_whole_number(a, "value"), _whole_number(b, "value")),
        "max": lambda a, b: max(_whole_number(a, "value"), _whole_number(b, "value")),
        "percent_of": percent_of,
    }
