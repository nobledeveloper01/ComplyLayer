"""The data model.

Two departures from §5 of the specification, both from the plan review.

**`Decision` is partitioned by month from this first migration** (D10). At the
stated throughput of 2,000 decisions per second the table takes 172 million rows
a day, and the seven-year retention promise in §11.7 lands near a petabyte.
Adding partitioning to a table that size later is a project; adding it now costs
an afternoon. No early customer will sustain 2,000/sec, which is exactly why the
decision has to be made before one does.

**`Decision.context` stores the resolved facts as well as the input** (D11).
§5 says the input is stored "so it can be replayed", but a velocity rule's
outcome depends on a Redis window that has since rolled forward. Replaying a
90-day-old decision against live Redis re-gathers facts that no longer exist and
would present a different answer as a reproduction.
"""

from __future__ import annotations

from django.db import models


class Tenant(models.Model):
    id = models.CharField(primary_key=True, max_length=32)  # tnt_...
    name = models.CharField(max_length=128)

    # Per §10.3 a tenant may override the per-severity fallback, but must do so
    # explicitly, and the change is an audit event. Empty means the documented
    # defaults apply: fail-closed for block, fail-open for flag.
    fallback_policy = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "complylayer_tenant"

    def __str__(self) -> str:
        return f"{self.name} ({self.id})"


class Rule(models.Model):
    id = models.CharField(primary_key=True, max_length=32)  # rul_...
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="rules")
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=64)  # kyc | velocity | aml | fraud
    regulatory_reference = models.CharField(max_length=255, blank=True)

    expression = models.TextField()
    severity = models.CharField(max_length=16)  # block | flag | allow_with_note
    priority = models.IntegerField(default=0)
    applies_to = models.JSONField(default=dict, blank=True)

    # The wording a customer sees when a transfer is refused. Written by the
    # compliance team in the rule builder rather than hard-coded by an engineer,
    # because it is a compliance decision (§7.1).
    customer_message = models.TextField(blank=True)

    state = models.CharField(max_length=16, default="draft")
    version = models.PositiveIntegerField(default=1)

    created_by = models.CharField(max_length=64)
    approved_by = models.CharField(max_length=64, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "complylayer_rule"
        unique_together = [("tenant", "name", "version")]
        indexes = [models.Index(fields=["tenant", "state"])]


class RuleSetVersion(models.Model):
    """An immutable snapshot of every active rule at a moment in time.

    Decisions reference this rather than the `Rule` rows, which is what makes a
    decision reproducible after the underlying rules have changed.

    `lists_snapshot` is here for the same reason the rules are. A rule reading
    `in_list(destination_country, high_risk_countries)` depends on a
    tenant-configured list; left outside the snapshot, editing that list would
    change decisions without changing the version, and two decisions recording
    the same version would no longer represent the same control (D11).
    """

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="rulesets")
    version = models.PositiveIntegerField()
    rules_snapshot = models.JSONField()
    lists_snapshot = models.JSONField(default=dict, blank=True)
    published_at = models.DateTimeField(auto_now_add=True)
    published_by = models.CharField(max_length=64)

    class Meta:
        db_table = "complylayer_rulesetversion"
        unique_together = [("tenant", "version")]


class Decision(models.Model):
    """One served decision. Partitioned by month on `decided_at` (D10).

    Postgres requires the partition key in every unique constraint, so the
    primary key is (id, decided_at). That has a consequence worth stating: a
    unique constraint on (tenant, idempotency_key) would also have to include
    the partition key, which would let the same key produce two decisions either
    side of a month boundary. The guarantee therefore lives in
    :class:`IdempotencyRecord`, which is not partitioned.
    """

    id = models.CharField(max_length=32)  # dec_...
    decided_at = models.DateTimeField(db_index=True)
    pk = models.CompositePrimaryKey("id", "decided_at")

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="decisions")
    idempotency_key = models.CharField(max_length=128)
    ruleset_version = models.PositiveIntegerField()

    transaction_ref = models.CharField(max_length=128, db_index=True)
    customer_ref_hash = models.CharField(max_length=64, db_index=True)
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3)

    # The input as received.
    context = models.JSONField()
    # Every fact as resolved at decision time — velocity counts, aggregates.
    # Without this, replay is not reproduction (D11).
    resolved_facts = models.JSONField(default=dict, blank=True)

    outcome = models.CharField(max_length=16)  # allow | flag | block
    matched_rules = models.JSONField(default=list)
    shadow_matches = models.JSONField(default=list)
    reason = models.TextField(blank=True)

    # A fallback was used, so at least one control did not actually run. §11.3
    # treats this as an availability metric rather than a quality one.
    degraded = models.BooleanField(default=False)
    errored_rules = models.JSONField(default=list, blank=True)

    latency_ms = models.PositiveIntegerField()

    review_status = models.CharField(max_length=16, blank=True)
    reviewed_by = models.CharField(max_length=64, blank=True)
    review_notes = models.TextField(blank=True)

    class Meta:
        db_table = "complylayer_decision"
        indexes = [
            models.Index(fields=["tenant", "outcome", "decided_at"]),
            models.Index(fields=["tenant", "review_status", "decided_at"]),
        ]


class IdempotencyRecord(models.Model):
    """The idempotency guarantee, deliberately kept off the partitioned table.

    A retry has to return the original decision verbatim, including its original
    timestamp (A4). A unique constraint on the partitioned `Decision` table would
    have to include the partition key and so could not span months, which is
    exactly the case a retry at a month boundary would hit.

    Small, unpartitioned, and pruned on the idempotency horizon rather than the
    seven-year retention one.
    """

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="idempotency")
    key = models.CharField(max_length=128)
    decision_id = models.CharField(max_length=32)
    decision_decided_at = models.DateTimeField()
    response_body = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "complylayer_idempotency"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "key"], name="idempotency_key_per_tenant")
        ]


class ApiKey(models.Model):
    """A key resolves to exactly one tenant. No key spans tenants. Ever.

    §8.1's first isolation layer. The prefix is stored in the clear so a lookup
    is one indexed query and a dashboard can show which key is which; the secret
    is Argon2id-hashed and shown once at creation.

    Keys are scoped per environment, so a leaked test key can do nothing to live
    data — and per role, so a key issued for an integration cannot activate a
    rule any more than the engineer holding it could.
    """

    id = models.CharField(primary_key=True, max_length=32)  # key_...
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="api_keys")
    name = models.CharField(max_length=128)
    prefix = models.CharField(max_length=24, unique=True, db_index=True)
    hashed_secret = models.CharField(max_length=255)
    environment = models.CharField(max_length=8, default="live")  # test | live
    role = models.CharField(max_length=32)
    created_by = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    # Rotation uses overlapping validity: a new key is issued and both work until
    # the old one is explicitly revoked, so rotation never causes downtime.
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "complylayer_apikey"
        indexes = [models.Index(fields=["tenant", "environment"])]

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class NamedList(models.Model):
    """A tenant-configured list a rule can refer to by name.

    Versioned into the rule set snapshot rather than read live (D11). A rule
    reading `in_list(destination_country, high_risk_countries)` depends on this,
    so editing it has to publish a new version — otherwise two decisions
    recording the same version would not represent the same control.
    """

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="lists")
    name = models.CharField(max_length=64)
    values = models.JSONField(default=list)
    updated_by = models.CharField(max_length=64)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "complylayer_namedlist"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "name"], name="named_list_per_tenant")
        ]


class AuditRecord(models.Model):
    """Append-only, hash-chained. The grants and trigger that enforce that arrive
    with the management API in phase 5; the shape is fixed here so nothing has to
    migrate later."""

    id = models.CharField(primary_key=True, max_length=32)  # aud_...
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="audit")
    event_type = models.CharField(max_length=64, db_index=True)
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(auto_now_add=True)
    actor = models.JSONField(default=dict)
    subject = models.JSONField(default=dict)
    payload = models.JSONField(default=dict)
    prev_hash = models.CharField(max_length=71, blank=True)  # sha256:<64 hex>
    hash = models.CharField(max_length=71)

    class Meta:
        db_table = "complylayer_auditrecord"
        indexes = [models.Index(fields=["tenant", "recorded_at"])]
