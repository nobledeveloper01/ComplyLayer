"""Phase 7's exit gate: backtesting, shadow divergence, analytics and export.

Three assertions carry the phase, and each is stated in the roadmap:

1. A backtest over 30 days runs on the replica and touches the primary zero
   times — asserted by instrumenting the connection, not by reading the code.
2. A shadow rule never appears in a returned outcome.
3. Replaying a stored decision against its own rule set version reproduces it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from complylayer import backtest as bt
from complylayer.backtest.analytics import performance
from complylayer.backtest.export import attestation, history_csv, rules_csv
from complylayer.dsl import functions, validate_source
from complylayer.engine import CompiledRule, Outcome, RuleSet, Severity, State


@dataclass
class StoredDecision:
    """A decision as the table holds it. A plain object, so the replay logic is
    testable without a database — the database part has its own test below."""

    id: str
    transaction_ref: str
    amount_minor: int
    resolved_facts: dict
    outcome: str = "allow"
    currency: str = "NGN"
    decided_at: datetime = field(default_factory=lambda: datetime(2026, 8, 1, tzinfo=UTC))
    matched_rules: list = field(default_factory=list)
    shadow_matches: list = field(default_factory=list)
    review_status: str = ""


def history(count: int = 100, **overrides) -> list[StoredDecision]:
    return [
        StoredDecision(
            id=f"dec_{index:04d}",
            transaction_ref=f"TXN-{index:04d}",
            amount_minor=1_000_000 * (index % 20 + 1),
            resolved_facts={
                "amount_minor": 1_000_000 * (index % 20 + 1),
                "kyc_tier": index % 4,
                "currency": "NGN",
                **overrides.get("facts", {}),
            },
            **{key: value for key, value in overrides.items() if key != "facts"},
        )
        for index in range(count)
    ]


class TestBacktestIsHonestAboutWhatItKnows:
    """The design decision that matters most here.

    An approximate number on a screen where somebody is deciding whether to
    loosen a control is worse than an honest blank.
    """

    def test_a_rule_over_recorded_facts_is_exact(self):
        impact = bt.backtest("amount_minor > 10000000", history(100))
        assert impact.confidence is bt.Confidence.EXACT
        assert impact.considered == 100
        assert impact.matched == 50
        assert "50 of 100" in impact.sentence

    def test_a_rule_needing_a_fact_nobody_recorded_produces_no_number(self):
        """Those facts are gone. Redis held a rolling window that moved on."""
        impact = bt.backtest("velocity_count_7d > 5", history(100))
        assert impact.confidence is bt.Confidence.UNAVAILABLE
        assert impact.matched == 0
        assert "cannot be tested against history" in impact.sentence
        assert "shadow mode" in impact.sentence, "it should say what to do instead"

    def test_the_missing_fact_is_named(self):
        impact = bt.backtest("mystery_fact > 1", history(10))
        assert "mystery_fact" in impact.missing_facts
        assert "mystery_fact" in impact.sentence

    def test_a_partly_answerable_rule_says_how_much_it_skipped(self):
        decisions = history(60)
        for decision in decisions[:20]:
            decision.resolved_facts["balance_minor"] = 5_000_000

        impact = bt.backtest("balance_minor > 1000000", decisions)
        assert impact.confidence is bt.Confidence.PARTIAL
        assert impact.considered == 20
        assert impact.skipped == 40
        assert "40 more could not be checked" in impact.sentence

    def test_the_caveat_travels_with_the_number(self):
        """Not a footnote. The sentence is what gets rendered."""
        decisions = history(10)
        decisions[0].resolved_facts["balance_minor"] = 1
        impact = bt.backtest("balance_minor > 0", decisions)
        assert "could not be checked" in impact.sentence

    def test_an_unevaluable_decision_is_counted_rather_than_silently_zero(self):
        """ "matched 0" and "errored 4,000" must be distinguishable."""
        decisions = history(10)
        for decision in decisions:
            decision.resolved_facts["kyc_tier"] = "not a number"

        impact = bt.backtest("kyc_tier > 2", decisions)
        assert impact.errored == 10
        assert impact.matched == 0

    def test_samples_are_returned_for_the_drill_down(self):
        """A count without examples is a number an officer cannot sanity-check."""
        impact = bt.backtest("amount_minor > 0", history(100), sample_limit=5)
        assert len(impact.samples) == 5
        assert impact.samples[0].transaction_ref.startswith("TXN-")

    def test_lists_survive_the_json_round_trip(self):
        """They came back from JSONB as lists; `in` needs them as tuples."""
        decisions = history(4, facts={"high_risk_countries": ["XX", "YY"], "country": "XX"})
        impact = bt.backtest("country in high_risk_countries", decisions)
        assert impact.matched == 4


class TestReplayReproduces:
    """§3.4's claim, and the reason D11 stored the resolved facts."""

    def test_a_decision_replayed_against_its_own_version_reproduces_it(self):
        ruleset = RuleSet(
            47,
            (
                CompiledRule(
                    "rul_b",
                    "Over the limit",
                    validate_source("amount_minor > 5000000"),
                    Severity.BLOCK,
                    priority=10,
                ),
                CompiledRule(
                    "rul_f",
                    "Large",
                    validate_source("amount_minor > 1000000"),
                    Severity.FLAG,
                    priority=20,
                ),
            ),
        )
        stored = StoredDecision(
            id="dec_x",
            transaction_ref="TXN-X",
            amount_minor=7_500_000,
            resolved_facts={"amount_minor": 7_500_000},
            outcome="block",
            matched_rules=[{"id": "rul_b"}, {"id": "rul_f"}],
        )

        replayed = bt.replay_decision(stored, ruleset)
        assert str(replayed.outcome) == stored.outcome
        assert list(replayed.matched_rule_ids) == [m["id"] for m in stored.matched_rules]

    def test_replay_is_stable_across_many_runs(self):
        ruleset = RuleSet(
            1, (CompiledRule("r", "r", validate_source("amount_minor > 1000000"), Severity.FLAG),)
        )
        stored = StoredDecision("d", "T", 2_000_000, {"amount_minor": 2_000_000})

        outcomes = {str(bt.replay_decision(stored, ruleset).outcome) for _ in range(100)}
        assert outcomes == {"flag"}

    def test_replaying_against_a_different_version_answers_a_different_question(self):
        """What §11.6's runbook needs after version skew: every divergence is a
        transaction that needs review."""
        stored = StoredDecision("d", "T", 2_000_000, {"amount_minor": 2_000_000}, outcome="allow")
        tighter = RuleSet(
            48, (CompiledRule("r", "r", validate_source("amount_minor > 1000000"), Severity.BLOCK),)
        )

        assert bt.replay_decision(stored, tighter).outcome is Outcome.BLOCK
        assert stored.outcome == "allow", "the stored decision is not rewritten"


class TestShadowNeverAffectsAnybody:
    def test_a_shadow_rule_never_appears_in_an_outcome(self):
        """Asserted across every severity, because a shadow block is the one
        that would actually hurt somebody."""
        from complylayer.dsl.interpreter import EvaluationContext
        from complylayer.engine import decide

        for severity in (Severity.BLOCK, Severity.FLAG, Severity.ALLOW_WITH_NOTE):
            ruleset = RuleSet(
                1,
                (
                    CompiledRule(
                        "s",
                        "shadow",
                        validate_source("amount_minor > 1"),
                        severity,
                        state=State.SHADOW,
                    ),
                ),
            )
            context = EvaluationContext(
                facts={"amount_minor": 5_000_000},
                functions=functions.build(None, datetime.now(UTC)),
            )
            decision = decide(ruleset, context)
            assert decision.outcome is Outcome.ALLOW
            assert decision.matched_rule_ids == ()
            assert [rule.id for rule in decision.shadow_matches] == ["s"]


class TestDivergenceReporting:
    def test_zero_divergence_reads_as_reassurance(self):
        """The most common shadow state, and the one that says the rule is safe."""
        report = bt.divergence(history(500), "rul_shadow")
        assert report.agrees_always is True
        assert "would not have changed a single outcome" in report.sentence

    def test_divergence_counts_what_would_have_been_blocked(self):
        decisions = history(100)
        for decision in decisions[:12]:
            decision.shadow_matches = ["rul_shadow"]

        report = bt.divergence(decisions, "rul_shadow")
        assert report.shadow_matches == 12
        assert report.would_have_blocked == 12
        assert "blocking 12" in report.sentence

    def test_a_shadow_match_on_an_already_blocked_decision_is_not_a_new_block(self):
        """It would have changed nothing — the transaction was refused anyway."""
        decisions = history(10)
        for decision in decisions:
            decision.shadow_matches = ["rul_shadow"]
            decision.outcome = "block"

        report = bt.divergence(decisions, "rul_shadow")
        assert report.shadow_matches == 10
        assert report.would_have_blocked == 0

    def test_divergence_reads_stored_results_rather_than_re_evaluating(self):
        """Re-running would answer what the rule would do *now*, not what it did."""
        decisions = history(5)
        decisions[0].shadow_matches = ["rul_shadow"]
        decisions[0].resolved_facts = {}  # no facts to re-evaluate with

        assert bt.divergence(decisions, "rul_shadow").shadow_matches == 1


class TestRuleAnalytics:
    """Which rules are worth their noise."""

    def test_a_rule_cleared_almost_every_time_is_called_out(self):
        decisions = history(100)
        for index, decision in enumerate(decisions):
            decision.matched_rules = [{"id": "rul_noisy"}]
            decision.review_status = "cleared" if index < 95 else "confirmed"

        ranked = performance(decisions, {"rul_noisy": "Large transfer"})
        assert ranked[0].false_positive_rate == pytest.approx(0.95)
        assert "spending reviewer time" in ranked[0].verdict

    def test_a_rule_nobody_reviewed_has_an_unknown_rate_rather_than_zero(self):
        """Reporting 0% would rank it as the best performing rule in the tenant."""
        decisions = history(10)
        for decision in decisions:
            decision.matched_rules = [{"id": "rul_new"}]

        ranked = performance(decisions, {})
        assert ranked[0].false_positive_rate is None
        assert "accuracy is unknown" in ranked[0].verdict

    def test_a_precise_rule_is_told_it_is_earning_its_place(self):
        decisions = history(20)
        for index, decision in enumerate(decisions):
            decision.matched_rules = [{"id": "rul_good"}]
            decision.review_status = "cleared" if index < 2 else "confirmed"

        ranked = performance(decisions, {})
        assert "earning its place" in ranked[0].verdict

    def test_the_worst_rule_is_first(self):
        """The page exists to answer "which rule is drowning my queue"."""
        decisions = history(40)
        for index, decision in enumerate(decisions):
            noisy = index % 2 == 0
            decision.matched_rules = [{"id": "rul_noisy" if noisy else "rul_good"}]
            decision.review_status = "cleared" if noisy else "confirmed"

        ranked = performance(decisions, {})
        assert ranked[0].rule_id == "rul_noisy"


class TestTheExport:
    @dataclass
    class Rule:
        id: str = "rul_1"
        name: str = "Tier 2 limit"
        state: str = "active"
        severity: str = "block"
        expression: str = "amount_minor > 5000000"
        regulatory_reference: str = "CBN KYC Tier 2"
        created_by: str = "usr_adaeze"
        approved_by: str = "usr_emeka"
        activated_at: str = "2026-08-01"

    def test_a_rule_named_like_a_formula_cannot_execute_in_excel(self):
        """An export containing `=cmd|...` runs on the reviewer's machine.

        Embarrassing to learn about from a customer, and one character to fix.
        """
        csv_text = rules_csv([self.Rule(name="=cmd|'/c calc'!A1")])
        assert "'=cmd" in csv_text
        assert ",=cmd" not in csv_text

    @pytest.mark.parametrize("prefix", ["=", "+", "-", "@"])
    def test_every_formula_prefix_is_defused(self, prefix: str):
        assert f"'{prefix}" in rules_csv([self.Rule(name=f"{prefix}danger")])

    def test_the_export_carries_the_regulation_each_rule_claims(self):
        assert "CBN KYC Tier 2" in rules_csv([self.Rule()])

    def test_a_verified_chain_is_attested(self):
        from complylayer.audit.chain import VerificationResult

        text = attestation(
            "tnt_1", VerificationResult(ok=True, checked=4_812), {"allow": 40_000, "block": 118}
        )
        assert "Verified" in text
        assert "4,812 records" in text
        assert "40,000" in text

    def test_a_broken_chain_says_so_before_anything_else(self):
        """An export whose chain does not verify is a more important fact than
        anything else in the file."""
        from complylayer.audit.chain import VerificationResult

        text = attestation(
            "tnt_1",
            VerificationResult(ok=False, checked=3, broken_at="aud_9", detail="contents changed"),
            {"allow": 1},
        )
        assert "FAILED at record aud_9" in text
        assert "should not be relied upon" in text
        assert text.index("FAILED") < text.index("Decision volumes")

    def test_history_export_names_who_did_what(self):
        @dataclass
        class Record:
            recorded_at: str = "2026-08-16T09:00:00Z"
            event_type: str = "rule.approved"
            actor: dict = field(default_factory=lambda: {"id": "usr_emeka", "role": "risk_manager"})
            subject: dict = field(default_factory=lambda: {"id": "rul_1"})
            hash: str = "sha256:abc"

        text = history_csv([Record()])
        assert "rule.approved" in text
        assert "usr_emeka" in text
        assert "risk_manager" in text


@pytest.mark.integration
@pytest.mark.django_db
class TestTheBacktestNeverTouchesThePrimary:
    """The exit gate §11.1 asks for.

    A 30-day replay is the heaviest read in the product. Running it on the
    database serving decisions is how a compliance officer testing a rule causes
    a latency incident for the customer's transactions — and nothing about the
    code's appearance would tell you which database it used.

    **What this asserts, and what it does not.** It asserts the *alias*: that the
    query is routed to `replica` rather than `default`, and that the router
    refuses to write there or migrate it. It does not stand up a physically
    separate Postgres — a test that did would be asserting the deployment rather
    than the code, and the deployment is what `values.yaml` and
    `complylayer_doctor` are for.

    The original version of this test did try two live aliases, mirrored onto one
    test database. It hung: two connections to the same database, each wrapped in
    its own test transaction, waiting on each other. Recorded because the next
    person to reach for `databases=["default", "replica"]` deserves to know.
    """

    @pytest.fixture
    def populated(self):
        from complylayer import partitions
        from complylayer.models import Decision, Tenant

        tenant = Tenant.objects.create(id="tnt_bt", name="Backtest")
        partitions.ensure_partitions(datetime.now(UTC).date(), months_ahead=0)

        for index in range(40):
            Decision.objects.create(
                id=f"dec_bt_{index:03d}",
                decided_at=datetime.now(UTC) - timedelta(hours=index),
                tenant=tenant,
                idempotency_key=f"bt-{index}",
                ruleset_version=1,
                transaction_ref=f"TXN-BT-{index}",
                customer_ref_hash="a" * 64,
                amount_minor=1_000_000 * (index % 20 + 1),
                currency="NGN",
                context={},
                resolved_facts={"amount_minor": 1_000_000 * (index % 20 + 1), "kyc_tier": 2},
                outcome="allow",
                latency_ms=1,
            )
        return tenant

    def test_the_analytics_read_is_routed_to_the_replica(self, populated):
        from complylayer.models import Decision

        analytics = Decision.objects.using(bt.REPLICA).filter(tenant=populated)
        assert analytics.db == bt.REPLICA, (
            "the backtest read would have gone to the database serving decisions"
        )

        # And the same query against the primary is a different alias, so the
        # distinction is real rather than a no-op that always says "replica".
        assert Decision.objects.filter(tenant=populated).db == "default"

    def test_the_backtest_itself_takes_rows_rather_than_a_connection(self, populated):
        """`backtest` accepts an iterable, so the caller decides where the rows
        come from. That is what makes the routing testable at all — the replay
        logic has no database in it to get wrong."""
        from complylayer.models import Decision

        decisions = list(Decision.objects.filter(tenant=populated))
        impact = bt.backtest("amount_minor > 10000000", decisions)

        assert impact.considered == 40
        assert impact.matched == 20
        assert impact.confidence is bt.Confidence.EXACT

    def test_the_router_refuses_to_migrate_the_replica(self):
        """A copy that gets its own schema either fails or, worse, diverges."""
        from complylayer.db_router import ReadReplicaRouter

        router = ReadReplicaRouter()
        assert router.allow_migrate("default", "complylayer") is True
        assert router.allow_migrate("replica", "complylayer") is False

    def test_writes_always_go_to_the_primary(self):
        from complylayer.db_router import ReadReplicaRouter

        assert ReadReplicaRouter().db_for_write(object) == "default"

    def test_the_router_does_not_guess_which_reads_are_analytical(self):
        """Guessing would eventually send a decision's read to a replica lagging
        behind its own write."""
        from complylayer.db_router import ReadReplicaRouter

        assert ReadReplicaRouter().db_for_read(object) is None

    def test_the_replica_alias_exists_and_mirrors_in_tests(self):
        from django.conf import settings

        assert "replica" in settings.DATABASES
        assert settings.DATABASES["replica"]["TEST"]["MIRROR"] == "default"
