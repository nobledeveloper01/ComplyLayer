"""Running a rule against decisions that already happened.

This is the feature the product is sold on. §2.5's before-and-after table has one
row that changes how a compliance officer works: *"Ability to test a rule against
history before activating it — none / backtest with impact report."* Everything
else makes changing a control possible; this makes changing one safe.

**The honesty problem, and how it is handled.**

D11 stored the resolved facts with each decision precisely so a replay could be a
reproduction rather than a re-evaluation. That works perfectly for a rule using
facts that were already gathered. It cannot work for a rule that needs a fact
nobody recorded — a velocity window that was never asked for, an aggregate that
did not exist then. Those facts are gone; Redis holds a rolling window that
moved on months ago.

So a backtest reports its own confidence:

- **Exact** — every fact the rule needs was recorded with the decision. The
  result is what would have happened.
- **Partial** — some decisions had the facts and some did not. The count covers
  only those that did, and says so.
- **Unavailable** — the rule needs a fact no decision recorded. No number is
  produced at all, because an approximate number on a screen where somebody is
  deciding whether to loosen a control is worse than an honest blank.

A compliance officer over-trusting a number is a worse outcome than showing them
a caveat, and the caveat has to travel with the number rather than sit in a
footnote.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from complylayer.dsl import parse
from complylayer.dsl.errors import RuleEvaluationError
from complylayer.dsl.interpreter import EvaluationContext, evaluate

# Backtests run against the read replica. §11.1: analytics must never touch the
# database serving decisions, and a 30-day replay is the heaviest read in the
# product.
REPLICA = "replica"


class Confidence(StrEnum):
    EXACT = "exact"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Sample:
    """One decision the candidate rule matched, for the drill-down.

    §3.2's B2 asks for "a drill-down sample of each", because a count without
    examples is a number an officer cannot sanity-check.
    """

    decision_id: str
    transaction_ref: str
    amount_minor: int
    currency: str
    decided_at: str
    original_outcome: str


@dataclass
class Impact:
    confidence: Confidence
    considered: int = 0
    skipped: int = 0
    matched: int = 0
    errored: int = 0
    samples: list[Sample] = field(default_factory=list)
    missing_facts: set[str] = field(default_factory=set)

    @property
    def total(self) -> int:
        return self.considered + self.skipped

    @property
    def match_rate(self) -> float:
        return self.matched / self.considered if self.considered else 0.0

    @property
    def sentence(self) -> str:
        """What the officer reads. The caveat travels with the number."""
        if self.confidence is Confidence.UNAVAILABLE:
            missing = ", ".join(sorted(self.missing_facts)) or "a fact"
            return (
                f"This rule cannot be tested against history: it needs {missing}, "
                "which was not recorded with past decisions. Run it in shadow mode "
                "instead — it will be evaluated on live traffic without affecting "
                "anyone."
            )

        base = (
            f"Would have matched {self.matched:,} of {self.considered:,} transactions "
            f"({self.match_rate:.1%})."
        )
        if self.confidence is Confidence.PARTIAL:
            return (
                f"{base} {self.skipped:,} more could not be checked, because the facts "
                "this rule needs were not recorded at the time."
            )
        return base


def required_facts(expression: str) -> set[str]:
    """Every name the rule reads. Used to decide whether history can answer it."""
    tree = parse(expression)
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def _context(decision, functions: dict[str, Any] | None = None) -> EvaluationContext:
    facts = dict(decision.resolved_facts or {})
    # Lists came back from JSON; the interpreter wants tuples so `in` works the
    # way it did at decision time.
    for key, value in facts.items():
        if isinstance(value, list):
            facts[key] = tuple(value)
    return EvaluationContext(facts=facts, functions=functions or {})


def backtest(
    expression: str,
    decisions: Iterable[Any],
    *,
    sample_limit: int = 20,
    functions: dict[str, Any] | None = None,
) -> Impact:
    """Evaluate a candidate rule against stored decisions.

    Takes an iterable rather than a queryset so the caller decides where the rows
    come from — which in practice is the replica, and in tests is a list.
    """
    tree = parse(expression)
    needed = required_facts(expression)

    impact = Impact(confidence=Confidence.EXACT)

    for decision in decisions:
        available = set((decision.resolved_facts or {}).keys())
        missing = needed - available

        if missing:
            impact.skipped += 1
            impact.missing_facts |= missing
            continue

        impact.considered += 1
        try:
            matched = evaluate(tree, _context(decision, functions))
        except RuleEvaluationError:
            # A rule that cannot evaluate against a stored decision is not a
            # match and not a silent zero. Counted, so a backtest reporting
            # "matched 0" and one reporting "errored 4,000" are distinguishable.
            impact.errored += 1
            continue

        if matched:
            impact.matched += 1
            if len(impact.samples) < sample_limit:
                impact.samples.append(
                    Sample(
                        decision_id=decision.id,
                        transaction_ref=decision.transaction_ref,
                        amount_minor=decision.amount_minor,
                        currency=decision.currency,
                        decided_at=str(decision.decided_at),
                        original_outcome=decision.outcome,
                    )
                )

    if impact.considered == 0 and impact.skipped:
        impact.confidence = Confidence.UNAVAILABLE
    elif impact.skipped:
        impact.confidence = Confidence.PARTIAL

    return impact


def replay_decision(decision, ruleset, functions: dict[str, Any] | None = None):
    """Re-decide a stored decision against a rule set.

    Against its *own* version this must reproduce the original exactly — that is
    §3.4's whole claim, and the reason the resolved facts were stored. Against a
    different version it answers "what would this rule set have done", which is
    what §11.6's runbook needs after a version skew: every divergence is a
    transaction that needs review.
    """
    from complylayer.engine import decide

    return decide(ruleset, _context(decision, functions))


@dataclass(frozen=True)
class Divergence:
    """Where a shadow rule disagreed with the live rules.

    Shadow mode's value is entirely in this report. A rule that has agreed with
    the live set for a week is a rule an officer can activate without holding
    their breath, and the count of times it would have blocked something is the
    number they actually want.
    """

    decisions: int
    shadow_matches: int
    would_have_blocked: int
    samples: tuple[Sample, ...] = ()

    @property
    def agrees_always(self) -> bool:
        return self.shadow_matches == 0

    @property
    def sentence(self) -> str:
        if self.agrees_always:
            return (
                f"Over {self.decisions:,} decisions this rule would not have changed "
                "a single outcome."
            )
        return (
            f"Over {self.decisions:,} decisions this rule would have fired "
            f"{self.shadow_matches:,} times, blocking {self.would_have_blocked:,} "
            "transactions that were allowed."
        )


def divergence(decisions: Iterable[Any], rule_id: str, *, sample_limit: int = 20) -> Divergence:
    """Read shadow results out of stored decisions.

    Nothing is re-evaluated: shadow rules were already evaluated at decision
    time and recorded in `shadow_matches`. Re-running them would answer a
    different question — what they would do *now* rather than what they did.
    """
    total = fired = blocked = 0
    samples: list[Sample] = []

    for decision in decisions:
        total += 1
        if rule_id not in (decision.shadow_matches or []):
            continue

        fired += 1
        if decision.outcome != "block":
            blocked += 1
            if len(samples) < sample_limit:
                samples.append(
                    Sample(
                        decision_id=decision.id,
                        transaction_ref=decision.transaction_ref,
                        amount_minor=decision.amount_minor,
                        currency=decision.currency,
                        decided_at=str(decision.decided_at),
                        original_outcome=decision.outcome,
                    )
                )

    return Divergence(
        decisions=total,
        shadow_matches=fired,
        would_have_blocked=blocked,
        samples=tuple(samples),
    )
