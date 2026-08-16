"""The management API: §7.2's surface, on DRF.

**Every queryset is scoped to the authenticated tenant, and a miss is a 404.**
Not a 403. A 403 confirms the resource exists, which tells one tenant that
another tenant's rule id is real — and rule ids are guessable enough to matter.
§8.1's mandatory isolation test asserts 404 for exactly this reason.

The scoping is one method (`get_queryset`) that every view inherits, because
isolation implemented per view is isolation somebody forgets on the view they
add in a hurry. `tests/test_tenant_isolation.py` enumerates the URLconf and
fails if any route is not covered, so forgetting is caught rather than assumed
away.
"""

from __future__ import annotations

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from complylayer import audit, rules
from complylayer.api.management.permissions import IsAuthenticatedKey
from complylayer.api.management.serializers import (
    DecisionSerializer,
    NamedListSerializer,
    RuleSerializer,
    RuleSetVersionSerializer,
)
from complylayer.dsl import RuleSyntaxError, validate_source
from complylayer.dsl.structured import to_expression
from complylayer.models import Decision, NamedList, Rule, RuleSetVersion
from complylayer.rules.lifecycle import LifecycleError
from complylayer.tenancy import Action, PermissionDenied


class TenantScopedViewSet(viewsets.ModelViewSet):
    """The base every management view inherits.

    Isolation lives here rather than in each view, because isolation implemented
    per view is isolation somebody forgets on the view they add in a hurry.
    """

    permission_classes = [IsAuthenticatedKey]

    # An unordered paginated list can repeat or skip rows between pages, so
    # every view states its order. Django warns about this rather than failing,
    # which is exactly the kind of warning that gets scrolled past.
    ordering = ("id",)

    @property
    def tenant_id(self) -> str:
        return self.request.credentials.tenant_id

    @property
    def actor(self):
        return self.request.credentials.actor

    def get_queryset(self):
        return self.queryset.filter(tenant_id=self.tenant_id).order_by(*self.ordering)

    def get_object(self):
        """A cross-tenant id is indistinguishable from one that never existed."""
        obj = self.get_queryset().filter(pk=self.kwargs["pk"]).first()
        if obj is None:
            raise NotFound("No such object.")
        return obj

    def handle_exception(self, exc):
        if isinstance(exc, PermissionDenied):
            return Response({"error": "forbidden", "message": str(exc)}, status=403)
        if isinstance(exc, LifecycleError):
            return Response({"error": "invalid_transition", "message": str(exc)}, status=409)
        if isinstance(exc, RuleSyntaxError):
            return Response({"error": "invalid_rule", **exc.as_dict()}, status=400)
        return super().handle_exception(exc)


class RuleViewSet(TenantScopedViewSet):
    queryset = Rule.objects.all()
    serializer_class = RuleSerializer
    ordering = ("priority", "id")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rule = rules.create_draft(
            tenant_id=self.tenant_id, actor=self.actor, **serializer.validated_data
        )
        return Response(self.get_serializer(rule).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        rule = self.get_object()
        serializer = self.get_serializer(rule, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated = rules.edit_draft(rule=rule, actor=self.actor, **serializer.validated_data)
        return Response(self.get_serializer(updated).data)

    def destroy(self, request, *args, **kwargs):
        """There is no delete. A rule that once decided something is evidence."""
        rule = self.get_object()
        rules.archive(rule=rule, actor=self.actor, reason=request.data.get("reason", ""))
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["post"])
    def validate(self, request):
        """Syntax check without saving — what the rule builder calls on keystroke.

        Accepts either an expression or the builder's structured form, and
        returns the same errors either way. A rule built in the UI earns no
        leniency a typed one would not get.
        """
        expression = request.data.get("expression")
        if expression is None and "structured" in request.data:
            expression = to_expression(request.data["structured"])
        if not expression:
            raise ValidationError({"expression": "Send an expression or a structured rule."})

        validate_source(expression)
        return Response({"valid": True, "expression": expression})

    @action(detail=True, methods=["post"], url_path="request-approval")
    def request_approval(self, request, pk=None):
        rule = rules.request_approval(rule=self.get_object(), actor=self.actor)
        return Response(self.get_serializer(rule).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        rule = rules.approve(
            rule=self.get_object(), actor=self.actor, reason=request.data.get("reason", "")
        )
        return Response(self.get_serializer(rule).data)

    @action(detail=True, methods=["post"])
    def shadow(self, request, pk=None):
        rule = rules.move_to_shadow(rule=self.get_object(), actor=self.actor)
        return Response(self.get_serializer(rule).data)

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        rule, version = rules.activate(
            rule=self.get_object(),
            actor=self.actor,
            emergency_reason=request.data.get("emergency_reason", ""),
        )
        return Response({**self.get_serializer(rule).data, "ruleset_version": version.version})

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        rule, version = rules.archive(
            rule=self.get_object(), actor=self.actor, reason=request.data.get("reason", "")
        )
        return Response({**self.get_serializer(rule).data, "ruleset_version": version.version})


class RuleSetVersionViewSet(TenantScopedViewSet):
    """Immutable snapshots. Read-only by construction — a version somebody's
    decision references is not a thing that may be edited."""

    queryset = RuleSetVersion.objects.all()
    serializer_class = RuleSetVersionSerializer
    ordering = ("-version",)
    http_method_names = ["get", "post", "head", "options"]
    lookup_field = "version"

    def get_object(self):
        obj = self.get_queryset().filter(version=self.kwargs["version"]).first()
        if obj is None:
            raise NotFound("No such object.")
        return obj

    def create(self, request, *args, **kwargs):
        """`POST /v1/rulesets` with `revert_to` publishes a version whose content
        equals an older one. Forward, never backward (review finding G1)."""
        revert_to = request.data.get("revert_to")
        if revert_to is None:
            raise ValidationError({"revert_to": "Send the version to revert to."})
        version = rules.revert(
            tenant_id=self.tenant_id, actor=self.actor, to_version=int(revert_to)
        )
        return Response(self.get_serializer(version).data, status=status.HTTP_201_CREATED)


class DecisionViewSet(TenantScopedViewSet):
    queryset = Decision.objects.all()
    serializer_class = DecisionSerializer
    http_method_names = ["get", "post", "head", "options"]
    ordering = ("-decided_at", "id")

    def get_queryset(self):
        queryset = super().get_queryset()
        for field in ("outcome", "review_status"):
            value = self.request.query_params.get(field)
            if value:
                queryset = queryset.filter(**{field: value})
        return queryset

    def get_object(self):
        """By `id`, not `pk`.

        Decision's primary key is composite — (id, decided_at) — because the
        table is partitioned, and Django's tuple lookup refuses a bare value.
        The partitioning decision reaches further than the migration that made
        it, which is worth a comment rather than a puzzled half hour.
        """
        obj = self.get_queryset().filter(id=self.kwargs["pk"]).first()
        if obj is None:
            raise NotFound("No such object.")
        return obj

    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        from complylayer.tenancy import require

        require(self.actor, Action.REVIEW_DECISION)
        decision = self.get_object()
        decision.review_status = request.data.get("status", "cleared")
        decision.reviewed_by = self.actor.id
        decision.review_notes = request.data.get("notes", "")
        decision.save(update_fields=["review_status", "reviewed_by", "review_notes"])

        audit.append(
            tenant_id=self.tenant_id,
            event_type="decision.reviewed",
            actor=self.actor.as_audit_actor(),
            subject={"type": "decision", "id": decision.id},
            payload={"status": decision.review_status, "notes": decision.review_notes},
        )
        return Response(self.get_serializer(decision).data)


class NamedListViewSet(TenantScopedViewSet):
    """Editing a list publishes a new rule set version (D11).

    Otherwise a list edit changes decisions without changing the version, and
    two decisions recording the same version would not mean the same control.
    """

    queryset = NamedList.objects.all()
    serializer_class = NamedListSerializer
    ordering = ("name",)

    def perform_create(self, serializer):
        serializer.save(tenant_id=self.tenant_id, updated_by=self.actor.id)
        self._publish("list.created", serializer.instance)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.actor.id)
        self._publish("list.updated", serializer.instance)

    def _publish(self, event_type: str, instance) -> None:
        version = rules.publish_version(tenant_id=self.tenant_id, actor=self.actor)
        audit.append(
            tenant_id=self.tenant_id,
            event_type=event_type,
            actor=self.actor.as_audit_actor(),
            subject={"type": "named_list", "id": instance.name},
            payload={"values": instance.values, "ruleset_version": version.version},
        )
