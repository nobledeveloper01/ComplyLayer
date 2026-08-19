"""Appending to a tenant's audit chain.

Every append is serialised per tenant, because a hash chain is by definition a
sequence and two concurrent writers computing `prev_hash` from the same tail
would produce a fork. The lock is a row lock on the tenant, which costs nothing
on the management path — these are rule changes and approvals, not decisions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from django.db import transaction

from complylayer.audit.chain import GENESIS, compute_hash, new_audit_id, verify_chain


def append(
    *,
    tenant_id: str,
    event_type: str,
    actor: dict[str, Any],
    subject: dict[str, Any],
    payload: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
):
    """Write one record, chained to the tenant's current tail."""
    from complylayer.models import AuditRecord, Tenant

    moment = occurred_at or datetime.now(UTC)

    with transaction.atomic():
        # Lock the tenant row, not the audit table: two appends for the same
        # tenant must serialise, two for different tenants must not.
        Tenant.objects.select_for_update().filter(id=tenant_id).first()

        tail = (
            AuditRecord.objects.filter(tenant_id=tenant_id).order_by("-recorded_at", "-id").first()
        )
        prev_hash = tail.hash if tail else GENESIS

        record_id = new_audit_id()
        record_hash = compute_hash(
            record_id=record_id,
            tenant_id=tenant_id,
            event_type=event_type,
            occurred_at=moment,
            actor=actor,
            subject=subject,
            payload=payload or {},
            prev_hash=prev_hash,
        )

        return AuditRecord.objects.create(
            id=record_id,
            tenant_id=tenant_id,
            event_type=event_type,
            occurred_at=moment,
            actor=actor,
            subject=subject,
            payload=payload or {},
            prev_hash=prev_hash,
            hash=record_hash,
        )


def correct(*, tenant_id: str, corrects_id: str, actor: dict[str, Any], reason: str):
    """Append a compensating record. The original stays.

    §8.3: corrections are appended, never applied. A trail that can be tidied is
    not a trail, and the correction itself is evidence — of what was wrong, who
    noticed, and when.
    """
    return append(
        tenant_id=tenant_id,
        event_type="audit.corrected",
        actor=actor,
        subject={"type": "audit_record", "id": corrects_id},
        payload={"corrects": corrects_id, "reason": reason},
    )


def verify(tenant_id: str):
    """Verify a tenant's whole chain, oldest first."""
    from complylayer.models import AuditRecord

    records = list(AuditRecord.objects.filter(tenant_id=tenant_id).order_by("recorded_at", "id"))
    return verify_chain(records)


def write_checkpoint(*, tenant_id: str, private_pem: str, now: datetime | None = None):
    """Sign this tenant's current chain head.

    Everything before the head is covered, because the head hash already covers
    everything before it. One signature, not one per record.

    Returns None when the chain is empty — there is nothing to anchor, and a
    checkpoint over nothing would be a signature an auditor could mistake for a
    guarantee.
    """
    from complylayer.audit import checkpoint as cp
    from complylayer.models import AuditCheckpoint, AuditRecord

    moment = now or datetime.now(UTC)

    records = AuditRecord.objects.filter(tenant_id=tenant_id).order_by("recorded_at", "id")
    length = records.count()
    if length == 0:
        return None

    head = records.last()
    signature = cp.sign(
        private_pem,
        tenant_id=tenant_id,
        chain_length=length,
        head_hash=head.hash,
        signed_at=moment,
    )
    return AuditCheckpoint.objects.create(
        tenant_id=tenant_id,
        chain_length=length,
        head_hash=head.hash,
        signed_at=moment,
        signature=signature,
    )


def verify_anchoring(*, tenant_id: str, public_pem: str | None):
    """Whether the chain matches the last thing anybody signed.

    This is the check that survives an attacker with write access. The chain is
    unkeyed, so a rewrite that recomputes every hash verifies perfectly against
    `verify_chain`; it cannot match a signature made with a key that is not in
    the database.

    Returns UNANCHORED rather than True when no key is configured. A
    verification that cannot fail means nothing, and this answer goes to a
    customer's auditor.
    """
    from complylayer.audit import checkpoint as cp
    from complylayer.models import AuditCheckpoint, AuditRecord

    if not public_pem:
        return cp.CheckpointResult(
            anchoring=cp.Anchoring.UNANCHORED,
            detail=(
                "No checkpoint public key is configured, so nothing external anchors this "
                "chain. It will detect a record altered in place and will not detect a "
                "rewrite that recomputes every hash after it. Set "
                "COMPLYLAYER_CHECKPOINT_PUBLIC_KEY."
            ),
        )

    latest = AuditCheckpoint.objects.filter(tenant_id=tenant_id).order_by("-chain_length").first()
    if latest is None:
        return cp.CheckpointResult(
            anchoring=cp.Anchoring.UNANCHORED,
            detail="A key is configured but no checkpoint has been written yet.",
        )

    if not cp.verify_signature(
        public_pem,
        latest.signature,
        tenant_id=tenant_id,
        chain_length=latest.chain_length,
        head_hash=latest.head_hash,
        signed_at=latest.signed_at,
    ):
        return cp.CheckpointResult(
            anchoring=cp.Anchoring.BROKEN,
            checked_at=latest.signed_at,
            chain_length=latest.chain_length,
            detail=(
                "The stored checkpoint does not verify against the configured public key. "
                "Either the checkpoint was tampered with or the key is not the one that "
                "signed it. Both need a human."
            ),
        )

    # The signature is good. Now: does the chain still match what was signed?
    at_signing = (
        AuditRecord.objects.filter(tenant_id=tenant_id)
        .order_by("recorded_at", "id")[: latest.chain_length]
        .values_list("hash", flat=True)
    )
    hashes = list(at_signing)
    if len(hashes) < latest.chain_length:
        return cp.CheckpointResult(
            anchoring=cp.Anchoring.BROKEN,
            checked_at=latest.signed_at,
            chain_length=latest.chain_length,
            detail=(
                f"The last checkpoint signed {latest.chain_length} records and only "
                f"{len(hashes)} remain. Records have been deleted since it was written."
            ),
        )

    if hashes[-1] != latest.head_hash:
        return cp.CheckpointResult(
            anchoring=cp.Anchoring.BROKEN,
            checked_at=latest.signed_at,
            chain_length=latest.chain_length,
            detail=(
                "The chain has been rewritten. Record "
                f"{latest.chain_length} hashed to {latest.head_hash} when it was signed "
                f"and hashes to {hashes[-1]} now."
            ),
        )

    return cp.CheckpointResult(
        anchoring=cp.Anchoring.SIGNED,
        checked_at=latest.signed_at,
        chain_length=latest.chain_length,
        detail=(
            f"Anchored: {latest.chain_length} records, signed {latest.signed_at:%Y-%m-%d %H:%M}Z."
        ),
    )
