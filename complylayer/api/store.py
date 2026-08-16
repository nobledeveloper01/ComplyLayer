"""Persistence for the decision path.

A thin seam rather than an abstraction for its own sake. The decision path has
to be testable without Postgres, the D1 benchmark has to measure the framework
rather than the database, and phase 4 replaces the synchronous write with a
durable local queue (D3) without touching the handler.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from complylayer.api.handler import public_body
from complylayer.api.validation import Transaction
from complylayer.engine import Severity


class DecisionStore(Protocol):  # pragma: no cover - an interface, not code
    def find_response(self, tenant_id: str, idempotency_key: str) -> dict[str, Any] | None: ...
    def fallback_policy(self, tenant_id: str) -> dict[Severity, str]: ...
    def named_lists(self, tenant_id: str, version: int) -> dict[str, list]: ...
    def save(self, **kwargs) -> None: ...


class InMemoryStore:
    """Used by the tests and the benchmark. Same contract, no database."""

    def __init__(self, fallback=None, lists=None):
        self.responses: dict[tuple[str, str], dict[str, Any]] = {}
        self.saved: list[dict[str, Any]] = []
        self._fallback = fallback or {}
        self._lists = lists or {}

    def find_response(self, tenant_id: str, idempotency_key: str) -> dict[str, Any] | None:
        return self.responses.get((tenant_id, idempotency_key))

    def fallback_policy(self, tenant_id: str) -> dict[Severity, str]:
        return self._fallback

    def named_lists(self, tenant_id: str, version: int) -> dict[str, list]:
        return self._lists

    def save(self, *, tenant_id, body, transaction, idempotency_key, **rest) -> None:
        self.responses[(tenant_id, idempotency_key)] = public_body(body)
        self.saved.append({"body": body, "transaction": transaction, **rest})


class DatabaseStore:
    """The real one. Writes the decision and its idempotency record together."""

    def find_response(self, tenant_id: str, idempotency_key: str) -> dict[str, Any] | None:
        from complylayer.models import IdempotencyRecord

        record = IdempotencyRecord.objects.filter(tenant_id=tenant_id, key=idempotency_key).first()
        return record.response_body if record else None

    def fallback_policy(self, tenant_id: str) -> dict[Severity, str]:
        from complylayer.models import Tenant

        tenant = Tenant.objects.filter(id=tenant_id).only("fallback_policy").first()
        if not tenant or not tenant.fallback_policy:
            return {}
        return {Severity(key): value for key, value in tenant.fallback_policy.items()}

    def named_lists(self, tenant_id: str, version: int) -> dict[str, list]:
        from complylayer.models import RuleSetVersion

        snapshot = (
            RuleSetVersion.objects.filter(tenant_id=tenant_id, version=version)
            .only("lists_snapshot")
            .first()
        )
        return snapshot.lists_snapshot if snapshot else {}

    def save(
        self,
        *,
        tenant_id: str,
        body: dict[str, Any],
        transaction: Transaction,
        idempotency_key: str,
        customer_ref_hash: str,
        ruleset_version: int,
    ) -> None:
        from django.db import transaction as db_transaction

        from complylayer.models import Decision, IdempotencyRecord

        decided_at = datetime.fromisoformat(body["decided_at"])
        response = public_body(body)

        with db_transaction.atomic():
            Decision.objects.create(
                id=body["decision_id"],
                decided_at=decided_at,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                ruleset_version=ruleset_version,
                transaction_ref=transaction.transaction_ref,
                customer_ref_hash=customer_ref_hash,
                amount_minor=transaction.amount_minor,
                currency=transaction.currency,
                context=transaction.as_context(),
                resolved_facts=body.get("_resolved_facts", {}),
                outcome=body["outcome"],
                matched_rules=body["matched_rules"],
                shadow_matches=body.get("_shadow_matches", []),
                reason=body["reason"],
                degraded=body["degraded"],
                errored_rules=body.get("_errored_rules", []),
                latency_ms=body.get("latency_ms", 0),
            )
            # Two concurrent requests with one key can both miss the cache and
            # both evaluate. Because evaluation is deterministic against a pinned
            # version they produce the same answer, so the caller is never given
            # two different ones — and this constraint means only one idempotency
            # record survives, which is the one every later retry replays (D2).
            IdempotencyRecord.objects.get_or_create(
                tenant_id=tenant_id,
                key=idempotency_key,
                defaults={
                    "decision_id": body["decision_id"],
                    "decision_decided_at": decided_at,
                    "response_body": response,
                },
            )
