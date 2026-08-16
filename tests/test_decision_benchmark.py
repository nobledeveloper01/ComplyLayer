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


@pytest.mark.django_db
class TestTheD1QuestionAnsweredAtLast:
    """The half of the D1 benchmark that phase 2 could not run.

    ADR-0002 recorded D1 as *survivable, not proven*: hand-written validation
    cost 3 microseconds against a 5 millisecond budget, so DRF would have to be
    about 1,700 times slower to fail — and DRF was not a dependency yet, so the
    claim stayed untested. It is a dependency now.
    """

    def test_compare_the_two_request_cycles(self, handler, capsys, settings):
        import django
        from rest_framework.test import APIRequestFactory
        from rest_framework.views import APIView

        settings.REST_FRAMEWORK = {
            "DEFAULT_AUTHENTICATION_CLASSES": [],
            "DEFAULT_PERMISSION_CLASSES": [],
            "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
        }

        from rest_framework.response import Response as DrfResponse

        from complylayer.api import validation

        class DrfDecisionView(APIView):
            """The same work, behind DRF's request cycle."""

            def post(self, request):
                transaction = validation.parse_transaction(request.data)
                body = handler.decide(transaction, request.headers["Idempotency-Key"])
                return DrfResponse({k: v for k, v in body.items() if not k.startswith("_")})

        drf_view = DrfDecisionView.as_view()
        drf_factory = APIRequestFactory()
        plain_factory = RequestFactory()
        raw = orjson.dumps(BODY)
        counter = {"n": 0}

        def call_drf():
            counter["n"] += 1
            request = drf_factory.post(
                "/v1/decisions",
                data=raw,
                content_type="application/json",
                headers={"idempotency-key": f"drf-{counter['n']}"},
            )
            return drf_view(request)

        def call_plain():
            counter["n"] += 1
            request = plain_factory.post(
                "/v1/decisions",
                data=raw,
                content_type="application/json",
                headers={"idempotency-key": f"plain-{counter['n']}"},
            )
            request.decision_handler = handler
            return decisions(request)

        drf = measure(call_drf, 1000)
        plain = measure(call_plain, 1000)
        overhead = drf["p99"] - plain["p99"]

        with capsys.disabled():
            print(f"\n  D1 settled — Django {django.get_version()}, 100 active rules")
            print(f"    plain Django   p50 {plain['p50']:.3f} ms   p99 {plain['p99']:.3f} ms")
            print(f"    DRF            p50 {drf['p50']:.3f} ms   p99 {drf['p99']:.3f} ms")
            print(f"    DRF overhead   p99 +{overhead:.3f} ms against a 100 ms contract")
            verdict = (
                "DRF fits the budget — the two-path split does not pay for itself"
                if drf["p99"] < 25.0
                else "DRF misses the stage budget — the split is justified"
            )
            print(f"    verdict: {verdict}")

        # Whatever the numbers say, neither path may breach the contract.
        assert plain["p99"] < 100.0
        assert drf["p99"] < 100.0
