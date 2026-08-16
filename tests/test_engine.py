"""Many rules, one decision.

The interesting cases are not "does a block rule block". They are what happens
when rules disagree, when one cannot run at all, and when the same input is
evaluated twice — because those are the ones a compliance officer or a regulator
ends up asking about.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from complylayer.dsl import functions, validate_source
from complylayer.dsl.interpreter import EvaluationContext
from complylayer.engine import CompiledRule, Outcome, RuleSet, Severity, State, decide

NOW = datetime(2026, 8, 16, 14, 30, tzinfo=UTC)


def rule(
    rule_id: str,
    source: str,
    severity: Severity = Severity.FLAG,
    *,
    state: State = State.ACTIVE,
    priority: int = 0,
    name: str = "",
    customer_message: str = "",
) -> CompiledRule:
    return CompiledRule(
        id=rule_id,
        name=name or rule_id,
        tree=validate_source(source),
        severity=severity,
        state=state,
        priority=priority,
        customer_message=customer_message,
    )


def context(**facts) -> EvaluationContext:
    return EvaluationContext(facts=facts, functions=functions.build(None, NOW))


class TestCombiningOutcomes:
    def test_no_match_allows(self):
        ruleset = RuleSet(1, (rule("a", "amount_minor > 1_000_000"),))
        assert decide(ruleset, context(amount_minor=500)).outcome is Outcome.ALLOW

    def test_the_most_severe_match_wins(self):
        """Not the first, not the highest priority. Severity is the only
        defensible tie-break when a transaction trips both."""
        ruleset = RuleSet(
            1,
            (
                rule("flagger", "amount_minor > 100", Severity.FLAG, priority=1),
                rule("blocker", "amount_minor > 200", Severity.BLOCK, priority=99),
            ),
        )
        decision = decide(ruleset, context(amount_minor=500))
        assert decision.outcome is Outcome.BLOCK
        assert decision.matched_rule_ids == ("flagger", "blocker")

    def test_allow_with_note_matches_without_changing_the_outcome(self):
        ruleset = RuleSet(1, (rule("noted", "amount_minor > 1", Severity.ALLOW_WITH_NOTE),))
        decision = decide(ruleset, context(amount_minor=5))
        assert decision.outcome is Outcome.ALLOW
        assert decision.matched_rule_ids == ("noted",)

    def test_the_reason_names_the_rule_that_decided(self):
        ruleset = RuleSet(
            1,
            (
                rule("f", "amount_minor > 1", Severity.FLAG, name="Large transfer"),
                rule("b", "amount_minor > 2", Severity.BLOCK, name="Over tier limit"),
            ),
        )
        assert decide(ruleset, context(amount_minor=5)).reason == "Over tier limit"

    def test_only_a_block_carries_a_customer_message(self):
        blocking = RuleSet(
            1, (rule("b", "amount_minor > 1", Severity.BLOCK, customer_message="Nope."),)
        )
        assert decide(blocking, context(amount_minor=5)).customer_message == "Nope."

        flagging = RuleSet(
            1, (rule("f", "amount_minor > 1", Severity.FLAG, customer_message="Nope."),)
        )
        assert decide(flagging, context(amount_minor=5)).customer_message == ""


class TestEveryRuleIsEvaluated:
    def test_evaluation_does_not_stop_at_the_first_block(self):
        """Per-rule analytics need a fire count for every rule, and a reviewer
        needs the whole picture rather than the first thing that tripped."""
        ruleset = RuleSet(
            1,
            (
                rule("b", "amount_minor > 1", Severity.BLOCK, priority=1),
                rule("f1", "amount_minor > 2", Severity.FLAG, priority=2),
                rule("f2", "amount_minor > 3", Severity.FLAG, priority=3),
            ),
        )
        decision = decide(ruleset, context(amount_minor=100))
        assert decision.evaluated_rules == 3
        assert decision.matched_rule_ids == ("b", "f1", "f2")

    def test_matched_rules_are_ordered_by_priority_then_id(self):
        ruleset = RuleSet(
            1,
            (
                rule("zzz", "amount_minor > 1", priority=1),
                rule("aaa", "amount_minor > 1", priority=1),
                rule("mmm", "amount_minor > 1", priority=0),
            ),
        )
        decision = decide(ruleset, context(amount_minor=5))
        assert decision.matched_rule_ids == ("mmm", "aaa", "zzz")

    def test_the_ruleset_itself_is_stored_in_evaluation_order(self):
        """Order is fixed when the snapshot is built, not per request, so it is
        constant for a given version — which is what the timing note asks for."""
        ruleset = RuleSet(
            1,
            (
                rule("b", "amount_minor > 1", priority=5),
                rule("a", "amount_minor > 1", priority=1),
            ),
        )
        assert [r.id for r in ruleset.rules] == ["a", "b"]


class TestShadowRules:
    def test_a_shadow_match_never_changes_the_outcome(self):
        ruleset = RuleSet(
            1,
            (rule("s", "amount_minor > 1", Severity.BLOCK, state=State.SHADOW),),
        )
        decision = decide(ruleset, context(amount_minor=5))
        assert decision.outcome is Outcome.ALLOW
        assert decision.matched_rule_ids == ()
        assert [r.id for r in decision.shadow_matches] == ["s"]

    def test_shadow_rules_are_still_counted_as_evaluated(self):
        ruleset = RuleSet(
            1,
            (
                rule("live", "amount_minor > 1"),
                rule("shadow", "amount_minor > 1", state=State.SHADOW),
            ),
        )
        assert decide(ruleset, context(amount_minor=5)).evaluated_rules == 2

    def test_draft_and_archived_rules_are_not_evaluated_at_all(self):
        ruleset = RuleSet(
            1,
            (
                rule("draft", "amount_minor > 1", state=State.DRAFT),
                rule("archived", "amount_minor > 1", state=State.ARCHIVED),
            ),
        )
        decision = decide(ruleset, context(amount_minor=5))
        assert decision.evaluated_rules == 0
        assert decision.outcome is Outcome.ALLOW


class TestFallbackWhenARuleCannotRun:
    """§10.3. The failure mode is a product decision, made per severity."""

    def test_a_block_rule_that_cannot_evaluate_fails_closed(self):
        """The control did not run. Letting the transaction through would be a
        compliance breach, and "our vendor was down" is not a defence."""
        ruleset = RuleSet(1, (rule("b", "missing_fact > 1", Severity.BLOCK),))
        decision = decide(ruleset, context(amount_minor=5))
        assert decision.outcome is Outcome.BLOCK
        assert decision.degraded is True
        assert decision.matched_rule_ids == ("b",)

    def test_a_flag_rule_that_cannot_evaluate_fails_open(self):
        """Flagging exists for human review. Halting every transaction because
        the review system is unavailable is disproportionate."""
        ruleset = RuleSet(1, (rule("f", "missing_fact > 1", Severity.FLAG),))
        decision = decide(ruleset, context(amount_minor=5))
        assert decision.outcome is Outcome.ALLOW
        assert decision.degraded is True
        assert decision.matched_rule_ids == ()

    def test_the_failure_is_always_recorded_even_when_it_fails_open(self):
        """Otherwise "just take ComplyLayer down" becomes a way to move money
        past the controls, and nothing anywhere would report it."""
        ruleset = RuleSet(1, (rule("f", "missing_fact > 1", Severity.FLAG),))
        decision = decide(ruleset, context())
        assert len(decision.errored_rules) == 1
        assert "missing_fact" in decision.errored_rules[0].error

    def test_a_tenant_can_override_the_default(self):
        ruleset = RuleSet(1, (rule("b", "missing_fact > 1", Severity.BLOCK),))
        decision = decide(ruleset, context(), fallback={Severity.BLOCK: "open"})
        assert decision.outcome is Outcome.ALLOW
        assert decision.degraded is True, "an override changes the outcome, never the recording"

    def test_one_broken_rule_does_not_take_the_others_down(self):
        ruleset = RuleSet(
            1,
            (
                rule("broken", "missing_fact > 1", Severity.FLAG, priority=1),
                rule("working", "amount_minor > 1", Severity.FLAG, priority=2),
            ),
        )
        decision = decide(ruleset, context(amount_minor=5))
        assert decision.matched_rule_ids == ("working",)
        assert decision.evaluated_rules == 2
        assert decision.degraded is True

    def test_a_shadow_rule_failing_degrades_but_cannot_block(self):
        ruleset = RuleSet(1, (rule("s", "missing_fact > 1", Severity.BLOCK, state=State.SHADOW),))
        decision = decide(ruleset, context())
        assert decision.outcome is Outcome.ALLOW
        assert decision.degraded is True

    def test_a_healthy_decision_is_not_degraded(self):
        ruleset = RuleSet(1, (rule("a", "amount_minor > 1"),))
        assert decide(ruleset, context(amount_minor=5)).degraded is False


class TestStepAccounting:
    def test_steps_are_measured_not_counted(self):
        """A rule count would be a plausible-looking lie. Per-rule evaluation
        cost is what §11.4's slow-rule alert is built on."""
        one = decide(RuleSet(1, (rule("a", "amount_minor > 1"),)), context(amount_minor=5))
        three = decide(
            RuleSet(
                1,
                (
                    rule("a", "amount_minor > 1"),
                    rule("b", "amount_minor > 1 and kyc_tier < 3"),
                    rule("c", "amount_minor + 1 > 2"),
                ),
            ),
            context(amount_minor=5, kyc_tier=1),
        )
        assert one.steps > 1
        assert three.steps > one.steps


class TestDeterminism:
    def test_the_same_input_gives_the_same_answer_every_time(self):
        ruleset = RuleSet(
            47,
            (
                rule("b", "amount_minor > 5_000_000", Severity.BLOCK, priority=10),
                rule("f", "amount_minor > 1_000_000", Severity.FLAG, priority=20),
                rule("s", "amount_minor > 500_000", state=State.SHADOW, priority=30),
            ),
        )
        first = decide(ruleset, context(amount_minor=7_500_000))
        for _ in range(200):
            again = decide(ruleset, context(amount_minor=7_500_000))
            assert again.outcome is first.outcome
            assert again.matched_rule_ids == first.matched_rule_ids
            assert [r.id for r in again.shadow_matches] == [r.id for r in first.shadow_matches]
            assert again.reason == first.reason

    @pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0), (1, 0, 2)])
    def test_the_answer_does_not_depend_on_how_the_ruleset_was_assembled(self, order):
        rules = [
            rule("b", "amount_minor > 5_000_000", Severity.BLOCK, priority=10),
            rule("f", "amount_minor > 1_000_000", Severity.FLAG, priority=20),
            rule("n", "amount_minor > 100", Severity.ALLOW_WITH_NOTE, priority=5),
        ]
        ruleset = RuleSet(1, tuple(rules[i] for i in order))
        decision = decide(ruleset, context(amount_minor=7_500_000))
        assert decision.outcome is Outcome.BLOCK
        assert decision.matched_rule_ids == ("n", "b", "f")
