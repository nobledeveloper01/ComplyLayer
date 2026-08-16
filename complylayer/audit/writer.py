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
