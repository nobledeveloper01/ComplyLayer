"""Phase 8's chaos gate: what happens when the things it depends on stop.

§11.9 asks for two experiments and both are about the same question — does the
failure mode this product *documented* match the one it actually has?

1. Kill Redis. Every `block` rule must fail closed, every `flag` rule must fail
   open, and every degraded decision must be recorded. §10.3 makes that a product
   decision rather than an accident, and a product decision nobody tested is a
   paragraph.

2. Kill a worker mid-decision. A retry must return the original decision rather
   than making a second one.

The second experiment matters more than it looks. §10.3's note is blunt about
why: if a degraded decision passed silently, "just take ComplyLayer down" would
become a way to move money past the controls.
"""

from __future__ import annotations

import os

import pytest
import redis

from complylayer.api.handler import DecisionHandler, hash_customer_ref
from complylayer.api.store import InMemoryStore
from complylayer.api.validation import Transaction
from complylayer.dsl import validate_source
from complylayer.engine import CompiledRule, RuleSet, Severity, State
from complylayer.velocity import RedisVelocity

pytestmark = pytest.mark.integration

REDIS_URL = os.environ.get("COMPLYLAYER_REDIS_URL", "redis://127.0.0.1:6379/2")


class DeadRedis:
    """A Redis that is there in the type system and gone in every other sense.

    Modelled as connection refusal rather than as a slow response, because that
    is what a failed-over primary with no replica looks like from a worker.
    """

    def __init__(self, error: Exception | None = None):
        self.error = error or ConnectionError("Connection refused")
        self.attempts = 0

    def pipeline(self):
        self.attempts += 1
        raise self.error

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            self.attempts += 1
            raise self.error

        return fail


def velocity_rule(severity: Severity, *, state: State = State.ACTIVE) -> CompiledRule:
    return CompiledRule(
        id=f"rul_{severity}",
        name=f"No more than five an hour ({severity})",
        tree=validate_source("velocity_count(window='1h') > 5"),
        severity=severity,
        state=state,
        priority=10,
    )


def transaction(ref: str = "TXN-CHAOS-1") -> Transaction:
    return Transaction(
        transaction_ref=ref,
        customer_ref="usr_chaos",
        amount_minor=5_000_000,
        currency="NGN",
        transaction_type="transfer",
    )


def dead_velocity(error: Exception | None = None) -> RedisVelocity:
    """A real provider over a dead client.

    Deliberately not a stub provider: the failure has to happen where it happens
    in production — inside the pipeline, during the fetch — or the test proves
    something about the stub instead.
    """
    return RedisVelocity(DeadRedis(error), "tnt_chaos", "cust")


def handler(rules: RuleSet, velocity, store=None) -> DecisionHandler:
    return DecisionHandler("tnt_chaos", rules, store or InMemoryStore(), velocity=velocity)


class TestRedisIsGone:
    """§10.3's table, executed rather than quoted."""

    def test_a_block_rule_fails_closed(self):
        """A blocked transaction is one regulation says must not happen. Allowing
        it during an outage is a compliance breach, and "our vendor was down" has
        never been a defence."""
        velocity = dead_velocity()
        body = handler(RuleSet(1, (velocity_rule(Severity.BLOCK),)), velocity).decide(
            transaction(), "chaos-block"
        )

        assert body["outcome"] == "block"
        assert body["degraded"] is True
        assert velocity.client.attempts > 0, "the test must actually have tried to reach Redis"

    def test_a_flag_rule_fails_open(self):
        """Flagging exists for human review. Halting every transaction because
        the review system is unavailable is disproportionate to the risk."""
        body = handler(RuleSet(1, (velocity_rule(Severity.FLAG),)), dead_velocity()).decide(
            transaction(), "chaos-flag"
        )

        assert body["outcome"] == "allow"
        assert body["degraded"] is True

    def test_both_severities_together_behave_independently(self):
        """The realistic case: a rule set with both, and one outage."""
        rules = RuleSet(1, (velocity_rule(Severity.BLOCK), velocity_rule(Severity.FLAG)))
        body = handler(rules, dead_velocity()).decide(transaction(), "chaos-both")

        assert body["outcome"] == "block"
        assert body["degraded"] is True
        assert {entry["id"] for entry in body["matched_rules"]} == {"rul_block"}

    def test_a_tenant_may_override_the_default_and_it_is_still_recorded(self):
        """An override changes the outcome, never the recording."""
        store = InMemoryStore(fallback={Severity.BLOCK: "open"})
        body = handler(RuleSet(1, (velocity_rule(Severity.BLOCK),)), dead_velocity(), store).decide(
            transaction(), "chaos-override"
        )

        assert body["outcome"] == "allow"
        assert body["degraded"] is True

    def test_every_degraded_decision_is_recorded_with_its_reason(self):
        """Otherwise "just take ComplyLayer down" becomes a way to move money
        past the controls, and nothing anywhere would report it."""
        body = handler(RuleSet(1, (velocity_rule(Severity.BLOCK),)), dead_velocity()).decide(
            transaction(), "chaos-recorded"
        )

        errored = body["_errored_rules"]
        assert len(errored) == 1
        assert errored[0]["id"] == "rul_block"
        assert errored[0]["error"], "a degraded decision with no stated reason is unactionable"

    def test_rules_that_need_no_velocity_still_decide_normally(self):
        """A Redis outage must not take down rules that never asked for it.

        This is the difference between a degraded service and an outage: an
        amount limit does not need Redis, and it should keep working.
        """
        rules = RuleSet(
            1,
            (
                CompiledRule(
                    "rul_amount",
                    "Over the limit",
                    validate_source("amount_minor > 1000000"),
                    Severity.BLOCK,
                    priority=5,
                ),
                velocity_rule(Severity.FLAG),
            ),
        )
        body = handler(rules, dead_velocity()).decide(transaction(), "chaos-mixed")

        assert body["outcome"] == "block"
        assert {entry["id"] for entry in body["matched_rules"]} == {"rul_amount"}

    def test_a_shadow_rule_failing_cannot_change_the_outcome(self):
        """Even fail-closed. A shadow rule that blocked during an outage would be
        the worst possible version of "it never affects anyone"."""
        rules = RuleSet(1, (velocity_rule(Severity.BLOCK, state=State.SHADOW),))
        body = handler(rules, dead_velocity()).decide(transaction(), "chaos-shadow")

        assert body["outcome"] == "allow"
        assert body["degraded"] is True

    def test_an_empty_rule_set_is_unaffected(self):
        body = handler(RuleSet(1, ()), dead_velocity()).decide(transaction(), "chaos-empty")
        assert body["outcome"] == "allow"
        assert body["degraded"] is False

    @pytest.mark.parametrize(
        "failure",
        [
            ConnectionError("Connection refused"),
            TimeoutError("timed out"),
            OSError("No route to host"),
        ],
        ids=["refused", "timeout", "no-route"],
    )
    def test_every_way_redis_can_fail_lands_in_the_same_place(self, failure):
        """A worker cannot distinguish these, and the fallback must not try to."""
        body = handler(RuleSet(1, (velocity_rule(Severity.BLOCK),)), dead_velocity(failure)).decide(
            transaction(), f"chaos-{type(failure).__name__}"
        )
        assert body["outcome"] == "block"
        assert body["degraded"] is True


class TestRedisComesBack:
    """Recovery, which is the half of a chaos test people skip."""

    @pytest.fixture
    def client(self):
        connection = redis.Redis.from_url(REDIS_URL)
        connection.ping()
        yield connection
        connection.flushdb()

    def test_decisions_are_normal_again_once_redis_returns(self, client):
        rules = RuleSet(1, (velocity_rule(Severity.BLOCK),))
        customer = hash_customer_ref("usr_chaos", "salt")

        during = handler(rules, dead_velocity()).decide(transaction("TXN-DURING"), "recover-1")
        assert during["degraded"] is True

        after = handler(rules, RedisVelocity(client, "tnt_chaos", customer)).decide(
            transaction("TXN-AFTER"), "recover-2"
        )
        assert after["degraded"] is False
        assert after["outcome"] == "allow"

    def test_the_window_does_not_contain_the_decisions_made_while_it_was_down(self, client):
        """Honest consequence, worth knowing rather than discovering.

        A transaction decided while Redis was unreachable was never written to
        the window, so it does not count towards a later velocity rule. §11.7
        already says velocity state is deliberately not backed up because a
        rolling window rebuilds; this is the same trade seen from the other side,
        and it is why §10.3 asks for degraded decisions to be backfilled into the
        review queue rather than forgotten.
        """
        customer = hash_customer_ref("usr_chaos", "salt")
        rules = RuleSet(1, (velocity_rule(Severity.FLAG),))

        handler(rules, dead_velocity()).decide(transaction("TXN-LOST"), "lost-1")

        velocity = RedisVelocity(client, "tnt_chaos", customer)
        assert client.zcard(velocity.members_key) == 0


class TestAWorkerDiesMidDecision:
    """Kill a pod between deciding and returning. A retry must not decide twice."""

    def test_a_retry_returns_the_original_decision(self):
        store = InMemoryStore()
        rules = RuleSet(
            1,
            (
                CompiledRule(
                    "rul_a",
                    "Over the limit",
                    validate_source("amount_minor > 1000000"),
                    Severity.BLOCK,
                ),
            ),
        )

        first = handler(rules, None, store)
        body = first.decide(transaction(), "same-key")
        first.record(body, transaction(), "same-key")

        # A new handler stands in for a new worker: the old one is gone, and the
        # only thing shared is the store.
        replay = handler(rules, None, store).replay("same-key")

        assert replay is not None
        assert replay["decision_id"] == body["decision_id"]
        assert replay["decided_at"] == body["decided_at"]

    def test_a_decision_that_was_never_recorded_is_decided_again(self):
        """The genuine gap: the worker died *before* the write.

        There is no record, so the retry decides fresh — and because evaluation
        is deterministic against a pinned rule set version, it reaches the same
        answer. The caller is never given two different ones, which is the
        property that actually matters.
        """
        store = InMemoryStore()
        rules = RuleSet(
            1,
            (
                CompiledRule(
                    "rul_a",
                    "Over the limit",
                    validate_source("amount_minor > 1000000"),
                    Severity.BLOCK,
                ),
            ),
        )

        assert handler(rules, None, store).replay("never-written") is None

        again = handler(rules, None, store).decide(transaction(), "never-written")
        assert again["outcome"] == "block"

    def test_two_workers_deciding_the_same_key_agree(self):
        """Both miss the cache, both evaluate, and both must reach the same
        answer — otherwise the caller's retry changes the outcome."""
        store = InMemoryStore()
        rules = RuleSet(
            47,
            (
                CompiledRule(
                    "rul_a",
                    "Over the limit",
                    validate_source("amount_minor > 1000000"),
                    Severity.BLOCK,
                ),
            ),
        )

        outcomes = {
            handler(rules, None, store).decide(transaction(), "concurrent")["outcome"]
            for _ in range(20)
        }
        assert outcomes == {"block"}

    def test_the_velocity_write_is_idempotent_under_retry(self):
        """A retried decision must not inflate the window it was counted in."""
        connection = redis.Redis.from_url(REDIS_URL)
        connection.flushdb()
        try:
            velocity = RedisVelocity(connection, "tnt_retry", "cust")
            for _ in range(5):
                velocity.record_and_gather("TXN-RETRIED", 1_000_000, "transfer")
            assert connection.zcard(velocity.members_key) == 1
        finally:
            connection.flushdb()


class TestTheFallbackPolicyIsWhatTheDocumentSays:
    """A last check against §10.3's table, read as data rather than as prose."""

    def test_the_defaults_match_the_specification(self):
        from complylayer.engine.evaluation import DEFAULT_FALLBACK

        assert DEFAULT_FALLBACK[Severity.BLOCK] == "closed"
        assert DEFAULT_FALLBACK[Severity.FLAG] == "open"

    def test_a_degraded_decision_is_never_silently_normal(self):
        """The property the whole chaos suite exists to protect."""
        for severity in (Severity.BLOCK, Severity.FLAG, Severity.ALLOW_WITH_NOTE):
            body = handler(RuleSet(1, (velocity_rule(severity),)), dead_velocity()).decide(
                transaction(), f"never-silent-{severity}"
            )
            assert body["degraded"] is True, f"{severity} hid its own degradation"


class TestOutcomesUnderLoad:
    """A smoke test at a fraction of the §11.9 target, runnable anywhere.

    The real load test is `deploy/k6/decisions.js` at 2,000 decisions/second on
    dedicated hardware. This one exists so that a change which breaks throughput
    by an order of magnitude fails in CI rather than in a load-test run somebody
    schedules monthly.
    """

    def test_a_thousand_decisions_stay_correct_and_quick(self):
        import time

        rules = RuleSet(
            47,
            tuple(
                CompiledRule(
                    f"rul_{index:03d}",
                    f"Rule {index}",
                    validate_source(f"amount_minor > {1_000_000 + index * 1000}"),
                    Severity.BLOCK if index % 3 == 0 else Severity.FLAG,
                    priority=index,
                )
                for index in range(100)
            ),
        )
        decider = handler(rules, None, InMemoryStore())

        started = time.perf_counter()
        outcomes = [
            decider.decide(transaction(f"TXN-LOAD-{index}"), f"load-{index}")["outcome"]
            for index in range(1000)
        ]
        elapsed = time.perf_counter() - started

        assert set(outcomes) == {"block"}
        assert elapsed < 10, f"1,000 decisions over 100 rules took {elapsed:.1f}s"
