"""§3.4's reproducibility requirement, stated precisely enough to test.

The specification asks for byte-identical output. That is not achievable and
should not be attempted: the response carries a generated `decision_id`, the
instant it was decided, and a measured latency, none of which is deterministic.
A test asserting byte equality would either fail always or quietly exclude those
three — and a determinism test with a silent exclusion is worse than no test,
because it reports a guarantee nobody is checking.

So the requirement is restated field by field (D11), and the exclusions are
named here in the open where a reviewer can argue with them.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime

import pytest

from complylayer.api.handler import DecisionHandler, public_body
from complylayer.api.store import InMemoryStore
from complylayer.api.validation import Transaction
from complylayer.dsl import validate_source
from complylayer.engine import CompiledRule, RuleSet, Severity, State

# What must be identical for the same input and rule set version.
REPRODUCIBLE_FIELDS = ("outcome", "matched_rules", "reason", "ruleset_version", "degraded")

# What is allowed to differ, and why. Every entry here is a claim that this
# field carries no compliance meaning.
#
#   decision_id  a fresh identifier per decision, by design
#   decided_at   when it was decided, not what was decided
#   latency_ms   a measurement of this machine at this moment
EXCLUDED_FIELDS = ("decision_id", "decided_at", "latency_ms")


def ruleset(version: int = 47) -> RuleSet:
    return RuleSet(
        version,
        (
            CompiledRule(
                "rul_kyc_t2",
                "Tier 2 single transaction limit",
                validate_source("amount_minor > 5_000_000"),
                Severity.BLOCK,
                priority=10,
                regulatory_reference="CBN KYC Tier 2",
                customer_message="This transfer is above your tier limit.",
            ),
            CompiledRule(
                "rul_new_benef",
                "New beneficiary, large transfer",
                validate_source("amount_minor > 1_000_000 and destination_is_new_beneficiary"),
                Severity.FLAG,
                priority=20,
            ),
            CompiledRule(
                "rul_corridor",
                "High-risk corridor",
                validate_source("destination_country in high_risk_countries"),
                Severity.FLAG,
                priority=5,
            ),
            CompiledRule(
                "rul_shadow",
                "Shadow: tighter tier 2 cap",
                validate_source("amount_minor > 2_000_000"),
                Severity.BLOCK,
                state=State.SHADOW,
                priority=30,
            ),
        ),
    )


def transaction(amount: int = 7_500_000) -> Transaction:
    return Transaction(
        transaction_ref="TXN-2026-08-16-8842",
        customer_ref="usr_9931",
        amount_minor=amount,
        currency="NGN",
        transaction_type="transfer",
        channel="mobile",
        customer={"kyc_tier": 2, "account_created_at": "2026-07-30T10:00:00Z", "country": "NG"},
        destination={"country": "XX", "bank_code": "058", "is_new_beneficiary": True},
        device={"id": "dev_a83f", "ip_country": "NG"},
    )


def handler(store=None) -> DecisionHandler:
    return DecisionHandler(
        tenant_id="tnt_test",
        ruleset=ruleset(),
        store=store or InMemoryStore(lists={"high_risk_countries": ["XX", "YY"]}),
    )


def reproducible(body: dict) -> dict:
    return {key: body[key] for key in REPRODUCIBLE_FIELDS}


class TestTheSameInputGivesTheSameAnswer:
    def test_across_a_thousand_evaluations(self):
        first = handler().decide(transaction(), "key-1")
        for _ in range(1000):
            again = handler().decide(transaction(), "key-1")
            assert reproducible(again) == reproducible(first)

    def test_across_two_separate_processes(self):
        """One process can be deterministic through shared state that happens to
        persist. Two cannot."""
        script = (
            "import json, os, django;"
            "os.environ.setdefault('DJANGO_SETTINGS_MODULE','server.settings');"
            "django.setup();"
            "import sys; sys.path.insert(0, 'tests');"
            "from test_determinism import handler, transaction, reproducible;"
            "print(json.dumps(reproducible(handler().decide(transaction(), 'k'))))"
        )
        outputs = set()
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, check=True
            )
            outputs.add(result.stdout.strip())
        assert len(outputs) == 1, outputs

        local = reproducible(handler().decide(transaction(), "k"))
        import json

        assert json.loads(outputs.pop()) == json.loads(json.dumps(local))

    def test_the_shadow_result_is_reproducible_too(self):
        first = handler().decide(transaction(), "k")
        for _ in range(100):
            assert (
                handler().decide(transaction(), "k")["_shadow_matches"] == first["_shadow_matches"]
            )


class TestOrdering:
    def test_matched_rules_are_ordered_by_priority_then_id(self):
        body = handler().decide(transaction(), "k")
        assert [rule["id"] for rule in body["matched_rules"]] == [
            "rul_corridor",  # priority 5
            "rul_kyc_t2",  # priority 10
            "rul_new_benef",  # priority 20
        ]

    def test_the_order_does_not_depend_on_how_the_ruleset_was_built(self):
        forwards = RuleSet(47, ruleset().rules)
        backwards = RuleSet(47, tuple(reversed(ruleset().rules)))
        assert [r.id for r in forwards.rules] == [r.id for r in backwards.rules]


class TestWhatIsAllowedToDiffer:
    def test_the_excluded_fields_are_named_rather_than_silently_dropped(self):
        """The point of this test is the list itself.

        If someone adds a field to the response that is not reproducible, this
        forces the choice to be explicit: either it belongs in EXCLUDED_FIELDS
        with a stated reason, or the response is no longer reproducible.
        """
        body = public_body(handler().decide(transaction(), "k"))
        accounted = set(REPRODUCIBLE_FIELDS) | set(EXCLUDED_FIELDS)
        unaccounted = set(body) - accounted - {"evaluated_rules", "customer_message"}
        assert not unaccounted, f"new response fields need a determinism decision: {unaccounted}"

    def test_decision_ids_are_unique_per_decision(self):
        ids = {handler().decide(transaction(), "k")["decision_id"] for _ in range(50)}
        assert len(ids) == 50

    def test_latency_is_quantised_to_five_milliseconds(self):
        from complylayer.api.decision import _quantise

        assert _quantise(18.3) == 20
        assert _quantise(2.1) == 0
        assert _quantise(97.0) == 95


class TestDifferentInputsGiveDifferentAnswers:
    """The mirror property. A function returning a constant would pass everything above."""

    @pytest.mark.parametrize(
        "amount,expected",
        [(500_000, "allow"), (1_500_000, "flag"), (7_500_000, "block")],
    )
    def test_the_amount_changes_the_outcome(self, amount: int, expected: str):
        """Destination is deliberately a low-risk country here.

        With the default transaction the corridor rule fires on destination
        alone, so every amount flags and the test would prove nothing about the
        amount. Isolating one variable is the whole point of the case.
        """
        txn = transaction(amount)
        low_risk = Transaction(
            transaction_ref=txn.transaction_ref,
            customer_ref=txn.customer_ref,
            amount_minor=txn.amount_minor,
            currency=txn.currency,
            transaction_type=txn.transaction_type,
            channel=txn.channel,
            customer=txn.customer,
            destination={**txn.destination, "country": "NG"},
            device=txn.device,
        )
        assert handler().decide(low_risk, "k")["outcome"] == expected

    def test_a_different_ruleset_version_is_recorded(self):
        handler_a = DecisionHandler("tnt", ruleset(47), InMemoryStore())
        handler_b = DecisionHandler("tnt", ruleset(48), InMemoryStore())
        assert handler_a.decide(transaction(), "k")["ruleset_version"] == 47
        assert handler_b.decide(transaction(), "k")["ruleset_version"] == 48


class TestResolvedFactsAreStored:
    """D11: the input alone cannot be replayed once Redis has rolled forward."""

    def test_the_resolved_facts_accompany_the_decision(self):
        body = handler().decide(transaction(), "k")
        facts = body["_resolved_facts"]
        assert facts["amount_minor"] == 7_500_000
        assert facts["kyc_tier"] == 2
        assert facts["destination_country"] == "XX"

    def test_named_lists_are_resolved_from_the_snapshot(self):
        """Not from live configuration — editing a list must publish a version."""
        body = handler().decide(transaction(), "k")
        assert body["_resolved_facts"]["high_risk_countries"] == ["XX", "YY"]

    def test_resolved_facts_survive_json(self):
        import json

        body = handler().decide(transaction(), "k")
        assert json.loads(json.dumps(body["_resolved_facts"])) == body["_resolved_facts"]


class TestClockUse:
    def test_one_decision_reads_the_clock_once(self):
        """Two `days_since` calls in one rule must agree, and a replay has to
        reproduce against the instant the decision was originally made."""
        rules = RuleSet(
            1,
            (
                CompiledRule(
                    "r",
                    "same hour twice",
                    validate_source("hour_of_day() == hour_of_day()"),
                    Severity.FLAG,
                ),
            ),
        )
        body = DecisionHandler("tnt", rules, InMemoryStore()).decide(transaction(), "k")
        assert body["outcome"] == "flag"

    def test_decided_at_is_timezone_aware(self):
        body = handler().decide(transaction(), "k")
        moment = datetime.fromisoformat(body["decided_at"])
        assert moment.tzinfo is not None
        assert moment.utcoffset() == UTC.utcoffset(None)
