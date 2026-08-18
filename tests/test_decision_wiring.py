"""The test that was missing for eight phases.

Every other test of `POST /v1/decisions` attaches `request.decision_handler`
itself. That is reasonable for testing the endpoint's logic and it is exactly why
832 green tests sat on top of an endpoint that raised `AttributeError` on the
first real request: the seam every test used was the seam nobody had built.

So this file uses the real settings module, the real middleware stack, and a real
API key. Nothing is attached by hand. If the wiring breaks again, this fails —
and it is the only test here that would.

The lesson generalises past this bug: a test that supplies the thing under test
with its own dependencies proves the dependencies work together, not that
anything assembles them.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import orjson
import pytest
import redis
from django.test import Client

from complylayer import partitions, rules
from complylayer.api import auth
from complylayer.api import decision_middleware as wiring
from complylayer.models import ApiKey, Decision, Tenant
from complylayer.tenancy import Actor, Role

REDIS_URL = os.environ.get("COMPLYLAYER_REDIS_URL", "redis://127.0.0.1:6379/2")

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

BODY = {
    "transaction_ref": "TXN-WIRED-1",
    "customer_ref": "usr_9931",
    "amount_minor": 75_000_000,
    "currency": "NGN",
    "transaction_type": "transfer",
    "customer": {"kyc_tier": 2},
}


@pytest.fixture(autouse=True)
def decision_settings(settings):
    """The decision workload's own settings — not a test-shaped approximation."""
    settings.ROOT_URLCONF = "server.urls"
    settings.MIDDLEWARE = [
        "django.middleware.security.SecurityMiddleware",
        "django.middleware.common.CommonMiddleware",
        "complylayer.api.decision_middleware.DecisionMiddleware",
    ]
    # No polling thread. Propagation has its own test in
    # `tests/test_ruleset_cache.py`, which severs pub/sub entirely and requires
    # the poll to carry the change; here a thread would only be reading a
    # database this test is about to roll back.
    settings.COMPLYLAYER = {**settings.COMPLYLAYER, "WATCH_RULESET_VERSIONS": False}

    auth.clear_cache()
    wiring.shutdown()
    yield settings
    auth.clear_cache()
    wiring.shutdown()


@pytest.fixture
def tenant_with_a_live_rule():
    tenant = Tenant.objects.create(id="tnt_wired", name="Wired")
    partitions.ensure_partitions(datetime.now(UTC).date(), months_ahead=0)

    full_key, prefix = auth.generate_key("live")
    ApiKey.objects.create(
        id="key_wired",
        tenant=tenant,
        name="wired",
        prefix=prefix,
        hashed_secret=auth.hash_secret(full_key),
        role=Role.COMPLIANCE_OFFICER,
        created_by="setup",
    )

    analyst = Actor(id="usr_analyst", role=Role.COMPLIANCE_ANALYST)
    officer = Actor(id="usr_officer", role=Role.COMPLIANCE_OFFICER)
    rule = rules.create_draft(
        tenant_id=tenant.id,
        actor=analyst,
        name="Tier 2 single transaction limit",
        category="kyc",
        expression="amount_minor > 50_000_000",
        severity="block",
        priority=10,
        regulatory_reference="CBN KYC Tier 2",
        customer_message="This transfer is above your tier 2 limit.",
    )
    rules.approve(rule=rule, actor=officer)
    rules.activate(rule=rule, actor=officer)
    return tenant, full_key


def client_for(key: str) -> Client:
    return Client(headers={"authorization": f"Bearer {key}"})


def post(client: Client, body=None, key: str = "TXN-WIRED-1"):
    return client.post(
        "/v1/decisions",
        data=orjson.dumps(body or BODY),
        content_type="application/json",
        headers={"idempotency-key": key},
    )


class TestTheEndpointActuallyServes:
    def test_a_decision_is_returned_with_nothing_attached_by_hand(self, tenant_with_a_live_rule):
        """The one that would have caught it."""
        _, key = tenant_with_a_live_rule
        response = post(client_for(key))

        assert response.status_code == 200, response.content[:400]
        body = orjson.loads(response.content)
        assert body["outcome"] == "block"
        assert body["reason"] == "Tier 2 single transaction limit"
        assert body["ruleset_version"] == 1
        assert body["customer_message"] == "This transfer is above your tier 2 limit."

    def test_the_rule_set_comes_from_the_published_version(self, tenant_with_a_live_rule):
        """Not from a rule set the test handed it — the middleware loads the
        snapshot the management API published."""
        _, key = tenant_with_a_live_rule
        body = orjson.loads(post(client_for(key)).content)
        assert body["evaluated_rules"] == 1

    def test_an_allowed_transaction_is_allowed(self, tenant_with_a_live_rule):
        _, key = tenant_with_a_live_rule
        body = orjson.loads(post(client_for(key), {**BODY, "amount_minor": 1_000_000}).content)
        assert body["outcome"] == "allow"
        assert body["degraded"] is False

    def test_the_decision_is_persisted(self, tenant_with_a_live_rule):
        """The middleware wires the real store, not the in-memory one."""
        _, key = tenant_with_a_live_rule
        body = orjson.loads(post(client_for(key)).content)

        stored = Decision.objects.get(id=body["decision_id"])
        assert stored.outcome == "block"
        assert stored.resolved_facts["amount_minor"] == 75_000_000
        assert stored.customer_ref_hash != "usr_9931"

    def test_a_retry_replays_the_original_through_the_database(self, tenant_with_a_live_rule):
        _, key = tenant_with_a_live_rule
        first = orjson.loads(post(client_for(key)).content)
        second = orjson.loads(post(client_for(key)).content)

        assert first == second
        assert Decision.objects.filter(id=first["decision_id"]).count() == 1


class TestTheMiddlewareRefusesCleanly:
    def test_no_key_is_refused_before_anything_is_built(self, tenant_with_a_live_rule):
        assert (
            Client()
            .post(
                "/v1/decisions",
                data=b"{}",
                content_type="application/json",
                headers={"idempotency-key": "k"},
            )
            .status_code
            == 401
        )

    def test_an_unknown_key_is_refused(self, tenant_with_a_live_rule):
        assert post(client_for("cl_live_notarealkey")).status_code == 401

    def test_a_tenant_with_no_published_ruleset_gets_an_explanation(self):
        """Not a 500. A tenant that has not activated a rule yet is a normal
        state on somebody's first integration test."""
        tenant = Tenant.objects.create(id="tnt_empty", name="Empty")
        full_key, prefix = auth.generate_key("live")
        ApiKey.objects.create(
            id="key_empty",
            tenant=tenant,
            name="empty",
            prefix=prefix,
            hashed_secret=auth.hash_secret(full_key),
            role=Role.COMPLIANCE_OFFICER,
            created_by="setup",
        )

        response = post(client_for(full_key))
        assert response.status_code == 409
        body = orjson.loads(response.content)
        assert body["error"] == "no_ruleset"
        assert "Activate a rule" in body["message"]


class TestTheProbesWorkWithoutAKey:
    def test_healthz_needs_nothing(self):
        assert Client().get("/healthz").status_code == 200

    def test_readyz_is_not_ready_before_any_cache_is_warm(self):
        """It must not build the cache it was asked to check — a probe that
        warms the thing it measures always reports ready."""
        assert Client().get("/readyz").status_code == 503

    def test_readyz_reports_ready_once_a_decision_has_warmed_a_cache(self, tenant_with_a_live_rule):
        _, key = tenant_with_a_live_rule
        post(client_for(key))
        assert Client().get("/readyz").status_code == 200

    def test_the_version_gauge_is_published(self, tenant_with_a_live_rule):
        _, key = tenant_with_a_live_rule
        post(client_for(key))

        body = Client().get("/metrics").content.decode()
        assert "complylayer_ruleset_version" in body
        assert 'tenant="tnt_wired"' in body
        assert 'worker="' in body, "labelled per worker, not per pod (D12)"


class TestTenantIsolationOnTheDecisionPath:
    def test_a_key_only_ever_decides_for_its_own_tenant(self, tenant_with_a_live_rule):
        """The other tenant's rule set is stricter. If the wrong one were loaded,
        the outcome would change — which makes this a test of isolation rather
        than of configuration."""
        other = Tenant.objects.create(id="tnt_other_wired", name="Other")
        analyst = Actor(id="usr_a2", role=Role.COMPLIANCE_ANALYST)
        officer = Actor(id="usr_o2", role=Role.COMPLIANCE_OFFICER)
        strict = rules.create_draft(
            tenant_id=other.id,
            actor=analyst,
            name="Everything blocks",
            category="kyc",
            expression="amount_minor > 1",
            severity="block",
            priority=1,
        )
        rules.approve(rule=strict, actor=officer)
        rules.activate(rule=strict, actor=officer)

        _, key = tenant_with_a_live_rule
        body = orjson.loads(post(client_for(key), {**BODY, "amount_minor": 1_000_000}).content)

        assert body["outcome"] == "allow", "the other tenant's rule set decided this request"


@pytest.mark.integration
@pytest.mark.django_db
class TestVelocityReachesTheRulesThroughTheProductionSeam:
    """A velocity rule, evaluated the way production builds the handler.

    `DecisionHandler` takes either `velocity=` (one provider, which suits a
    test) or `velocity_factory=` (one per customer, which is what the middleware
    passes because Redis keys are scoped per customer). Every test used the
    first. Production only ever uses the second.

    `_gather` returned `self.velocity` — the constructor argument — rather than
    the provider it had just resolved from the factory. So in production it
    returned None, `functions.build(None, now)` bound the velocity functions to
    nothing, and every velocity rule raised `'NoneType' object has no attribute
    'count'`: a 500 on structuring and transaction-velocity rules, which are
    most of what this product is for.

    958 tests passed throughout. `make demo` caught it on its first real run.
    """

    def test_a_velocity_rule_decides_rather_than_raising(self):
        from complylayer.api.handler import DecisionHandler
        from complylayer.api.store import InMemoryStore
        from complylayer.api.validation import Transaction
        from complylayer.dsl import validate_source
        from complylayer.engine import CompiledRule, RuleSet, Severity
        from complylayer.velocity import RedisVelocity

        client = redis.Redis.from_url(REDIS_URL)
        client.flushdb()

        ruleset = RuleSet(
            1,
            (
                CompiledRule(
                    "rul_vel",
                    "More than two an hour",
                    validate_source("velocity_count(window='1h') > 2"),
                    Severity.FLAG,
                ),
            ),
        )

        # Built exactly as DecisionMiddleware builds it: a factory, no provider.
        handler = DecisionHandler(
            tenant_id="tnt_seam",
            ruleset=ruleset,
            store=InMemoryStore(),
            velocity_factory=lambda customer_hash: RedisVelocity(client, "tnt_seam", customer_hash),
            salt="a-real-salt",
        )

        def send(ref: str):
            return handler.decide(
                Transaction(
                    transaction_ref=ref,
                    customer_ref="usr_seam",
                    amount_minor=10_000,
                    currency="NGN",
                    transaction_type="transfer",
                ),
                ref,
            )

        for ref in ("T1", "T2"):
            body = send(ref)
            assert body["outcome"] == "allow", body
            assert not body["_errored_rules"], body["_errored_rules"]

        third = send("T3")
        assert third["outcome"] == "flag", third
        assert [r["id"] for r in third["matched_rules"]] == ["rul_vel"]
        assert not third["degraded"]

    def test_the_provider_is_the_one_the_factory_returned(self):
        """A guard on the shape of the fix. `_gather` must hand back the
        resolved provider, never the constructor argument."""
        from complylayer.api.handler import DecisionHandler
        from complylayer.api.store import InMemoryStore
        from complylayer.api.validation import Transaction
        from complylayer.engine import RuleSet
        from complylayer.velocity import RedisVelocity

        client = redis.Redis.from_url(REDIS_URL)
        client.flushdb()

        built = []

        def factory(customer_hash):
            provider = RedisVelocity(client, "tnt_seam2", customer_hash)
            built.append(provider)
            return provider

        handler = DecisionHandler(
            tenant_id="tnt_seam2",
            ruleset=RuleSet(1, ()),
            store=InMemoryStore(),
            velocity_factory=factory,
            salt="a-real-salt",
        )
        transaction = Transaction(
            transaction_ref="T1",
            customer_ref="usr_seam",
            amount_minor=10_000,
            currency="NGN",
            transaction_type="transfer",
        )

        assert handler.velocity is None, "production passes no provider directly"

        # Through `decide`, because the factory is resolved in `_evaluate`.
        # Calling `_gather` on its own reaches past the seam being tested.
        handler.decide(transaction, "T1")

        assert built, "the factory was never called"
        assert handler._provider is built[0], (
            "the handler evaluated against something other than the factory's provider"
        )
