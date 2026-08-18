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

    def __init__(
        self,
        tenant_id: str,
        ruleset: RuleSet,
        store,
        velocity=None,
        velocity_factory=None,
        salt: str = "salt",
    ):
        self.tenant_id = tenant_id
        self.ruleset = ruleset
        self.store = store
        # `velocity` is one provider for every transaction, which suits a test.
        # `velocity_factory` builds one per customer, which is what production
        # needs — Redis keys are scoped per customer, and the handler does not
        # know which customer until a request arrives.
        self.velocity = velocity
        self.velocity_factory = velocity_factory
        self.salt = salt
        self._provider = velocity

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
        customer_hash = hash_customer_ref(transaction.customer_ref, self.salt)
        fallback = self.store.fallback_policy(self.tenant_id)

        decision, facts = self._evaluate(transaction, now, customer_hash, fallback)

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

    def _evaluate(self, transaction, now, customer_hash, fallback):
        """Gather and evaluate.

        The window is fetched with this transaction already in it, atomically —
        see complylayer/velocity/redis_store.py for why that replaced the lock
        D2 originally called for. The upshot here is that there is no second
        evaluation pass and no lock to acquire: one round trip, one evaluation.
        """
        # The window is fetched first, because build_facts reads the aggregates
        # it returns. If that fetch fails, `velocity` comes back as a provider
        # that reports its own unavailability per rule rather than taking the
        # decision down.
        self._provider = self.velocity
        if self._provider is None and self.velocity_factory is not None:
            self._provider = self.velocity_factory(customer_hash)

        velocity = self._gather(transaction, now)
        facts = self.build_facts(transaction, now, velocity)
        context = EvaluationContext(facts=facts, functions=functions.build(velocity, now))
        return decide(self.ruleset, context, fallback=fallback), facts

    def _gather(self, transaction: Transaction, now: datetime):
        """The write path (D9). Windows count attempts, blocked ones included.

        **A failure here degrades rather than raises**, which the chaos suite
        found the hard way. §10.3's fallback originally covered a rule that could
        not *evaluate*; it did not cover the fetch that feeds every velocity rule
        at once. So a Redis outage propagated out of `decide()` and became a 500
        — the one failure mode the whole fail-open/fail-closed design exists to
        replace, and it would have looked like an outage rather than a degraded
        decision.

        On failure the provider is swapped for one whose velocity functions raise
        `RuleEvaluationError`, which puts the outcome back where §10.3 wants it:
        decided per rule, per severity, and recorded.
        """
        provider = self._provider
        gather = getattr(provider, "record_and_gather", None)
        if gather is None:
            return provider

        try:
            gather(
                transaction.transaction_ref,
                transaction.amount_minor,
                transaction.transaction_type,
                now.timestamp(),
            )
        except Exception as exc:
            return UnavailableVelocity(exc)
        return self.velocity

    def build_facts(
        self, transaction: Transaction, now: datetime, velocity: Any = None
    ) -> dict[str, Any]:
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

        # Aggregate facts about the customer's history, from the same fetch as
        # the rolling window — no second round trip.
        aggregates = getattr(
            velocity if velocity is not None else self.velocity, "aggregate_facts", None
        )
        if aggregates is not None:
            facts.update(aggregates())

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


class UnavailableVelocity:
    """Stands in when the velocity fetch failed.

    Every call raises the same evaluation error, so each rule that needed a
    window degrades on its own terms — fail-closed for `block`, fail-open for
    `flag` — and each one is recorded. Rules that never asked for velocity are
    untouched, which is what makes this a degraded service rather than an outage.
    """

    def __init__(self, cause: Exception):
        self.cause = cause

    def _unavailable(self, *args, **kwargs):
        from complylayer.dsl.errors import RuleEvaluationError

        raise RuleEvaluationError(f"velocity data is unavailable: {self.cause}")

    count = _unavailable
    total = _unavailable

    def aggregate_facts(self) -> dict[str, int]:
        """No aggregates rather than wrong ones.

        A rule reading `lifetime_transaction_count` gets an unknown-fact error
        and degrades, which is right: reporting zero would quietly turn every
        established customer into a brand-new one during an outage.
        """
        return {}


def _serialisable(facts: dict[str, Any]) -> dict[str, Any]:
    """Tuples become lists so the resolved facts round-trip through JSON."""
    return {key: list(value) if isinstance(value, tuple) else value for key, value in facts.items()}


def public_body(body: dict[str, Any]) -> dict[str, Any]:
    """Strip the internal keys before the response goes out."""
    return {key: value for key, value in body.items() if not key.startswith("_")}
