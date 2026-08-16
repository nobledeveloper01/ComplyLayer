"""Turning a rule set and a transaction into one decision.

The interpreter answers "did this rule match?". This module answers the question
the customer actually asked: allow, flag or block — and why.

Three things here are worth more than the code that implements them.

**Every rule is evaluated, not just enough of them.** Stopping at the first
`block` would be faster and would lose the rest of the picture: a compliance
officer reviewing a blocked transaction needs to know it also tripped two flag
rules, and per-rule analytics need a fire count for every rule rather than for
whichever ones happened to sort first. §11.2's `rule_match_total` is meaningless
if evaluation short-circuits. Evaluation order is also constant per rule set
version, which is what §10's timing note asks for.

**A rule that cannot evaluate is not a rule that did not match.** A missing fact
or a Redis outage means the control did not run, and the tenant's configured
fallback for that severity decides what happens — fail-closed for `block`,
fail-open for `flag` (§10.3). Every such decision is marked degraded, counted,
and alertable, because §11.4 treats a sustained degraded rate as an incident
rather than a blip: otherwise "just take ComplyLayer down" becomes a way to move
money past the controls.

**Shadow rules never touch the outcome.** They are evaluated and recorded so a
compliance officer can watch a rule in production before trusting it, and the
divergence between shadow and live is the report that makes activation a
decision rather than a leap.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from complylayer.dsl import limits
from complylayer.dsl.errors import RuleEvaluationError
from complylayer.dsl.interpreter import EvaluationContext, Interpreter


class Severity(StrEnum):
    BLOCK = "block"
    FLAG = "flag"
    ALLOW_WITH_NOTE = "allow_with_note"


class Outcome(StrEnum):
    ALLOW = "allow"
    FLAG = "flag"
    BLOCK = "block"


class State(StrEnum):
    DRAFT = "draft"
    SHADOW = "shadow"
    ACTIVE = "active"
    ARCHIVED = "archived"


# Fail-closed for block, fail-open for flag. A blocked transaction is one that
# regulation says must not happen, and allowing it during an outage is a
# compliance breach — "our vendor was down" has never been a defence. Flagging
# exists for human review, so halting every transaction because the review system
# is unavailable is disproportionate to the risk.
DEFAULT_FALLBACK: Mapping[Severity, str] = {
    Severity.BLOCK: "closed",
    Severity.FLAG: "open",
    Severity.ALLOW_WITH_NOTE: "open",
}

_SEVERITY_OUTCOME: Mapping[Severity, Outcome] = {
    Severity.BLOCK: Outcome.BLOCK,
    Severity.FLAG: Outcome.FLAG,
    Severity.ALLOW_WITH_NOTE: Outcome.ALLOW,
}


@dataclass(frozen=True)
class CompiledRule:
    """A rule as the cache holds it: parsed once, evaluated many times."""

    id: str
    name: str
    tree: ast.Expression
    severity: Severity
    state: State = State.ACTIVE
    priority: int = 0
    regulatory_reference: str = ""
    customer_message: str = ""

    @property
    def sort_key(self) -> tuple[int, str]:
        """Deterministic, and stated rather than inherited from insertion order.

        `matched_rules` in the response is ordered by this. Without it the order
        is whatever the rule set happened to be built in, which is stable right
        up until it isn't — and the reproducibility requirement in §3.4 would be
        quietly false.
        """
        return (self.priority, self.id)


@dataclass(frozen=True)
class RuleOutcome:
    rule: CompiledRule
    matched: bool
    error: str = ""
    steps: int = 0

    @property
    def errored(self) -> bool:
        return bool(self.error)


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    matched_rules: tuple[CompiledRule, ...] = ()
    shadow_matches: tuple[CompiledRule, ...] = ()
    errored_rules: tuple[RuleOutcome, ...] = ()
    evaluated_rules: int = 0
    degraded: bool = False
    reason: str = ""
    customer_message: str = ""
    steps: int = 0

    @property
    def matched_rule_ids(self) -> tuple[str, ...]:
        return tuple(rule.id for rule in self.matched_rules)


@dataclass(frozen=True)
class RuleSet:
    """An immutable snapshot. Decisions reference the version, not the rules.

    That indirection is what makes a decision reproducible after the underlying
    rules have changed, which is the only way to answer "why was this allowed six
    months ago?" — the question that actually gets asked.
    """

    version: int
    rules: tuple[CompiledRule, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "rules", tuple(sorted(self.rules, key=lambda r: r.sort_key)))

    @property
    def active(self) -> tuple[CompiledRule, ...]:
        return tuple(rule for rule in self.rules if rule.state is State.ACTIVE)

    @property
    def shadow(self) -> tuple[CompiledRule, ...]:
        return tuple(rule for rule in self.rules if rule.state is State.SHADOW)


def decide(
    ruleset: RuleSet,
    context: EvaluationContext,
    fallback: Mapping[Severity, str] | None = None,
    max_steps: int = limits.MAX_STEPS,
) -> Decision:
    """Evaluate every active and shadow rule and combine them into one answer."""
    policy = {**DEFAULT_FALLBACK, **(fallback or {})}

    live = [_evaluate_one(rule, context, max_steps) for rule in ruleset.active]
    shadow = [_evaluate_one(rule, context, max_steps) for rule in ruleset.shadow]

    matched = tuple(result.rule for result in live if result.matched)
    errored = tuple(result for result in live + shadow if result.errored)

    # A live rule that could not evaluate is treated as though it matched when
    # its severity fails closed. The control did not run, so the safe reading is
    # the one that does not let the transaction through.
    failed_closed = tuple(
        result.rule
        for result in live
        if result.errored and policy.get(result.rule.severity) == "closed"
    )

    # Deduplicated by id rather than by object, because a CompiledRule holds an
    # AST and hashing one would rest on object identity — true today, and not a
    # property worth depending on.
    by_id = {rule.id: rule for rule in matched + failed_closed}
    deciding = tuple(sorted(by_id.values(), key=lambda rule: rule.sort_key))
    outcome = _combine(deciding)

    return Decision(
        outcome=outcome,
        matched_rules=deciding,
        shadow_matches=tuple(result.rule for result in shadow if result.matched),
        errored_rules=errored,
        evaluated_rules=len(live) + len(shadow),
        degraded=bool(errored),
        reason=_reason(deciding, outcome),
        customer_message=_customer_message(deciding, outcome),
        steps=sum(result.steps for result in live + shadow),
    )


def _evaluate_one(rule: CompiledRule, context: EvaluationContext, max_steps: int) -> RuleOutcome:
    interpreter = Interpreter(context, max_steps)
    try:
        matched = interpreter.run(rule.tree)
    except RuleEvaluationError as exc:
        # One rule failing must not take the decision down with it. The others
        # still ran, and their answers are still worth having.
        return RuleOutcome(rule=rule, matched=False, error=str(exc), steps=interpreter.steps)
    return RuleOutcome(rule=rule, matched=matched, steps=interpreter.steps)


def _combine(matched: Sequence[CompiledRule]) -> Outcome:
    """The most severe match wins. Nothing else is a defensible tie-break."""
    if any(rule.severity is Severity.BLOCK for rule in matched):
        return Outcome.BLOCK
    if any(rule.severity is Severity.FLAG for rule in matched):
        return Outcome.FLAG
    return Outcome.ALLOW


def _deciding_rule(matched: Sequence[CompiledRule], outcome: Outcome) -> CompiledRule | None:
    for rule in matched:
        if _SEVERITY_OUTCOME[rule.severity] is outcome:
            return rule
    return None


def _reason(matched: Sequence[CompiledRule], outcome: Outcome) -> str:
    rule = _deciding_rule(matched, outcome)
    return rule.name if rule else ""


def _customer_message(matched: Sequence[CompiledRule], outcome: Outcome) -> str:
    """Only a block carries one, and only the compliance team writes it.

    §7.1 puts this in the rule builder rather than in application code on
    purpose: the wording a customer sees about a refused transaction is a
    compliance decision, not an engineering one.
    """
    if outcome is not Outcome.BLOCK:
        return ""
    rule = _deciding_rule(matched, outcome)
    return rule.customer_message if rule else ""
