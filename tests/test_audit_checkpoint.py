"""The anchor, and the attack it exists to catch.

§8.3 lists four mechanisms and the README states the hole in them plainly: the
hash chain is unkeyed SHA-256, so an attacker with write access can edit a record
and recompute every hash after it. The result verifies perfectly against
`verify_chain` — it is a correct chain, of the wrong history.

`test_a_rewritten_chain_still_verifies_but_fails_its_anchor` is the whole point
of this file: it performs that rewrite, confirms the chain check is fooled, and
confirms the signature is not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from complylayer import audit
from complylayer.audit import Anchoring
from complylayer.audit import checkpoint as cp
from complylayer.models import AuditRecord, Tenant

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def keys():
    return cp.generate_key()


@pytest.fixture
def tenant():
    return Tenant.objects.create(id="tnt_anchor", name="Anchor")


@pytest.fixture
def unprotected_audit_table():
    """Model an attacker who is not going through the application.

    The append-only trigger refuses UPDATE and DELETE, superusers included, so
    simulating tampering through Django hits that wall first. That wall is not
    what the checkpoint defends. The checkpoint defends the case where the
    database itself was bypassed: a restored backup, a replica edited at file
    level, a dump reloaded with triggers off. Migration 0002's docstring names
    this exact escape hatch for non-production use.

    A fixture rather than a context manager inside the test, because Postgres
    refuses `ALTER TABLE` once a transaction has pending trigger events — so the
    disable has to happen before the test writes anything.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE complylayer_auditrecord DISABLE TRIGGER USER")
    yield
    # Deliberately no re-enable. `ALTER TABLE` is transactional in Postgres and
    # this test runs inside the transaction pytest-django rolls back, so the
    # trigger is restored by that rollback. Trying to re-enable here fails with
    # "cannot ALTER TABLE because it has pending trigger events" — the same
    # refusal that made this a fixture rather than a context manager.
    #
    # `test_the_trigger_is_back` below is the check on that reasoning: if the
    # rollback ever stopped restoring it, the rest of the suite would start
    # accepting writes to the audit trail and nothing else would say so.


def append_some(tenant_id: str, count: int = 4) -> None:
    for index in range(count):
        audit.append(
            tenant_id=tenant_id,
            event_type="rule.created",
            actor={"type": "user", "id": "ada@demo.ng"},
            subject={"type": "rule", "id": f"rul_{index}"},
            payload={"n": index},
        )


class TestSigningAChainHead:
    def test_a_fresh_chain_verifies_as_anchored(self, tenant, keys):
        private, public = keys
        append_some(tenant.id)
        audit.write_checkpoint(tenant_id=tenant.id, private_pem=private)

        result = audit.verify_anchoring(tenant_id=tenant.id, public_pem=public)
        assert result.anchoring is Anchoring.SIGNED
        assert result.ok
        assert result.chain_length == 4

    def test_an_empty_chain_is_not_signed_at_all(self, tenant, keys):
        """A signature over nothing is a signature an auditor could mistake for
        a guarantee."""
        private, _ = keys
        assert audit.write_checkpoint(tenant_id=tenant.id, private_pem=private) is None

    def test_one_signature_covers_everything_before_it(self, tenant, keys):
        """The head hash already covers the whole chain, so signing each record
        would buy the same guarantee thousands of times over."""
        from complylayer.models import AuditCheckpoint

        private, _ = keys
        append_some(tenant.id, 50)
        audit.write_checkpoint(tenant_id=tenant.id, private_pem=private)

        assert AuditCheckpoint.objects.filter(tenant=tenant).count() == 1
        assert AuditCheckpoint.objects.get(tenant=tenant).chain_length == 50


class TestWhatTheAnchorActuallyCatches:
    def test_a_rewritten_chain_still_verifies_but_fails_its_anchor(
        self, tenant, keys, unprotected_audit_table
    ):
        """The attack the checkpoint exists for.

        Edit a record, then recompute every hash after it. `verify_chain` is
        satisfied — it is a valid chain. The signature is not, because it was
        made with a key that is not in the database.
        """
        private, public = keys
        append_some(tenant.id, 4)
        audit.write_checkpoint(tenant_id=tenant.id, private_pem=private)

        assert audit.verify(tenant.id).ok
        assert audit.verify_anchoring(tenant_id=tenant.id, public_pem=public).ok

        # The rewrite. Change a payload, then repair the chain behind it exactly
        # as somebody with write access would.
        records = list(AuditRecord.objects.filter(tenant=tenant).order_by("recorded_at", "id"))
        records[1].payload = {"n": 1, "tampered": True}
        previous = records[0].hash
        for record in records[1:]:
            record.prev_hash = previous
            record.hash = audit.compute_hash(
                record_id=record.id,
                tenant_id=record.tenant_id,
                event_type=record.event_type,
                occurred_at=record.occurred_at,
                actor=record.actor,
                subject=record.subject,
                payload=record.payload,
                prev_hash=record.prev_hash,
            )
            previous = record.hash
            AuditRecord.objects.filter(id=record.id).update(
                payload=record.payload, prev_hash=record.prev_hash, hash=record.hash
            )

        assert audit.verify(tenant.id).ok, (
            "the rewrite should produce a chain that is internally consistent — "
            "that is the whole reason an anchor is needed"
        )

        anchored = audit.verify_anchoring(tenant_id=tenant.id, public_pem=public)
        assert anchored.anchoring is Anchoring.BROKEN
        assert not anchored.ok
        assert "rewritten" in anchored.detail

    def test_truncating_the_chain_back_to_a_signed_head_is_caught(
        self, tenant, keys, unprotected_audit_table
    ):
        """Without the length in the signed payload, an attacker could delete
        everything after an old checkpoint and present it as current."""
        private, public = keys
        append_some(tenant.id, 6)
        audit.write_checkpoint(tenant_id=tenant.id, private_pem=private)

        keep = list(AuditRecord.objects.filter(tenant=tenant).order_by("recorded_at", "id"))[:3]
        AuditRecord.objects.filter(tenant=tenant).exclude(id__in=[r.id for r in keep]).delete()

        anchored = audit.verify_anchoring(tenant_id=tenant.id, public_pem=public)
        assert anchored.anchoring is Anchoring.BROKEN
        assert "deleted" in anchored.detail

    def test_a_signature_from_the_wrong_key_is_refused(self, tenant, keys):
        private, _ = keys
        _, other_public = cp.generate_key()
        append_some(tenant.id)
        audit.write_checkpoint(tenant_id=tenant.id, private_pem=private)

        anchored = audit.verify_anchoring(tenant_id=tenant.id, public_pem=other_public)
        assert anchored.anchoring is Anchoring.BROKEN
        assert "does not verify" in anchored.detail


class TestItRefusesToPretend:
    def test_no_key_configured_reports_unanchored_rather_than_ok(self, tenant):
        """A verification that cannot fail means nothing, and this answer goes
        to a customer's auditor."""
        append_some(tenant.id)
        result = audit.verify_anchoring(tenant_id=tenant.id, public_pem="")

        assert result.anchoring is Anchoring.UNANCHORED
        assert not result.ok, "unanchored must not read as ok"
        assert "recomputes every hash" in result.detail

    def test_a_key_with_no_checkpoint_yet_says_so(self, tenant, keys):
        _, public = keys
        append_some(tenant.id)
        result = audit.verify_anchoring(tenant_id=tenant.id, public_pem=public)

        assert result.anchoring is Anchoring.UNANCHORED
        assert "no checkpoint has been written" in result.detail

    def test_the_interval_is_the_window_and_it_is_not_hidden(self, tenant, keys):
        """A tamper after the last checkpoint is invisible until the next one.
        That is a real limit; this test states it so nobody discovers it during
        an audit."""
        private, public = keys
        append_some(tenant.id, 3)
        audit.write_checkpoint(tenant_id=tenant.id, private_pem=private)

        # Everything appended afterwards is unsigned until the next run.
        append_some(tenant.id, 2)
        result = audit.verify_anchoring(tenant_id=tenant.id, public_pem=public)

        assert result.anchoring is Anchoring.SIGNED
        assert result.chain_length == 3, "only what was signed is covered"
        assert AuditRecord.objects.filter(tenant=tenant).count() == 5


class TestTheCheckpointItselfCannotBeRewritten:
    def test_updating_a_checkpoint_is_refused_by_the_database(self, tenant, keys):
        from django.db import IntegrityError, transaction

        from complylayer.models import AuditCheckpoint

        private, _ = keys
        append_some(tenant.id)
        audit.write_checkpoint(tenant_id=tenant.id, private_pem=private)

        with pytest.raises(IntegrityError), transaction.atomic():
            AuditCheckpoint.objects.filter(tenant=tenant).update(head_hash="sha256:" + "0" * 64)

    def test_deleting_a_checkpoint_is_refused_by_the_database(self, tenant, keys):
        from django.db import IntegrityError, transaction

        from complylayer.models import AuditCheckpoint

        private, _ = keys
        append_some(tenant.id)
        audit.write_checkpoint(tenant_id=tenant.id, private_pem=private)

        with pytest.raises(IntegrityError), transaction.atomic():
            AuditCheckpoint.objects.filter(tenant=tenant).delete()


class TestTheCanonicalPayload:
    def test_the_signed_bytes_do_not_depend_on_dict_ordering(self):
        moment = datetime(2026, 8, 1, tzinfo=UTC)
        first = cp.canonical_checkpoint(
            tenant_id="t", chain_length=3, head_hash="h", signed_at=moment
        )
        second = cp.canonical_checkpoint(
            signed_at=moment, head_hash="h", chain_length=3, tenant_id="t"
        )
        assert first == second

    def test_the_length_is_part_of_what_is_signed(self):
        moment = datetime(2026, 8, 1, tzinfo=UTC)
        assert cp.canonical_checkpoint(
            tenant_id="t", chain_length=3, head_hash="h", signed_at=moment
        ) != cp.canonical_checkpoint(tenant_id="t", chain_length=4, head_hash="h", signed_at=moment)

    def test_one_tenants_signature_does_not_verify_for_another(self):
        private, public = cp.generate_key()
        moment = datetime(2026, 8, 1, tzinfo=UTC) + timedelta(hours=1)
        signature = cp.sign(
            private, tenant_id="tnt_a", chain_length=1, head_hash="h", signed_at=moment
        )

        assert cp.verify_signature(
            public, signature, tenant_id="tnt_a", chain_length=1, head_hash="h", signed_at=moment
        )
        assert not cp.verify_signature(
            public, signature, tenant_id="tnt_b", chain_length=1, head_hash="h", signed_at=moment
        )


@pytest.mark.django_db
def test_the_trigger_is_back_after_the_tampering_tests():
    """The fixture above disables the append-only trigger and never re-enables
    it, on the reasoning that the test transaction's rollback does.

    If that reasoning is ever wrong, the audit trail silently becomes writable
    for every test that runs afterwards — which is precisely the class of
    failure this project keeps finding. So it is checked rather than assumed.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname, tgenabled FROM pg_trigger "
            "WHERE tgrelid = 'complylayer_auditrecord'::regclass AND NOT tgisinternal"
        )
        states = dict(cursor.fetchall())

    assert states, "the append-only triggers are missing entirely"
    assert all(state == "O" for state in states.values()), (
        f"an audit trigger is still disabled after the tampering tests: {states}. "
        "The rollback did not restore it, and the trail is writable."
    )
