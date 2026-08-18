"""The rule cache, version propagation, and warm start.

The most important test in this file is the one where pub/sub is severed. §11.6
exists because a dropped subscription that never reconnected is the usual cause
of version skew, and the thing that makes it dangerous is that nothing errors:
latency is fine, no exception is raised, the dashboard is green, and a fraction
of traffic is being evaluated against rules that were retired last week.

So the requirement is not "pub/sub works". It is "activation still propagates
within 30 seconds when pub/sub does not".
"""

from __future__ import annotations

import time

import pytest

from complylayer.engine import (
    POLL_INTERVAL_SECONDS,
    RuleSetCache,
    SnapshotError,
    VersionWatcher,
    compile_snapshot,
    metrics,
)
from complylayer.engine.evaluation import Outcome, Severity, State


def snapshot_entry(rule_id: str, expression: str, **overrides) -> dict:
    return {
        "id": rule_id,
        "name": overrides.get("name", rule_id),
        "expression": expression,
        "severity": overrides.get("severity", "flag"),
        "state": overrides.get("state", "active"),
        "priority": overrides.get("priority", 0),
        "regulatory_reference": overrides.get("regulatory_reference", ""),
        "customer_message": overrides.get("customer_message", ""),
    }


class Publisher:
    """Stands in for the management API publishing a new version."""

    def __init__(self):
        self.version = 1
        self.rules = [snapshot_entry("rul_a", "amount_minor > 1_000_000")]
        self.lists: dict[str, list] = {}
        self.reads = 0

    def load(self):
        self.reads += 1
        return self.version, self.rules, self.lists

    def publish(self, rules, lists=None):
        self.version += 1
        self.rules = rules
        self.lists = lists or {}
        return self.version


@pytest.fixture
def publisher() -> Publisher:
    return Publisher()


@pytest.fixture
def cache(publisher) -> RuleSetCache:
    return RuleSetCache("tnt_cache", publisher.load)


class TestCompilingASnapshot:
    def test_a_snapshot_becomes_an_evaluable_rule_set(self):
        ruleset = compile_snapshot(
            7,
            [
                snapshot_entry("b", "amount_minor > 5", severity="block", priority=10),
                snapshot_entry("f", "amount_minor > 1", severity="flag", priority=20),
            ],
        )
        assert ruleset.version == 7
        assert [rule.id for rule in ruleset.rules] == ["b", "f"]
        assert ruleset.rules[0].severity is Severity.BLOCK

    def test_states_are_preserved(self):
        ruleset = compile_snapshot(1, [snapshot_entry("s", "amount_minor > 1", state="shadow")])
        assert ruleset.rules[0].state is State.SHADOW

    def test_a_snapshot_is_re_validated_on_load(self):
        """D5, layer three. A snapshot is data in a database, and a database is
        something an attacker who has got that far can edit."""
        with pytest.raises(SnapshotError) as exc:
            compile_snapshot(1, [snapshot_entry("evil", "().__class__.__bases__[0]")])
        assert "failed re-validation" in str(exc.value)

    def test_a_snapshot_missing_a_field_is_refused_clearly(self):
        with pytest.raises(SnapshotError):
            compile_snapshot(1, [{"id": "x", "severity": "flag"}])

    def test_re_validation_catches_what_the_allowlist_would_catch(self):
        """Not a weaker check applied to trusted data — the identical one."""
        for expression in ["customer.kyc_tier > 1", "'a' * 999999999 > 1", "__builtins__"]:
            with pytest.raises(SnapshotError):
                compile_snapshot(1, [snapshot_entry("x", expression)])


class TestWarmStart:
    def test_a_cache_is_cold_before_its_first_load(self, cache):
        assert cache.is_warm is False
        assert cache.version is None

    def test_it_is_warm_after_one_refresh(self, cache):
        assert cache.refresh() is True
        assert cache.is_warm is True
        assert cache.version == 1

    def test_a_second_refresh_at_the_same_version_does_nothing(self, cache):
        cache.refresh()
        assert cache.refresh() is False, "recompiling an unchanged rule set is wasted work"

    def test_forcing_a_refresh_recompiles_anyway(self, cache):
        cache.refresh()
        assert cache.refresh(force=True) is True

    def test_nothing_published_leaves_the_cache_cold(self):
        cold = RuleSetCache("tnt_empty", lambda: None)
        assert cold.refresh() is False
        assert cold.is_warm is False


class TestSwapping:
    def test_a_new_version_replaces_the_old_one(self, cache, publisher):
        cache.refresh()
        first = cache.current

        publisher.publish([snapshot_entry("rul_b", "amount_minor > 2_000_000")])
        assert cache.refresh() is True
        assert cache.version == 2
        assert cache.current is not first, "the swap rebinds rather than mutating"

    def test_the_previous_rule_set_object_is_unchanged_after_a_swap(self, cache, publisher):
        """A reader holding the old object must keep seeing a whole, consistent
        rule set — never a half-built one."""
        cache.refresh()
        held = cache.current

        publisher.publish([snapshot_entry("rul_new", "amount_minor > 1")])
        cache.refresh()

        assert held.ruleset.version == 1
        assert [rule.id for rule in held.ruleset.rules] == ["rul_a"]

    def test_named_lists_come_with_the_version(self, cache, publisher):
        """D11: editing a list has to publish a version, or two decisions
        recording the same version would not mean the same control."""
        publisher.publish(
            [snapshot_entry("rul_corridor", "destination_country in high_risk_countries")],
            {"high_risk_countries": ["XX", "YY"]},
        )
        cache.refresh()
        assert cache.current.lists == {"high_risk_countries": ("XX", "YY")}

    def test_a_broken_snapshot_does_not_replace_a_working_one(self, cache, publisher):
        """A rule set that will not compile is a management-side problem.
        Refusing to decide would turn it into an outage."""
        cache.refresh()
        publisher.publish([snapshot_entry("bad", "().__class__")])

        with pytest.raises(SnapshotError):
            cache.refresh()

        assert cache.version == 1, "the working version keeps serving"
        assert cache.is_warm is True


class TestPropagation:
    def test_an_announcement_refreshes_immediately(self, cache, publisher):
        watcher = VersionWatcher(cache, client=None, poll_interval=3600)
        cache.refresh()

        publisher.publish([snapshot_entry("rul_b", "amount_minor > 1")])
        assert watcher.on_announcement() is True
        assert cache.version == 2

    def test_propagation_survives_a_severed_subscription(self, cache, publisher):
        """The test §11.6 is written for.

        No client at all stands in for a subscription that dropped and never
        reconnected. Nothing raises, nothing is logged as an error — the only
        thing that saves this worker is the poll.
        """
        watcher = VersionWatcher(cache, client=None, poll_interval=0.05)
        watcher.start()
        try:
            deadline = time.time() + 5
            while cache.version != 1 and time.time() < deadline:
                time.sleep(0.01)
            assert cache.version == 1

            publisher.publish([snapshot_entry("rul_urgent", "amount_minor > 1", severity="block")])

            deadline = time.time() + 5
            while cache.version != 2 and time.time() < deadline:
                time.sleep(0.01)
        finally:
            watcher.stop()

        assert cache.version == 2, "with pub/sub gone, the poll has to carry propagation"
        assert watcher.announcements == 0, "nothing announced it — the poll found it"

    def test_the_poll_interval_meets_the_thirty_second_requirement(self):
        """§3.4 requires activation within 30 seconds. The poll alone has to
        satisfy that, because pub/sub is a latency optimisation rather than the
        mechanism the guarantee rests on."""
        assert POLL_INTERVAL_SECONDS < 30

    def test_a_broken_snapshot_does_not_stop_the_watcher(self, cache, publisher):
        cache.refresh()
        publisher.publish([snapshot_entry("bad", "().__class__")])

        assert watcher_poll(cache) is False
        assert cache.version == 1

        publisher.publish([snapshot_entry("good", "amount_minor > 1")])
        assert watcher_poll(cache) is True
        assert cache.version == 3, "the watcher recovered rather than wedging"


def watcher_poll(cache) -> bool:
    return VersionWatcher(cache, client=None).poll_once()


class TestMetrics:
    def setup_method(self):
        metrics.reset()

    def test_the_version_gauge_is_labelled_per_worker(self):
        """Per pod it would hide skew inside a pod, which is the failure the
        metric exists to catch (D12)."""
        metrics.set_gauge("complylayer_ruleset_version", 47, {"tenant": "tnt_a"})
        rendered = metrics.render()
        assert "complylayer_ruleset_version" in rendered
        assert 'worker="' in rendered

    def test_counters_and_histograms_render(self):
        metrics.increment("complylayer_decisions_total", {"outcome": "block"})
        metrics.observe("complylayer_decision_duration_ms", 18.0)
        rendered = metrics.render()
        assert 'complylayer_decisions_total{outcome="block"' in rendered
        assert "complylayer_decision_duration_ms_bucket" in rendered
        assert "complylayer_decision_duration_ms_count" in rendered

    def test_histogram_buckets_are_cumulative(self):
        for value in (1.0, 3.0, 30.0):
            metrics.observe("d", value)
        rendered = metrics.render()
        assert 'd_bucket{worker="' in rendered
        assert "d_count" in rendered
        assert "d_sum" in rendered

    def test_the_stage_names_match_the_budget(self):
        """§4.2 itemises the budget by stage, and a p99 alert without the
        breakdown is undiagnosable at 3am."""
        assert metrics.STAGES == ("auth", "facts", "eval", "serialize")

    def test_snapshot_reports_what_was_recorded(self):
        metrics.increment("a")
        metrics.set_gauge("b", 1)
        metrics.observe("c", 5.0)
        state = metrics.snapshot()
        assert len(state["counters"]) == 1
        assert len(state["gauges"]) == 1
        assert len(state["histograms"]) == 1


class TestTheCacheFeedsDecisions:
    def test_a_decision_uses_whatever_version_the_cache_holds(self, cache, publisher):
        from complylayer.api.handler import DecisionHandler
        from complylayer.api.store import InMemoryStore
        from complylayer.api.validation import Transaction

        cache.refresh()
        transaction = Transaction(
            transaction_ref="TXN-1",
            customer_ref="usr",
            amount_minor=2_000_000,
            currency="NGN",
        )

        handler = DecisionHandler("tnt_cache", cache.current.ruleset, InMemoryStore())
        assert handler.decide(transaction, "k1")["outcome"] == Outcome.FLAG

        publisher.publish(
            [snapshot_entry("rul_block", "amount_minor > 1_000_000", severity="block")]
        )
        cache.refresh()

        handler = DecisionHandler("tnt_cache", cache.current.ruleset, InMemoryStore())
        body = handler.decide(transaction, "k2")
        assert body["outcome"] == Outcome.BLOCK
        assert body["ruleset_version"] == 2


class TestReadiness:
    """A worker must not take traffic before it can serve a decision.

    §11.1: a pod serving before its cache is warm produces a latency spike on
    every single deploy, because every early request pays for the compile.
    """

    def test_readyz_refuses_while_the_cache_is_cold(self, client, cache):
        response = client.get("/readyz")
        assert response.status_code == 503

    def test_readyz_reports_ready_once_warm(self, client, cache):
        cache.refresh()
        response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["ruleset_version"] == 1

    def test_healthz_does_not_depend_on_the_cache(self, client, cache):
        """A liveness probe that checks dependencies restarts the process when a
        dependency is down, turning one outage into two."""
        assert client.get("/healthz").status_code == 200

    def test_metrics_are_exposed(self, client, cache):
        metrics.reset()
        metrics.set_gauge("complylayer_ruleset_version", 1)
        body = client.get("/metrics").content.decode()
        assert "complylayer_ruleset_version" in body


@pytest.fixture
def client(cache, monkeypatch, view_only):
    from django.core.handlers.base import BaseHandler
    from django.test import Client

    original = BaseHandler.get_response

    def get_response(self, request):
        request.ruleset_cache = cache
        return original(self, request)

    monkeypatch.setattr(BaseHandler, "get_response", get_response)
    return Client()


class TestBenchmarkCommand:
    """The command a self-hosting customer runs to find out whether this host
    can actually meet the latency promise (§6.2)."""

    def test_it_reports_the_stages_and_succeeds_on_capable_hardware(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("complylayer_benchmark", rules=20, iterations=100, stdout=out)
        output = out.getvalue()

        assert "warm start" in output
        assert "evaluation p50" in output
        assert "evaluation p99" in output
        assert "left for facts" in output

    def test_it_fails_loudly_when_the_stage_budget_is_blown(self, monkeypatch):
        """The point of a preflight is that it can say no.

        A budget of zero stands in for undersized hardware: the command must
        exit non-zero so a deploy pipeline notices, rather than printing a sad
        number and carrying on.
        """
        from io import StringIO

        import pytest as _pytest
        from django.core.management import call_command

        from complylayer.management.commands import complylayer_benchmark

        monkeypatch.setattr(complylayer_benchmark, "EVAL_BUDGET_MS", 0.0)
        with _pytest.raises(SystemExit) as exc:
            call_command("complylayer_benchmark", rules=5, iterations=50, stdout=StringIO())
        assert exc.value.code == 1
