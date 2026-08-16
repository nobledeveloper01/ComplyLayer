"""The D1 benchmark: is the plain-Django decision path actually worth it?

D1 split the HTTP surface in two on the grounds that a DRF request cycle costs
more than the 5 ms the latency budget allows for validation and serialisation
combined. That was an argument, not a measurement, and the plan committed to
settling it here rather than leaving a second code path in the repository on the
strength of an assumption.

The comparison is deliberately narrow. Both paths do identical work — same
handler, same rule set, same store — so the only difference measured is the
framework overhead. If DRF lands inside the budget with headroom, the plain path
should be deleted and ADR-0002 updated to say so.

Marked `benchmark` rather than run by default: timing on a shared CI runner is
noise, and a flaky blocking gate is one somebody eventually comments out. Run it
with `make bench`.
"""

from __future__ import annotations

import statistics
import time

import orjson
import pytest
from django.test import RequestFactory

from complylayer.api.decision import decisions
from complylayer.api.handler import DecisionHandler
from complylayer.api.store import InMemoryStore
from complylayer.api.validation import parse_transaction
from complylayer.dsl import validate_source
from complylayer.engine import CompiledRule, RuleSet, Severity

pytestmark = pytest.mark.benchmark

ITERATIONS = 2000

# The budget line D1 is arguing about: 2 ms to read and validate the request
# plus 2 ms to serialise the response, with 1 ms for the key check between them.
FRAMEWORK_BUDGET_MS = 5.0

BODY = {
    "transaction_ref": "TXN-2026-08-16-8842",
    "customer_ref": "usr_9931",
    "amount_minor": 75_000_000,
    "currency": "NGN",
    "transaction_type": "transfer",
    "channel": "mobile",
    "customer": {"kyc_tier": 2, "account_created_at": "2026-07-30T10:00:00Z", "country": "NG"},
    "destination": {"country": "NG", "bank_code": "058", "is_new_beneficiary": True},
    "device": {"id": "dev_a83f", "ip_country": "NG"},
}


def realistic_ruleset(rule_count: int = 100) -> RuleSet:
    """100 active rules, which is what §11.9's load target assumes."""
    rules = []
    for index in range(rule_count):
        rules.append(
            CompiledRule(
                id=f"rul_{index:03d}",
                name=f"Rule {index}",
                tree=validate_source(f"amount_minor > {1_000_000 + index * 1000} and kyc_tier < 4"),
                severity=Severity.FLAG if index % 2 else Severity.BLOCK,
                priority=index,
            )
        )
    return RuleSet(47, tuple(rules))


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def measure(call, iterations: int = ITERATIONS) -> dict[str, float]:
    # Warm up: the first calls pay for import and JIT-ish caching, and reporting
    # those as steady state is the same mistake the doctor's Redis check made.
    for _ in range(100):
        call()

    timings = []
    for _ in range(iterations):
        started = time.perf_counter()
        call()
        timings.append((time.perf_counter() - started) * 1000)

    return {
        "p50": statistics.median(timings),
        "p99": percentile(timings, 0.99),
        "mean": statistics.fmean(timings),
    }


@pytest.fixture
def handler() -> DecisionHandler:
    return DecisionHandler("tnt_bench", realistic_ruleset(), InMemoryStore())


class TestWhereTheTimeGoes:
    def test_evaluation_alone(self, handler, capsys):
        """The floor. Everything else is overhead on top of this."""
        transaction = parse_transaction(BODY)
        stats = measure(lambda: handler.decide(transaction, "k"))
        with capsys.disabled():
            print(f"\n  evaluation only   p50 {stats['p50']:.3f} ms  p99 {stats['p99']:.3f} ms")
        assert stats["p99"] < 25.0, "100 rules should evaluate well inside the budget"

    def test_the_plain_django_path(self, handler, capsys):
        factory = RequestFactory()
        raw = orjson.dumps(BODY)

        counter = {"n": 0}

        def call():
            counter["n"] += 1
            request = factory.post(
                "/v1/decisions",
                data=raw,
                content_type="application/json",
                headers={"idempotency-key": f"bench-{counter['n']}"},
            )
            request.decision_handler = handler
            return decisions(request)

        stats = measure(call)
        with capsys.disabled():
            print(f"  plain Django      p50 {stats['p50']:.3f} ms  p99 {stats['p99']:.3f} ms")
        assert stats["p99"] < 25.0

    def test_validation_and_serialisation_in_isolation(self, handler, capsys):
        """The framework overhead D1 is actually arguing about, on its own."""
        raw = orjson.dumps(BODY)
        from complylayer.api import validation

        def call():
            payload = validation.parse_body(raw)
            transaction = validation.parse_transaction(payload)
            return orjson.dumps({"outcome": "allow", "ref": transaction.transaction_ref})

        stats = measure(call)
        with capsys.disabled():
            print(
                f"  validate + serialise  p50 {stats['p50'] * 1000:.1f} us  "
                f"p99 {stats['p99'] * 1000:.1f} us  (budget {FRAMEWORK_BUDGET_MS} ms)"
            )
        assert stats["p99"] < FRAMEWORK_BUDGET_MS


class TestTheD1Question:
    def test_report_the_numbers_that_decide_it(self, handler, capsys):
        """Prints the comparison ADR-0002 needs.

        DRF is not installed at phase 2 — it arrives with the management API in
        phase 5 — so the honest thing to report today is the plain path's margin
        against the budget, and to re-run this the day DRF is a dependency.
        """
        raw = orjson.dumps(BODY)
        factory = RequestFactory()
        counter = {"n": 0}

        def call():
            counter["n"] += 1
            request = factory.post(
                "/v1/decisions",
                data=raw,
                content_type="application/json",
                headers={"idempotency-key": f"d1-{counter['n']}"},
            )
            request.decision_handler = handler
            return decisions(request)

        stats = measure(call)
        headroom = 100.0 - stats["p99"]

        with capsys.disabled():
            print("\n  D1 — plain Django decision path, 100 active rules")
            print(f"    p50       {stats['p50']:.3f} ms")
            print(f"    p99       {stats['p99']:.3f} ms")
            print(f"    headroom  {headroom:.1f} ms against the 100 ms contract")
            print("    DRF comparison pending: it is not a dependency until phase 5.")

        assert stats["p99"] < 100.0
