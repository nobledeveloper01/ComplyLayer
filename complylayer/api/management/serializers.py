"""Serialisers for the management API.

Note what is *not* writable. `state`, `approved_by`, `activated_at` and `version`
are all read-only: they are moved by the lifecycle state machine, and a PATCH
that could set `state: active` would route around the approval workflow entirely.
An API whose serialiser can undo its own state machine has no state machine.
"""

from __future__ import annotations

from rest_framework import serializers

from complylayer.models import ApiKey, Decision, NamedList, Rule, RuleSetVersion


class RuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Rule
        fields = [
            "id",
            "name",
            "description",
            "category",
            "regulatory_reference",
            "expression",
            "severity",
            "priority",
            "customer_message",
            "state",
            "version",
            "created_by",
            "approved_by",
            "approved_at",
            "activated_at",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "state",
            "version",
            "created_by",
            "approved_by",
            "approved_at",
            "activated_at",
            "created_at",
        ]


class RuleSetVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = RuleSetVersion
        fields = ["version", "rules_snapshot", "lists_snapshot", "published_at", "published_by"]
        read_only_fields = fields


class DecisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Decision
        fields = [
            "id",
            "decided_at",
            "ruleset_version",
            "transaction_ref",
            "amount_minor",
            "currency",
            "outcome",
            "matched_rules",
            "shadow_matches",
            "reason",
            "degraded",
            "latency_ms",
            "review_status",
            "reviewed_by",
            "review_notes",
            "context",
            "resolved_facts",
        ]
        read_only_fields = fields


class NamedListSerializer(serializers.ModelSerializer):
    class Meta:
        model = NamedList
        fields = ["name", "values", "updated_by", "updated_at"]
        read_only_fields = ["updated_by", "updated_at"]


class ApiKeySerializer(serializers.ModelSerializer):
    """A key, as everyone other than its creator ever sees it.

    `hashed_secret` is not a field here and never will be. The secret itself
    exists in the clear exactly once, in the response to the request that
    created it — after that the database holds an Argon2id hash and there is no
    way back, which is the property that makes a leaked database not a leaked
    set of credentials.
    """

    active = serializers.BooleanField(source="is_active", read_only=True)

    class Meta:
        model = ApiKey
        fields = [
            "id",
            "name",
            "prefix",
            "environment",
            "role",
            "created_by",
            "created_at",
            "revoked_at",
            "active",
        ]
        read_only_fields = ["id", "prefix", "created_by", "created_at", "revoked_at", "active"]
