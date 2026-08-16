"""The audit trail, and the four independent things that make it evidence.

Each layer covers a case the one before it does not, so each is tested on its
own. A test suite that only proves "the application does not call save()" would
pass on a system where anyone with a psql prompt can rewrite history.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.db import IntegrityError, connection, transaction

from complylayer import audit
from complylayer.audit.chain import GENESIS, compute_hash, verify_chain
from complylayer.models import AuditRecord, Tenant

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

ACTOR = {"type": "user", "id": "usr_adaeze", "ip": "10.0.0.1"}


@pytest.fixture
def tenant() -> Tenant:
    return Tenant.objects.create(id="tnt_audit", name="Audit")


def append(tenant, event_type="rule.activated", **overrides):
    return audit.append(
        tenant_id=tenant.id,
        event_type=event_type,
        actor=overrides.get("actor", ACTOR),
        subject=overrides.get("subject", {"type": "rule", "id": "rul_1"}),
        payload=overrides.get("payload", {}),
    )


class TestTheChain:
    def test_the_first_record_follows_the_genesis_hash(self, tenant):
        record = append(tenant)
        assert record.prev_hash == GENESIS
        assert record.hash.startswith("sha256:")

    def test_each_record_links_to_the_one_before(self, tenant):
        first = append(tenant)
        second = append(tenant, "rule.approved")
        third = append(tenant, "rule.archived")

        assert second.prev_hash == first.hash
        assert third.prev_hash == second.hash

    def test_a_clean_chain_verifies(self, tenant):
        for _ in range(10):
            append(tenant)
        result = audit.verify(tenant.id)
        assert result.ok is True
        assert result.checked == 10

    def test_an_empty_chain_verifies(self, tenant):
        assert audit.verify(tenant.id).ok is True

    def test_chains_are_per_tenant(self, tenant):
        """One global chain would serialise every tenant's writes behind each
        other, and would leak the fact of one tenant's activity into another's
        verification."""
        other = Tenant.objects.create(id="tnt_other", name="Other")
        append(tenant)
        first_of_other = append(other)

        assert first_of_other.prev_hash == GENESIS
        assert audit.verify(tenant.id).ok
        assert audit.verify(other.id).ok


class TestTamperDetection:
    """What catches an edit made outside the database — a restored backup, an
    altered replica, a file-level change."""

    def test_changing_a_payload_breaks_the_chain(self, tenant):
        append(tenant)
        target = append(tenant, payload={"threshold": 5_000_000})
        append(tenant)

        # Bypasses the ORM *and* the trigger, standing in for tampering that
        # happened somewhere other than this database.
        records = list(AuditRecord.objects.filter(tenant_id=tenant.id).order_by("recorded_at"))
        records[1].payload = {"threshold": 50_000_000}

        result = verify_chain(records)
        assert result.ok is False
        assert result.broken_at == target.id
        assert "changed after it was written" in result.detail

    def test_removing_a_record_breaks_the_chain(self, tenant):
        append(tenant)
        append(tenant)
        third = append(tenant)

        records = list(AuditRecord.objects.filter(tenant_id=tenant.id).order_by("recorded_at"))
        result = verify_chain([records[0], records[2]])

        assert result.ok is False
        assert result.broken_at == third.id

    def test_reordering_records_breaks_the_chain(self, tenant):
        append(tenant)
        append(tenant)
        records = list(AuditRecord.objects.filter(tenant_id=tenant.id).order_by("recorded_at"))
        assert verify_chain(list(reversed(records))).ok is False

    def test_the_first_broken_record_is_named(self, tenant):
        """A verification that says "something is wrong" without saying where is
        one nobody can act on — and this output goes to a customer's auditor."""
        for _ in range(5):
            append(tenant)
        records = list(AuditRecord.objects.filter(tenant_id=tenant.id).order_by("recorded_at"))
        records[2].actor = {"type": "user", "id": "someone_else"}

        result = verify_chain(records)
        assert result.broken_at == records[2].id
        assert result.checked == 2, "it stops at the first break rather than guessing further"


class TestDatabaseEnforcement:
    """Grants protect against accident. The trigger protects against authority."""

    def test_an_update_is_refused_by_the_database(self, tenant):
        record = append(tenant)
        with pytest.raises(IntegrityError) as exc, transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE complylayer_auditrecord SET payload = %s WHERE id = %s",
                    ['{"tampered": true}', record.id],
                )
        assert "append-only" in str(exc.value)

    def test_a_delete_is_refused_by_the_database(self, tenant):
        record = append(tenant)
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM complylayer_auditrecord WHERE id = %s", [record.id])

    def test_a_truncate_is_refused_too(self):
        """TRUNCATE bypasses row-level triggers entirely. Without its own
        statement trigger, one command empties the whole evidence trail.

        No insert first, deliberately: Postgres refuses TRUNCATE outright on a
        table with pending trigger events, so an insert in this transaction
        would make the test pass for a reason that is not the trigger.
        """
        with pytest.raises(IntegrityError) as exc, transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("TRUNCATE complylayer_auditrecord")
        assert "append-only" in str(exc.value)

    def test_the_trigger_is_installed_on_both_events(self):
        """`complylayer_doctor` checks this on a live deployment; this checks it
        on every migration path."""
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT tgname FROM pg_trigger
                WHERE tgrelid = 'complylayer_auditrecord'::regclass AND NOT tgisinternal
                ORDER BY tgname
                """
            )
            names = [row[0] for row in cursor.fetchall()]
        assert "complylayer_audit_append_only" in names
        assert "complylayer_audit_no_truncate" in names

    def test_the_refusal_explains_what_to_do_instead(self, tenant):
        record = append(tenant)
        with pytest.raises(IntegrityError) as exc, transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM complylayer_auditrecord WHERE id = %s", [record.id])
        assert "corrects" in str(exc.value), "the error should point at the correction mechanism"

    def test_inserting_still_works(self, tenant):
        """Append-only means append-only, not read-only."""
        append(tenant)
        assert AuditRecord.objects.filter(tenant_id=tenant.id).count() == 1


class TestCorrections:
    def test_a_correction_is_appended_and_the_original_stays(self, tenant):
        wrong = append(tenant, payload={"threshold": 1})
        correction = audit.correct(
            tenant_id=tenant.id,
            corrects_id=wrong.id,
            actor=ACTOR,
            reason="threshold was recorded in major units",
        )

        assert correction.payload["corrects"] == wrong.id
        assert AuditRecord.objects.filter(id=wrong.id).exists(), "the original must survive"
        assert audit.verify(tenant.id).ok is True

    def test_a_correction_carries_its_reason(self, tenant):
        wrong = append(tenant)
        correction = audit.correct(
            tenant_id=tenant.id, corrects_id=wrong.id, actor=ACTOR, reason="wrong actor recorded"
        )
        assert correction.payload["reason"] == "wrong actor recorded"
        assert correction.event_type == "audit.corrected"


class TestHashStability:
    def test_the_hash_does_not_depend_on_dictionary_ordering(self):
        """A chain whose input depends on dict ordering verifies today and fails
        after a Python upgrade."""
        common = {
            "record_id": "aud_1",
            "tenant_id": "tnt_1",
            "event_type": "rule.activated",
            "occurred_at": datetime(2026, 8, 16, tzinfo=UTC),
            "prev_hash": GENESIS,
            "subject": {"type": "rule", "id": "rul_1"},
        }
        one = compute_hash(**common, actor={"a": 1, "b": 2}, payload={"x": 1, "y": 2})
        two = compute_hash(**common, actor={"b": 2, "a": 1}, payload={"y": 2, "x": 1})
        assert one == two

    def test_recorded_at_is_deliberately_outside_the_hash(self, tenant):
        """It is when the row was written, which a replica or a restore can
        legitimately differ on. Including it would make the chain fail for
        reasons that are not tampering."""
        record = append(tenant)
        recomputed = compute_hash(
            record_id=record.id,
            tenant_id=record.tenant_id,
            event_type=record.event_type,
            occurred_at=record.occurred_at,
            actor=record.actor,
            subject=record.subject,
            payload=record.payload,
            prev_hash=record.prev_hash,
        )
        assert recomputed == record.hash

    def test_any_field_change_changes_the_hash(self):
        base = {
            "record_id": "aud_1",
            "tenant_id": "tnt_1",
            "event_type": "rule.activated",
            "occurred_at": datetime(2026, 8, 16, tzinfo=UTC),
            "actor": {"id": "a"},
            "subject": {"id": "s"},
            "payload": {"p": 1},
            "prev_hash": GENESIS,
        }
        original = compute_hash(**base)
        for field, value in [
            ("event_type", "rule.archived"),
            ("actor", {"id": "b"}),
            ("payload", {"p": 2}),
            ("prev_hash", "sha256:" + "1" * 64),
        ]:
            assert compute_hash(**{**base, field: value}) != original, f"{field} must matter"
