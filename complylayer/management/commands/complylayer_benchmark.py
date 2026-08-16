"""Measure this deployment's decision latency against the contract.

§6.2 puts this next to `doctor` for a reason: the latency promise is the product,
and a self-hosted deployment on undersized infrastructure, or with Redis in
another availability zone, will not meet it. The customer should discover that
during installation rather than during an incident.

Reports the stages the budget is itemised by (§4.2), because a total that misses
is not actionable and a stage that misses is.
"""

from __future__ import annotations

import statistics
import time
from datetime import UTC, datetime

from django.core.management.base import BaseCommand

from complylayer.dsl import functions
from complylayer.dsl.interpreter import EvaluationContext
from complylayer.engine import compile_snapshot, decide

CONTRACT_P99_MS = 100.0
EVAL_BUDGET_MS = 5.0


class Command(BaseCommand):
    help = "Measure decision latency on this host against the 100 ms contract."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--rules", type=int, default=100)
        parser.add_argument("--iterations", type=int, default=2000)

    def handle(self, *args, **options) -> None:
        rule_count = options["rules"]
        iterations = options["iterations"]

        self.stdout.write(f"Measuring {rule_count} rules over {iterations} evaluations.\n")

        snapshot = [
            {
                "id": f"rul_{index:03d}",
                "name": f"Rule {index}",
                "expression": f"amount_minor > {1_000_000 + index} and kyc_tier < 4",
                "severity": "flag",
                "state": "active",
                "priority": index,
            }
            for index in range(rule_count)
        ]

        started = time.perf_counter()
        ruleset = compile_snapshot(1, snapshot)
        compile_ms = (time.perf_counter() - started) * 1000
        self.stdout.write(
            f"  warm start        {compile_ms:8.1f} ms  (a worker holds traffic for this)"
        )

        context = EvaluationContext(
            facts={"amount_minor": 7_500_000, "kyc_tier": 2},
            functions=functions.build(None, datetime.now(UTC)),
        )

        for _ in range(50):
            decide(ruleset, context)

        timings = []
        for _ in range(iterations):
            begin = time.perf_counter()
            decide(ruleset, context)
            timings.append((time.perf_counter() - begin) * 1000)

        timings.sort()
        p50 = statistics.median(timings)
        p99 = timings[min(len(timings) - 1, int(len(timings) * 0.99))]

        self.stdout.write(f"  evaluation p50    {p50:8.3f} ms")
        self.stdout.write(f"  evaluation p99    {p99:8.3f} ms  (stage budget {EVAL_BUDGET_MS} ms)")
        self.stdout.write("")

        if p99 > EVAL_BUDGET_MS:
            self.stdout.write(
                self.style.ERROR(
                    f"Evaluation alone uses {p99:.1f} ms of a {CONTRACT_P99_MS:.0f} ms contract. "
                    "This host cannot meet the latency promise with this rule set."
                )
            )
            raise SystemExit(1)

        headroom = CONTRACT_P99_MS - p99
        self.stdout.write(
            self.style.SUCCESS(
                f"Evaluation fits with {headroom:.1f} ms left for facts, I/O and the network. "
                "Run complylayer_doctor as well: Redis distance is the other half of the budget."
            )
        )
