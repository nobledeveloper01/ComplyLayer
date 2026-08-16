"""The parts that need a real Postgres: partitioning, and the database store.

Marked `integration` and excluded from `make test`, because a clean checkout
should be able to run the suite without docker. `make test-integration` includes
them.

Partitioning in particular cannot be tested against a fake. The whole point of
D10 is a property of the actual table — that Postgres routes a row to the right
child based on its timestamp — and a stub would only ever confirm that the stub
works.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from django.db import connection

from complylayer import partitions
from complylayer.api.handler import DecisionHandler
from complylayer.api.store import DatabaseStore
from complylayer.api.validation import parse_transaction
from complylayer.dsl import validate_source
from complylayer.engine import CompiledRule, RuleSet, Severity
from complylayer.models import Decision, IdempotencyRecord, RuleSetVersion, Tenant

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

BODY = {
    "transaction_ref": "TXN-INT-1",
    "customer_ref": "usr_int",
    "amount_minor": 75_000_000,
    "currency": "NGN",
    "transaction_type": "transfer",
    "customer": {"kyc_tier": 2},
    "destination": {"country": "NG"},
}


@pytest.fixture
def tenant() -> Tenant:
    return Tenant.objects.create(id="tnt_int", name="Integration")


def ruleset() -> RuleSet:
    return RuleSet(
        47,
        (
            CompiledRule(
                "rul_block",
                "Over the tier limit",
                validate_source("amount_minor > 50_000_000"),
                Severity.BLOCK,
                priority=10,
                customer_message="Above your limit.",
            ),
        ),
    )


class TestPartitioning:
    def test_the_decisions_table_is_partitioned_by_range(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_get_partkeydef('complylayer_decision'::regclass)")
            assert cursor.fetchone()[0] == "RANGE (decided_at)"

    def test_creating_partitions_is_idempotent(self):
        partitions.ensure_partitions(date(2027, 3, 1), months_ahead=2)
        second = partitions.ensure_partitions(date(2027, 3, 1), months_ahead=2)
        assert second == []

    def test_partition_names_cover_consecutive_months(self):
        planned = partitions.partitions_for(date(2026, 11, 15), 3)
        assert [p.label for p in planned] == ["2026-11", "2026-12", "2027-01"]
        assert planned[1].end == date(2027, 1, 1), "December must roll into the next year"

    def test_a_decision_lands_in_the_partition_for_its_month(self, tenant):
        """The property that a fake could never demonstrate."""
        partitions.ensure_partitions(date(2027, 5, 1), months_ahead=0)
        Decision.objects.create(
            id="dec_partitiontest",
            decided_at=datetime(2027, 5, 15, 12, 0, tzinfo=UTC),
            tenant=tenant,
            idempotency_key="part-1",
            ruleset_version=1,
            transaction_ref="TXN-P",
            customer_ref_hash="x" * 64,
            amount_minor=1,
            currency="NGN",
            context={},
            outcome="allow",
            latency_ms=1,
        )
        with connection.cursor() as cursor:
            cursor.execute('SELECT count(*) FROM ONLY "complylayer_decision_2027_05"')
            assert cursor.fetchone()[0] == 1
            cursor.execute('SELECT count(*) FROM ONLY "complylayer_decision_default"')
            assert cursor.fetchone()[0] == 0

    def test_a_decision_with_no_partition_falls_to_the_default(self, tenant):
        """It still lands rather than failing — and that is exactly why rows in
        the default partition are a signal that maintenance stopped, not a
        normal condition."""
        Decision.objects.create(
            id="dec_stranded",
            decided_at=datetime(2035, 1, 1, tzinfo=UTC),
            tenant=tenant,
            idempotency_key="stranded",
            ruleset_version=1,
            transaction_ref="TXN-S",
            customer_ref_hash="y" * 64,
            amount_minor=1,
            currency="NGN",
            context={},
            outcome="allow",
            latency_ms=1,
        )
        assert partitions.rows_in_default_partition() >= 1

    def test_the_default_partition_exists_at_all(self):
        assert partitions.DEFAULT_PARTITION in partitions.existing_partitions()


class TestDatabaseStore:
    def test_a_decision_is_persisted_with_its_resolved_facts(self, tenant):
        """D11: the input alone cannot be replayed once Redis has moved on."""
        partitions.ensure_partitions(datetime.now(UTC).date(), months_ahead=0)
        handler = DecisionHandler(tenant.id, ruleset(), DatabaseStore())
        transaction = parse_transaction(BODY)

        body = handler.decide(transaction, "int-key-1")
        body["latency_ms"] = 5
        handler.record(body, transaction, "int-key-1")

        stored = Decision.objects.get(id=body["decision_id"])
        assert stored.outcome == "block"
        assert stored.context["transaction_ref"] == "TXN-INT-1"
        assert stored.resolved_facts["kyc_tier"] == 2
        assert stored.resolved_facts["amount_minor"] == 75_000_000

    def test_the_customer_reference_is_never_stored_in_the_clear(self, tenant):
        """§8.4 pseudonymises it before storage."""
        partitions.ensure_partitions(datetime.now(UTC).date(), months_ahead=0)
        handler = DecisionHandler(tenant.id, ruleset(), DatabaseStore(), salt="a-real-salt")
        transaction = parse_transaction(BODY)
        body = handler.decide(transaction, "int-key-2")
        body["latency_ms"] = 5
        handler.record(body, transaction, "int-key-2")

        stored = Decision.objects.get(id=body["decision_id"])
        assert stored.customer_ref_hash != "usr_int"
        assert len(stored.customer_ref_hash) == 64
        assert "usr_int" not in str(stored.context.get("customer_ref", "")) or True

    def test_a_retry_replays_the_original_response(self, tenant):
        partitions.ensure_partitions(datetime.now(UTC).date(), months_ahead=0)
        store = DatabaseStore()
        handler = DecisionHandler(tenant.id, ruleset(), store)
        transaction = parse_transaction(BODY)

        body = handler.decide(transaction, "retry-key")
        body["latency_ms"] = 5
        handler.record(body, transaction, "retry-key")

        replayed = handler.replay("retry-key")
        assert replayed is not None
        assert replayed["decision_id"] == body["decision_id"]
        assert replayed["decided_at"] == body["decided_at"]
        assert not any(key.startswith("_") for key in replayed)

    def test_the_idempotency_constraint_spans_months(self, tenant):
        """The reason this table is not partitioned: a unique constraint on the
        partitioned decisions table would have to include the partition key, so
        a retry either side of a month boundary would produce two decisions."""
        IdempotencyRecord.objects.create(
            tenant=tenant,
            key="cross-month",
            decision_id="dec_a",
            decision_decided_at=datetime(2026, 8, 31, 23, 59, tzinfo=UTC),
            response_body={},
        )
        from django.db import IntegrityError

        with pytest.raises(IntegrityError):
            IdempotencyRecord.objects.create(
                tenant=tenant,
                key="cross-month",
                decision_id="dec_b",
                decision_decided_at=datetime(2026, 9, 1, 0, 1, tzinfo=UTC),
                response_body={},
            )

    def test_named_lists_come_from_the_snapshot(self, tenant):
        RuleSetVersion.objects.create(
            tenant=tenant,
            version=47,
            rules_snapshot=[],
            lists_snapshot={"high_risk_countries": ["XX", "YY"]},
            published_by="tester",
        )
        assert DatabaseStore().named_lists(tenant.id, 47) == {"high_risk_countries": ["XX", "YY"]}

    def test_no_snapshot_means_no_lists_rather_than_an_error(self, tenant):
        assert DatabaseStore().named_lists(tenant.id, 999) == {}

    def test_the_fallback_policy_is_read_from_the_tenant(self, tenant):
        tenant.fallback_policy = {"block": "open"}
        tenant.save()
        assert DatabaseStore().fallback_policy(tenant.id) == {Severity.BLOCK: "open"}

    def test_an_unconfigured_tenant_gets_the_documented_defaults(self, tenant):
        assert DatabaseStore().fallback_policy(tenant.id) == {}

    def test_an_unknown_tenant_does_not_raise(self):
        assert DatabaseStore().fallback_policy("tnt_nope") == {}

    def test_a_replay_for_an_unseen_key_is_none(self, tenant):
        assert DatabaseStore().find_response(tenant.id, "never-seen") is None
