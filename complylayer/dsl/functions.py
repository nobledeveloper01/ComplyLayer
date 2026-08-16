"""The functions a rule may call.

This set is the widest part of the security boundary that a non-engineer can
reach, so it stays small and every addition is a reviewed change with a
corresponding entry in the escape corpus.

Phase 1 declares the names and their signatures. The implementations arrive with
the interpreter in phase 2 — the validator's job is to know what may be called
and with which keywords, not yet what happens when it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
