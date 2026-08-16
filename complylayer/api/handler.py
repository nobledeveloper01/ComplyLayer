"""What happens between a validated request and a returned decision.

Kept out of the view so it can be exercised without HTTP, and so the D1
benchmark can run the same work behind both a plain view and a DRF one and
compare only the framework.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from typing import Any

from complylayer.api.validation import Transaction
from complylayer.dsl import functions
from complylayer.dsl.interpreter import EvaluationContext
from complylayer.engine import Outcome, RuleSet, decide


def new_decision_id() -> str:
    return f"dec_{secrets.token_hex(12)}"


def hash_customer_ref(customer_ref: str, salt: str) -> str:
    """Pseudonymised with a per-tenant salt before storage (§8.4).

    HMAC rather than a plain hash so the salt is a key rather than a suffix: a
    stolen decisions table without the salt yields nothing, and a rainbow table
    over customer identifiers is not a shortcut.
    """
    return hmac.new(salt.encode(), customer_ref.encode(), hashlib.sha256).hexdigest()


class DecisionHandler:
    """One tenant's decision path.

    ``store`` and ``velocity`` are injected. Phase 3 supplies a Redis-backed
    velocity provider and phase 4 a versioned in-process rule cache; nothing in
    here needs to change when they arrive.
    """

    def __init__(self, tenant_id: str, ruleset: RuleSet, store, velocity=None, salt: str = "salt"):
        self.tenant_id = tenant_id
        self.ruleset = ruleset
        self.store = store
        self.velocity = velocity
        self.salt = salt

    def replay(self, idempotency_key: str) -> dict[str, Any] | None:
        """The original response, verbatim, or None.

        Verbatim matters: the timestamp, the decision id and the recorded
        latency are the original ones. A retry that reported today's time would
        be a different decision wearing the same id, and the audit trail would
        show two events for one transaction.
        """
        return self.store.find_response(self.tenant_id, idempotency_key)

    def decide(self, transaction: Transaction, idempotency_key: str) -> dict[str, Any]:
        now = datetime.now(UTC)
        facts = self.build_facts(transaction, now)

        context = EvaluationContext(
            facts=facts,
            functions=functions.build(self.velocity, now),
        )
        decision = decide(
            self.ruleset, context, fallback=self.store.fallback_policy(self.tenant_id)
        )

        body: dict[str, Any] = {
            "decision_id": new_decision_id(),
            "outcome": str(decision.outcome),
            "reason": decision.reason,
            "matched_rules": [
                {
                    "id": rule.id,
                    "name": rule.name,
                    "severity": str(rule.severity),
                    "regulatory_reference": rule.regulatory_reference,
                }
                for rule in decision.matched_rules
            ],
            "evaluated_rules": decision.evaluated_rules,
            "ruleset_version": self.ruleset.version,
            "degraded": decision.degraded,
            "decided_at": now.isoformat(),
        }
        if decision.outcome is Outcome.BLOCK and decision.customer_message:
            body["customer_message"] = decision.customer_message

        # Carried alongside the response rather than inside it: the caller does
        # not need the shadow results, but the divergence report does.
        body["_shadow_matches"] = [rule.id for rule in decision.shadow_matches]
        body["_errored_rules"] = [
            {"id": result.rule.id, "error": result.error} for result in decision.errored_rules
        ]
        body["_resolved_facts"] = _serialisable(facts)
        return body

    def build_facts(self, transaction: Transaction, now: datetime) -> dict[str, Any]:
        """The entire namespace a rule can see for this transaction.

        Flat, because the DSL has no dot: `kyc_tier`, not `customer.kyc_tier`.
        Every name here is one a compliance officer can use, and nothing outside
        it is reachable.
        """
        facts: dict[str, Any] = {
            "amount_minor": transaction.amount_minor,
            "currency": transaction.currency,
            "transaction_type": transaction.transaction_type,
            "channel": transaction.channel,
        }

        for key, value in transaction.customer.items():
            facts[key] = value
        for key, value in transaction.destination.items():
            facts[f"destination_{key}"] = value
        for key, value in transaction.device.items():
            facts[f"device_{key}"] = value

        # Named lists come from the frozen snapshot, not from live configuration
        # (D11) — editing a list has to publish a new version, or two decisions
        # recording the same version would not mean the same control.
        for name, values in self.store.named_lists(self.tenant_id, self.ruleset.version).items():
            facts[name] = tuple(values)

        return facts

    def record(self, body: dict[str, Any], transaction: Transaction, idempotency_key: str) -> None:
        """Persist the decision and its idempotency record.

        §4.2 makes this asynchronous through a durable local queue (D3). Phase 2
        writes synchronously so the behaviour is settled and testable; the queue
        arrives with the latency work in phase 4, where it can be measured
        rather than assumed.
        """
        self.store.save(
            tenant_id=self.tenant_id,
            body=body,
            transaction=transaction,
            idempotency_key=idempotency_key,
            customer_ref_hash=hash_customer_ref(transaction.customer_ref, self.salt),
            ruleset_version=self.ruleset.version,
        )


def _serialisable(facts: dict[str, Any]) -> dict[str, Any]:
    """Tuples become lists so the resolved facts round-trip through JSON."""
    return {key: list(value) if isinstance(value, tuple) else value for key, value in facts.items()}


def public_body(body: dict[str, Any]) -> dict[str, Any]:
    """Strip the internal keys before the response goes out."""
    return {key: value for key, value in body.items() if not key.startswith("_")}
