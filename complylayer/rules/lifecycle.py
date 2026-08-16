"""The rule lifecycle: draft, shadow, approval, activation, archive.

Every transition here writes an audit record, and that is not decoration. §9 maps
"evidence of which controls were in force at a given time" to this module: a
regulator's question is answered by the trail these transitions leave, not by the
current state of the rules table.

Three behaviours are worth reading the code for.

**Editing a rule that is awaiting approval resets the approval.** Otherwise the
approval workflow is theatre: get a harmless change approved, then edit it. This
is the single easiest way to defeat four-eyes review and it has to be closed in
the state machine rather than in the UI.

**Activation publishes a new immutable rule set version**, snapshotting every
active rule *and* every named list. Decisions reference the version, which is
what makes them reproducible after the rules have moved on — and what makes
"why was this allowed six months ago?" answerable.

**Reverting is a forward action.** `revert(to_version=N)` creates a new version
whose content equals N rather than rewinding anything, so the trail stays
append-only. Without it, undoing a bad rule means recreating it by hand, during
an incident, by somebody who is not an engineer.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from django.db import transaction

from complylayer import audit
from complylayer.dsl import RuleSyntaxError, validate_source
from complylayer.engine.evaluation import State
from complylayer.tenancy.roles import Action, Actor, require, require_not_author


class LifecycleError(ValueError):
    """A transition the state machine does not allow."""


@dataclass(frozen=True)
class RuleChange:
    """What changed, for the approval diff.

    The diff is the highest-stakes screen in the product: `amount_minor >
    5_000_000` becoming `amount_minor > 50_000_000` is one character that moves a
    limit tenfold, and a risk manager either catches it or does not.
    """

    field: str
    before: object
    after: object


def new_rule_id() -> str:
    return f"rul_{secrets.token_hex(8)}"


def create_draft(*, tenant_id: str, actor: Actor, **fields):
    """A new rule, always as a draft. Nothing is born active."""
    from complylayer.models import Rule

    require(actor, Action.CREATE_DRAFT)
    _validate_expression(fields.get("expression", ""))

    rule = Rule.objects.create(
        id=new_rule_id(),
        tenant_id=tenant_id,
        state=State.DRAFT,
        created_by=actor.id,
        **fields,
    )
    audit.append(
        tenant_id=tenant_id,
        event_type="rule.created",
        actor=actor.as_audit_actor(),
        subject={"type": "rule", "id": rule.id},
        payload={"name": rule.name, "expression": rule.expression, "severity": rule.severity},
    )
    return rule


def edit_draft(*, rule, actor: Actor, **fields):
    """Edit a draft or a rule awaiting approval.

    Editing something already approved-but-not-active resets the approval, and
    the audit record says so. An approval that survives an edit is not an
    approval of anything in particular.
    """
    require(actor, Action.EDIT_DRAFT)

    if rule.state not in (State.DRAFT, State.SHADOW):
        raise LifecycleError(
            f"a rule in state {rule.state} cannot be edited; archive it and create a new "
            "version, so the change is visible in the trail"
        )

    if "expression" in fields:
        _validate_expression(fields["expression"])

    changes = [
        RuleChange(field=name, before=getattr(rule, name), after=value)
        for name, value in fields.items()
        if getattr(rule, name) != value
    ]
    if not changes:
        return rule

    approval_reset = bool(rule.approved_by)
    for name, value in fields.items():
        setattr(rule, name, value)
    if approval_reset:
        rule.approved_by = ""
        rule.approved_at = None
    rule.version += 1
    rule.save()

    audit.append(
        tenant_id=rule.tenant_id,
        event_type="rule.edited",
        actor=actor.as_audit_actor(),
        subject={"type": "rule", "id": rule.id},
        payload={
            "version": rule.version,
            "changes": [
                {"field": change.field, "before": change.before, "after": change.after}
                for change in changes
            ],
            "approval_reset": approval_reset,
        },
    )
    return rule


def request_approval(*, rule, actor: Actor):
    require(actor, Action.REQUEST_APPROVAL)
    if rule.state not in (State.DRAFT, State.SHADOW):
        raise LifecycleError(f"a rule in state {rule.state} is not awaiting approval")

    audit.append(
        tenant_id=rule.tenant_id,
        event_type="rule.approval_requested",
        actor=actor.as_audit_actor(),
        subject={"type": "rule", "id": rule.id},
        payload={"version": rule.version, "expression": rule.expression},
    )
    return rule


def approve(*, rule, actor: Actor, reason: str = ""):
    """Approve someone else's change. Never your own."""
    require(actor, Action.APPROVE)
    require_not_author(actor, rule.created_by, Action.APPROVE)

    rule.approved_by = actor.id
    rule.approved_at = datetime.now(UTC)
    rule.save(update_fields=["approved_by", "approved_at"])

    audit.append(
        tenant_id=rule.tenant_id,
        event_type="rule.approved",
        actor=actor.as_audit_actor(),
        subject={"type": "rule", "id": rule.id},
        payload={"version": rule.version, "author": rule.created_by, "reason": reason},
    )
    return rule


def move_to_shadow(*, rule, actor: Actor):
    """Evaluate in production without affecting a single customer."""
    require(actor, Action.SHADOW)
    rule.state = State.SHADOW
    rule.save(update_fields=["state"])

    audit.append(
        tenant_id=rule.tenant_id,
        event_type="rule.shadowed",
        actor=actor.as_audit_actor(),
        subject={"type": "rule", "id": rule.id},
        payload={"version": rule.version},
    )
    return rule


def activate(*, rule, actor: Actor, emergency_reason: str = ""):
    """Put a rule in force, and publish the rule set version that includes it.

    Without an approval this needs an emergency override, which is a separate
    permission, requires a written reason, and pages the risk lead. §11.4 makes
    that a page rather than a warning because a control changed outside the
    approval workflow is exactly the event nobody should be able to bury.
    """
    require(actor, Action.ACTIVATE)

    if not rule.approved_by:
        if not emergency_reason:
            raise LifecycleError(
                "this rule has not been approved. Have someone else approve it, or use "
                "the emergency override with a written reason — which pages the risk lead."
            )
        require(actor, Action.EMERGENCY_OVERRIDE)

    with transaction.atomic():
        rule.state = State.ACTIVE
        rule.activated_at = datetime.now(UTC)
        rule.save(update_fields=["state", "activated_at"])

        version = publish_version(tenant_id=rule.tenant_id, actor=actor)

        audit.append(
            tenant_id=rule.tenant_id,
            event_type="rule.emergency_activated" if emergency_reason else "rule.activated",
            actor=actor.as_audit_actor(),
            subject={"type": "rule", "id": rule.id},
            payload={
                "version": rule.version,
                "ruleset_version": version.version,
                "approved_by": rule.approved_by,
                "emergency_reason": emergency_reason,
            },
        )
    return rule, version


def archive(*, rule, actor: Actor, reason: str = ""):
    require(actor, Action.ARCHIVE)

    with transaction.atomic():
        rule.state = State.ARCHIVED
        rule.save(update_fields=["state"])
        version = publish_version(tenant_id=rule.tenant_id, actor=actor)

        audit.append(
            tenant_id=rule.tenant_id,
            event_type="rule.archived",
            actor=actor.as_audit_actor(),
            subject={"type": "rule", "id": rule.id},
            payload={"reason": reason, "ruleset_version": version.version},
        )
    return rule, version


def publish_version(*, tenant_id: str, actor: Actor):
    """Freeze every active and shadow rule, plus every named list, as one version.

    Lists are in here for the same reason the rules are (D11): a rule reading
    `in_list(destination_country, high_risk_countries)` depends on that list, so
    leaving it outside the snapshot would let an edit change decisions without
    changing the version — and two decisions recording the same version would no
    longer mean the same control.
    """
    from complylayer.models import NamedList, Rule, RuleSetVersion

    latest = RuleSetVersion.objects.filter(tenant_id=tenant_id).order_by("-version").first()
    next_version = (latest.version + 1) if latest else 1

    rules = Rule.objects.filter(
        tenant_id=tenant_id, state__in=[State.ACTIVE, State.SHADOW]
    ).order_by("priority", "id")

    snapshot = [
        {
            "id": rule.id,
            "name": rule.name,
            "expression": rule.expression,
            "severity": rule.severity,
            "state": rule.state,
            "priority": rule.priority,
            "regulatory_reference": rule.regulatory_reference,
            "customer_message": rule.customer_message,
        }
        for rule in rules
    ]

    lists = {
        entry.name: entry.values
        for entry in NamedList.objects.filter(tenant_id=tenant_id).order_by("name")
    }

    return RuleSetVersion.objects.create(
        tenant_id=tenant_id,
        version=next_version,
        rules_snapshot=snapshot,
        lists_snapshot=lists,
        published_by=actor.id,
    )


def revert(*, tenant_id: str, actor: Actor, to_version: int):
    """Publish a new version whose content equals an older one.

    Forward, never backward. Rewinding would mean deleting versions, and a
    version somebody's decision references is not a thing that may be deleted.
    """
    from complylayer.models import RuleSetVersion

    require(actor, Action.REVERT)

    source = RuleSetVersion.objects.filter(tenant_id=tenant_id, version=to_version).first()
    if source is None:
        raise LifecycleError(f"there is no version {to_version} to revert to")

    latest = RuleSetVersion.objects.filter(tenant_id=tenant_id).order_by("-version").first()
    new_version = RuleSetVersion.objects.create(
        tenant_id=tenant_id,
        version=latest.version + 1,
        rules_snapshot=source.rules_snapshot,
        lists_snapshot=source.lists_snapshot,
        published_by=actor.id,
    )

    audit.append(
        tenant_id=tenant_id,
        event_type="ruleset.reverted",
        actor=actor.as_audit_actor(),
        subject={"type": "ruleset", "id": str(new_version.version)},
        payload={"reverted_to": to_version, "new_version": new_version.version},
    )
    return new_version


def _validate_expression(expression: str) -> None:
    """Parsed and validated here, at publish time, never on the decision path."""
    try:
        validate_source(expression)
    except RuleSyntaxError:
        raise
