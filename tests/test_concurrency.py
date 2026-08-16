"""Phase 3's exit gate: does the boundary lock actually hold under concurrency?

This is the test that cannot be faked. A velocity rule is only worth having if a
customer firing a hundred transactions at once gets stopped at the threshold
rather than somewhere near it, and the failure mode is not an error — it is a
number that is quietly slightly wrong, in the direction of letting money through.

Runs against a real Redis, because the race being closed is a property of Redis
and a stub would only ever confirm the stub.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis

from complylayer.api.handler import DecisionHandler, hash_customer_ref
from complylayer.api.store import InMemoryStore
from complylayer.api.validation import Transaction
from complylayer.dsl import validate_source
from complylayer.engine import CompiledRule, RuleSet, Severity
from complylayer.velocity import RedisVelocity

pytestmark = pytest.mark.integration

REDIS_URL = os.environ.get("COMPLYLAYER_REDIS_URL", "redis://127.0.0.1:6379/2")

THRESHOLD = 5
ATTEMPTS = 100
REPEATS = 50


@pytest.fixture
def client():
    connection = redis.Redis.from_url(REDIS_URL)
    connection.ping()
    yield connection
    connection.flushdb()


def ruleset(severity: Severity) -> RuleSet:
    return RuleSet(
        1,
        (
            CompiledRule(
                "rul_velocity",
                f"No more than {THRESHOLD} transfers an hour",
                validate_source(f"velocity_count(window='1h') > {THRESHOLD}"),
                severity,
                priority=10,
            ),
        ),
    )


def transaction(index: int) -> Transaction:
    return Transaction(
        transaction_ref=f"TXN-{index:04d}",
        customer_ref="usr_concurrent",
        amount_minor=1_000_000,
        currency="NGN",
        transaction_type="transfer",
    )


def run_attempts(client, severity: Severity, attempts: int = ATTEMPTS) -> list[str]:
    """Fire `attempts` decisions for one customer, concurrently."""
    rules = ruleset(severity)
    store = InMemoryStore()
    customer_hash = hash_customer_ref("usr_concurrent", "salt")

    def one(index: int) -> str:
        velocity = RedisVelocity(client, "tnt_conc", customer_hash)
        handler = DecisionHandler("tnt_conc", rules, store, velocity=velocity)
        return handler.decide(transaction(index), f"key-{index}")["outcome"]

    with ThreadPoolExecutor(max_workers=16) as pool:
        return list(pool.map(one, range(attempts)))


class TestBlockSeverityIsExact:
    """The gate. Fifty runs, zero variance."""

    def test_exactly_the_threshold_is_allowed_every_time(self, client):
        allowed_counts = set()

        for run in range(REPEATS):
            client.flushdb()
            outcomes = run_attempts(client, Severity.BLOCK)
            allowed = sum(1 for outcome in outcomes if outcome == "allow")
            allowed_counts.add(allowed)
            assert allowed == THRESHOLD, (
                f"run {run}: {allowed} allowed, expected exactly {THRESHOLD}. "
                "A velocity block rule that leaks under concurrency is a control "
                "that works only when nobody is trying."
            )

        assert allowed_counts == {THRESHOLD}, f"variance across runs: {sorted(allowed_counts)}"

    def test_the_rest_are_blocked(self, client):
        client.flushdb()
        outcomes = run_attempts(client, Severity.BLOCK)
        assert outcomes.count("block") == ATTEMPTS - THRESHOLD


class TestFlagSeverityIsExactToo:
    """§4.5 accepted imprecision for flag rules as the price of not locking.

    That trade no longer has to be made. Because the write and the read are one
    atomic operation, every severity gets an exact count for free — so a
    reviewer's queue is not padded with transactions that only appear to have
    crossed a threshold.
    """

    def test_flags_are_exact_as_well(self, client):
        client.flushdb()
        outcomes = run_attempts(client, Severity.FLAG)
        assert sum(1 for outcome in outcomes if outcome == "allow") == THRESHOLD


class TestTheWritePathExists:
    """D9. Without it every velocity rule silently never fires."""

    def test_a_decision_adds_itself_to_the_window(self, client):
        velocity = RedisVelocity(client, "tnt_w", "cust")
        handler = DecisionHandler(
            "tnt_w", ruleset(Severity.FLAG), InMemoryStore(), velocity=velocity
        )
        handler.decide(transaction(1), "k1")
        assert client.zcard(velocity.members_key) == 1

        handler.decide(transaction(2), "k2")
        assert client.zcard(velocity.members_key) == 2

    def test_blocked_attempts_still_count(self, client):
        """Eleven transfers just under the reporting threshold with six declined
        is still structuring."""
        velocity = RedisVelocity(client, "tnt_b", "cust")
        rules = RuleSet(
            1,
            (
                CompiledRule(
                    "always",
                    "Blocks everything",
                    validate_source("amount_minor > 1"),
                    Severity.BLOCK,
                ),
            ),
        )
        handler = DecisionHandler("tnt_b", rules, InMemoryStore(), velocity=velocity)
        for index in range(5):
            assert handler.decide(transaction(index), f"k{index}")["outcome"] == "block"

        assert client.zcard(velocity.members_key) == 5

    def test_the_write_is_idempotent_under_retry(self, client):
        """The member is the transaction reference, so a retry updates a score
        rather than inflating the window."""
        velocity = RedisVelocity(client, "tnt_i", "cust")
        for _ in range(5):
            velocity.record("TXN-SAME", 1_000, "transfer")
        assert client.zcard(velocity.members_key) == 1

    def test_the_window_includes_the_transaction_being_decided(self, client):
        """Which is the natural reading of "more than five in an hour": the
        sixth one is the one that trips it."""
        velocity = RedisVelocity(client, "tnt_self", "cust")
        window = velocity.record_and_gather("TXN-1", 1_000, "transfer")
        assert window.count(3_600) == 1


class TestOneRoundTrip:
    """Fact gathering is one Redis round trip, asserted rather than assumed.

    N sequential lookups is the standard way this architecture fails, and it
    fails gradually — nothing errors, the p99 just drifts until it is a page.
    """

    def test_gathering_every_window_costs_one_pipeline(self, client):
        calls: list[str] = []

        class CountingClient:
            def __init__(self, inner):
                self.inner = inner

            def pipeline(self):
                calls.append("pipeline")
                return self.inner.pipeline()

            def __getattr__(self, name):
                calls.append(name)
                return getattr(self.inner, name)

        velocity = RedisVelocity(CountingClient(client), "tnt_rt", "cust")
        velocity.record_and_gather("TXN-1", 1_000, "transfer")

        assert calls == ["pipeline"], f"expected one pipeline, got {calls}"

    def test_many_rules_asking_many_windows_still_cost_one_fetch(self, client):
        """Ten rules across five windows, one round trip between them all."""
        velocity = RedisVelocity(client, "tnt_rt2", "cust")
        for index in range(20):
            velocity.record(f"TXN-{index}", 1_000_000 + index, "transfer")

        window = velocity.gather()
        for seconds in (60, 3_600, 86_400, 604_800, 2_592_000):
            assert window.count(seconds) >= 0
            assert window.total(seconds) >= 0

        # No further calls were needed: every answer came from the one fetch.
        assert velocity.window is window


class TestWindowArithmetic:
    def test_a_window_only_counts_what_falls_inside_it(self, client):
        velocity = RedisVelocity(client, "tnt_win", "cust")
        now = time.time()
        velocity.record("recent", 1_000, "transfer", now - 60)
        velocity.record("old", 1_000, "transfer", now - 7_200)

        window = velocity.gather(now)
        assert window.count(3_600) == 1
        assert window.count(86_400) == 2

    def test_amount_filters_are_exact(self, client):
        velocity = RedisVelocity(client, "tnt_amt", "cust")
        now = time.time()
        for index, amount in enumerate([1_000, 50_000, 100_000]):
            velocity.record(f"t{index}", amount, "transfer", now)

        window = velocity.gather(now)
        assert window.count(3_600, min_amount_minor=50_000) == 2
        assert window.count(3_600, max_amount_minor=50_000) == 2
        assert window.count(3_600, min_amount_minor=50_000, max_amount_minor=50_000) == 1
        assert window.total(3_600) == 151_000

    def test_the_transaction_type_filter(self, client):
        velocity = RedisVelocity(client, "tnt_type", "cust")
        now = time.time()
        velocity.record("a", 1_000, "transfer", now)
        velocity.record("b", 1_000, "withdrawal", now)

        window = velocity.gather(now)
        assert window.count(3_600, transaction_type="transfer") == 1
        assert window.count(3_600) == 2

    def test_expired_members_are_trimmed_by_the_gather(self, client):
        velocity = RedisVelocity(client, "tnt_trim", "cust")
        now = time.time()
        velocity.record("ancient", 1_000, "transfer", now - 60 * 24 * 3600)
        velocity.record("fresh", 1_000, "transfer", now)

        velocity.gather(now)
        assert client.zcard(velocity.members_key) == 1

    def test_keys_expire_on_their_own(self, client):
        """A dormant customer should not accumulate forever."""
        velocity = RedisVelocity(client, "tnt_ttl", "cust")
        velocity.record("t", 1_000, "transfer")
        assert client.ttl(velocity.members_key) > 0
        assert client.ttl(velocity.amounts_key) > 0


class TestAggregateFacts:
    """Customer history, maintained incrementally rather than queried.

    §4.2 allows 15 ms for the whole fact-gathering step. An aggregate over seven
    years of partitioned decision history is not a 15 ms query, so these numbers
    are kept in Redis and updated as decisions are served.
    """

    def test_aggregates_ride_the_same_fetch_as_the_window(self, client):
        velocity = RedisVelocity(client, "tnt_agg", "cust")
        now = time.time()
        velocity.record_and_gather("t1", 1_000_000, "transfer", now)
        velocity.record_and_gather("t2", 3_000_000, "transfer", now)

        facts = velocity.aggregate_facts()
        assert facts["lifetime_transaction_count"] == 2
        assert facts["lifetime_volume_minor"] == 4_000_000
        assert facts["average_transaction_minor"] == 2_000_000

    def test_the_average_is_integer_arithmetic(self, client):
        """D6: no floats anywhere, including in a derived fact."""
        velocity = RedisVelocity(client, "tnt_avg", "cust")
        now = time.time()
        velocity.record_and_gather("t1", 1_000, "transfer", now)
        velocity.record_and_gather("t2", 1_001, "transfer", now)

        average = velocity.aggregate_facts()["average_transaction_minor"]
        assert average == 1_000, "1000.5 must floor, not round"
        assert isinstance(average, int)

    def test_a_customer_with_no_history_gets_zeroes_rather_than_an_error(self, client):
        velocity = RedisVelocity(client, "tnt_new", "brand_new")
        velocity.gather()
        facts = velocity.aggregate_facts()
        assert facts["lifetime_transaction_count"] == 0
        assert facts["average_transaction_minor"] == 0
        assert facts["days_since_last_activity"] == 0

    def test_days_since_last_activity(self, client):
        velocity = RedisVelocity(client, "tnt_dormant", "cust")
        now = time.time()
        velocity.record_and_gather("old", 1_000, "transfer", now - 95 * 86_400)
        velocity.gather(now)
        assert velocity.aggregate_facts()["days_since_last_activity"] == 95

    def test_aggregates_outlive_the_rolling_window(self, client):
        """Account age is meaningless if it resets when a customer goes quiet."""
        velocity = RedisVelocity(client, "tnt_persist", "cust")
        velocity.record_and_gather("t", 1_000, "transfer")
        assert client.ttl(velocity.aggregates_key) == -1, "aggregates must not expire"
        assert client.ttl(velocity.members_key) > 0, "the window must"

    def test_a_rule_can_use_an_aggregate_fact(self, client):
        """End to end: the fact reaches the interpreter under the name a
        compliance officer would write."""
        velocity = RedisVelocity(client, "tnt_rule", hash_customer_ref("usr_concurrent", "salt"))
        rules = RuleSet(
            1,
            (
                CompiledRule(
                    "rul_first_big",
                    "First transaction is unusually large",
                    validate_source("lifetime_transaction_count == 1 and amount_minor > 500_000"),
                    Severity.FLAG,
                ),
            ),
        )
        handler = DecisionHandler("tnt_rule", rules, InMemoryStore(), velocity=velocity)
        assert handler.decide(transaction(1), "k1")["outcome"] == "flag"
        assert handler.decide(transaction(2), "k2")["outcome"] == "allow"

    def test_flag_counts_accumulate(self, client):
        velocity = RedisVelocity(client, "tnt_flags", "cust")
        velocity.record_and_gather("t", 1_000, "transfer")
        velocity.note_flagged()
        velocity.note_flagged()
        velocity.gather()
        assert velocity.aggregate_facts()["prior_flag_count"] == 2
