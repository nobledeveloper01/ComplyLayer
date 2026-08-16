"""The audit trail: the difference between a system that logs and one that
produces evidence.

§8.3's claim is that immutability is *enforced, not promised*, and that phrase is
doing real work. Four independent mechanisms, and the order matters because each
one covers a case the one before it does not:

1. **Grants.** The application role has INSERT and SELECT on this table and
   nothing else. An ORM bug cannot update a record because the connection has no
   permission to.
2. **A trigger.** `BEFORE UPDATE OR DELETE` raises regardless of who is asking,
   so a superuser at a psql prompt at 2am fails loudly rather than quietly
   succeeding. Grants protect against accident; the trigger protects against
   authority.
3. **A hash chain.** Each record's hash covers its own content and the previous
   record's hash, so tampering anywhere invalidates every record after it. This
   is what catches an edit made *outside* the database — a restored backup, an
   altered replica, a file-level change.
4. **Corrections are appended, never applied.** A wrong record is followed by a
   compensating one carrying `corrects`. The original stays, because a trail that
   can be tidied is not a trail.

The chain is per tenant. One global chain would make a busy tenant's writes
serialise behind everyone else's, and would leak the fact of one tenant's
activity into another's verification.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

GENESIS = "sha256:" + "0" * 64


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    checked: int
    broken_at: str | None = None
    detail: str = ""


def new_audit_id() -> str:
    return f"aud_{secrets.token_hex(12)}"


def canonical_payload(
    *,
    record_id: str,
    tenant_id: str,
    event_type: str,
    occurred_at: datetime,
    actor: dict[str, Any],
    subject: dict[str, Any],
    payload: dict[str, Any],
    prev_hash: str,
) -> bytes:
    """The exact bytes the hash covers.

    Sorted keys and explicit separators, because a hash chain whose input
    depends on dict ordering verifies today and fails after a Python upgrade.
    `recorded_at` is deliberately excluded: it is when the row was written, which
    a replica or a restore can legitimately differ on, and including it would
    make the chain fail for reasons that are not tampering.
    """
    return json.dumps(
        {
            "id": record_id,
            "tenant_id": tenant_id,
            "event_type": event_type,
            "occurred_at": occurred_at.isoformat(),
            "actor": actor,
            "subject": subject,
            "payload": payload,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()


def compute_hash(**fields: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_payload(**fields)).hexdigest()


def verify_chain(records: list[Any]) -> VerificationResult:
    """Walk a tenant's chain in order and check every link.

    Returns rather than raises, and names the *first* broken record. A
    verification that reports "something is wrong" without saying where is a
    verification nobody can act on, and this output goes to a customer's auditor
    through `GET /v1/audit/verify`.
    """
    expected_prev = GENESIS

    for index, record in enumerate(records):
        if record.prev_hash != expected_prev:
            return VerificationResult(
                ok=False,
                checked=index,
                broken_at=record.id,
                detail=(
                    f"record {record.id} claims to follow {record.prev_hash} "
                    f"but the previous record hashed to {expected_prev}"
                ),
            )

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
        if recomputed != record.hash:
            return VerificationResult(
                ok=False,
                checked=index,
                broken_at=record.id,
                detail=(
                    f"record {record.id} does not hash to its stored value — "
                    "its contents changed after it was written"
                ),
            )

        expected_prev = record.hash

    return VerificationResult(ok=True, checked=len(records))
