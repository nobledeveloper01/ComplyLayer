"""Velocity logic without a Redis.

`tests/test_concurrency.py` proves this works against the real thing, including
the race that a fake could never demonstrate. This file covers the arithmetic and
the parsing, which are pure and deserve to be testable on a clean checkout with
no docker running.

The fake below implements only the commands the store actually uses. That is
deliberate: a fake that grows to cover all of Redis becomes a second
implementation to maintain and to be wrong in.
"""

from __future__ import annotations

import pytest

from complylayer.velocity import RedisVelocity, Window
from complylayer.velocity.redis_store import (
    KEY_TTL_SECONDS,
    Aggregates,
    _parse_aggregates,
    _split_amount,
)


class FakePipeline:
    def __init__(self, store: FakeRedis):
        self.store = store
        self.queued: list[tuple] = []

    def zremrangebyscore(self, key, low, high):
        self.queued.append(("zremrangebyscore", key, low, high))
        return self

    def zadd(self, key, mapping):
        self.queued.append(("zadd", key, mapping))
        return self

    def hset(self, key, field, value):
        self.queued.append(("hset", key, field, value))
        return self

    def hsetnx(self, key, field, value):
        self.queued.append(("hsetnx", key, field, value))
        return self

    def hincrby(self, key, field, amount):
        self.queued.append(("hincrby", key, field, amount))
        return self

    def expire(self, key, seconds):
        self.queued.append(("expire", key, seconds))
        return self

    def persist(self, key):
        self.queued.append(("persist", key))
        return self

    def zrangebyscore(self, key, low, high, withscores=False):
        self.queued.append(("zrangebyscore", key, low, high))
        return self

    def hgetall(self, key):
        self.queued.append(("hgetall", key))
        return self

    def execute(self):
        results = []
        for command in self.queued:
            results.append(self.store.apply(command))
        self.queued = []
        return results


class FakeRedis:
    """Enough Redis to exercise the store's own logic, and no more."""

    def __init__(self):
        self.sorted_sets: dict[str, dict[str, float]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttls: dict[str, int] = {}
        self.pipelines = 0

    def pipeline(self):
        self.pipelines += 1
        return FakePipeline(self)

    def apply(self, command):
        name, key, *rest = command
        if name == "zremrangebyscore":
            low, high = rest
            members = self.sorted_sets.setdefault(key, {})
            removed = [m for m, score in members.items() if low <= score <= high]
            for member in removed:
                del members[member]
            return len(removed)
        if name == "zadd":
            self.sorted_sets.setdefault(key, {}).update(rest[0])
            return 1
        if name == "hset":
            self.hashes.setdefault(key, {})[rest[0]] = str(rest[1])
            return 1
        if name == "hsetnx":
            fields = self.hashes.setdefault(key, {})
            if rest[0] in fields:
                return 0
            fields[rest[0]] = str(rest[1])
            return 1
        if name == "hincrby":
            fields = self.hashes.setdefault(key, {})
            fields[rest[0]] = str(int(fields.get(rest[0], 0)) + rest[1])
            return int(fields[rest[0]])
        if name == "expire":
            self.ttls[key] = rest[0]
            return 1
        if name == "persist":
            self.ttls.pop(key, None)
            return 1
        if name == "zrangebyscore":
            low, _ = rest
            members = self.sorted_sets.get(key, {})
            return sorted(
                ((m, s) for m, s in members.items() if s >= low), key=lambda pair: pair[1]
            )
        if name == "hgetall":
            return dict(self.hashes.get(key, {}))
        raise AssertionError(f"the fake does not implement {name}")


@pytest.fixture
def velocity() -> RedisVelocity:
    return RedisVelocity(FakeRedis(), "tnt", "cust")


class TestOneRoundTrip:
    def test_recording_and_gathering_is_a_single_pipeline(self, velocity):
        velocity.record_and_gather("t1", 1_000, "transfer", now=100.0)
        assert velocity.client.pipelines == 1

    def test_reading_without_writing_is_also_a_single_pipeline(self, velocity):
        velocity.gather(now=100.0)
        assert velocity.client.pipelines == 1

    def test_every_window_a_rule_asks_for_comes_from_that_one_fetch(self, velocity):
        for index in range(5):
            velocity.record_and_gather(f"t{index}", 1_000, "transfer", now=100.0)
        before = velocity.client.pipelines

        for seconds in (60, 3_600, 86_400, 604_800):
            velocity.count(seconds)
            velocity.total(seconds)
        velocity.aggregate_facts()

        assert velocity.client.pipelines == before, "answers must come from the fetched window"


class TestWindowArithmetic:
    def test_only_what_falls_inside_the_window_counts(self):
        window = Window(
            now=1000.0,
            entries=(("a", 999.0, 100, "transfer"), ("b", 100.0, 100, "transfer")),
        )
        assert window.count(60) == 1
        assert window.count(3_600) == 2

    def test_amount_bounds_are_inclusive(self):
        window = Window(
            now=1000.0,
            entries=(
                ("a", 999.0, 100, "transfer"),
                ("b", 999.0, 200, "transfer"),
                ("c", 999.0, 300, "transfer"),
            ),
        )
        assert window.count(60, min_amount_minor=200) == 2
        assert window.count(60, max_amount_minor=200) == 2
        assert window.count(60, min_amount_minor=200, max_amount_minor=200) == 1

    def test_totals_sum_the_amounts(self):
        window = Window(
            now=1000.0,
            entries=(("a", 999.0, 100, "transfer"), ("b", 999.0, 250, "transfer")),
        )
        assert window.total(60) == 350
        assert window.total(60, min_amount_minor=200) == 250

    def test_the_transaction_type_filter(self):
        window = Window(
            now=1000.0,
            entries=(("a", 999.0, 100, "transfer"), ("b", 999.0, 100, "withdrawal")),
        )
        assert window.count(60, transaction_type="transfer") == 1
        assert window.count(60) == 2

    def test_an_empty_window(self):
        window = Window(now=1000.0)
        assert window.count(3_600) == 0
        assert window.total(3_600) == 0


class TestAggregates:
    def test_the_average_floors(self):
        facts = Aggregates(lifetime_count=3, lifetime_volume_minor=1000).as_facts(now=0.0)
        assert facts["average_transaction_minor"] == 333

    def test_no_history_does_not_divide_by_zero(self):
        facts = Aggregates().as_facts(now=1000.0)
        assert facts["average_transaction_minor"] == 0
        assert facts["days_since_last_activity"] == 0
        assert facts["customer_known_days"] == 0

    def test_days_are_whole_and_never_negative(self):
        facts = Aggregates(last_transaction_at=1000.0, first_seen_at=1000.0).as_facts(now=500.0)
        assert facts["days_since_last_activity"] == 0, "a clock that went backwards is not -1 days"

    def test_days_since_last_activity(self):
        now = 100 * 86_400
        facts = Aggregates(last_transaction_at=now - 95 * 86_400).as_facts(now=now)
        assert facts["days_since_last_activity"] == 95

    def test_every_aggregate_fact_is_an_integer(self):
        """D6 again: a derived fact is as capable of introducing a float as an
        operator is."""
        facts = Aggregates(
            lifetime_count=7, lifetime_volume_minor=1_000_001, last_transaction_at=1.0
        ).as_facts(now=100_000.0)
        assert all(isinstance(value, int) for value in facts.values())


class TestParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (b"1000:transfer", (1000, "transfer")),
            ("1000:transfer", (1000, "transfer")),
            (b"1000:", (1000, "")),
            (None, (0, "")),
            (b"notanumber:transfer", (0, "transfer")),
            (b"", (0, "")),
        ],
    )
    def test_amount_fields(self, raw, expected):
        assert _split_amount(raw) == expected

    def test_aggregate_fields_survive_bytes_and_text(self):
        parsed = _parse_aggregates({b"lifetime_count": b"5", b"first_seen_at": b"1000.5"})
        assert parsed.lifetime_count == 5
        assert parsed.first_seen_at == 1000.5

    def test_missing_and_corrupt_fields_become_zero_rather_than_an_error(self):
        """A decision must not fail because an aggregate is unreadable — the
        rolling window is the control, and this is context around it."""
        parsed = _parse_aggregates({b"lifetime_count": b"nonsense"})
        assert parsed.lifetime_count == 0
        assert _parse_aggregates({}).lifetime_volume_minor == 0
        assert _parse_aggregates(None).prior_flag_count == 0


class TestKeyLifetimes:
    def test_the_window_expires_and_the_aggregates_do_not(self, velocity):
        velocity.record_and_gather("t", 1_000, "transfer", now=100.0)
        assert velocity.client.ttls[velocity.members_key] == KEY_TTL_SECONDS
        assert velocity.client.ttls[velocity.amounts_key] == KEY_TTL_SECONDS
        assert velocity.aggregates_key not in velocity.client.ttls

    def test_keys_are_scoped_by_tenant_and_customer(self):
        one = RedisVelocity(FakeRedis(), "tnt_a", "cust_1")
        two = RedisVelocity(FakeRedis(), "tnt_b", "cust_1")
        assert one.members_key != two.members_key
        assert "tnt_a" in one.members_key and "cust_1" in one.members_key


class TestTruncation:
    def test_a_very_large_window_is_capped_at_the_newest_members(self, monkeypatch):
        """One high-volume customer must not degrade the shared operation for
        everyone (§11.5). The cap is load protection, so it keeps the newest."""
        from complylayer.velocity import redis_store

        monkeypatch.setattr(redis_store, "MAX_MEMBERS_FETCHED", 10)
        velocity = RedisVelocity(FakeRedis(), "tnt", "busy")
        for index in range(25):
            velocity.record_and_gather(f"t{index:03d}", 1_000, "transfer", now=100.0 + index)

        window = velocity.window
        assert window.truncated is True
        assert len(window.entries) == 10
        assert window.entries[-1][0] == "t024", "the newest must survive the cap"
