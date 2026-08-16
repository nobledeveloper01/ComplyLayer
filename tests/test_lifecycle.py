"""The approval workflow, and whether separation of duties is real.

§10.2 is a table in a specification. This is the test that decides whether it is
also true. The cases that matter are not "can a compliance officer activate a
rule" — they are the ones where somebody with legitimate access tries to move a
control on their own.
"""

from __future__ import annotations

import pytest

from complylayer import audit, rules
from complylayer.dsl import RuleSyntaxError
from complylayer.engine.evaluation import State
from complylayer.models import AuditRecord, NamedList, Rule, RuleSetVersion, Tenant
from complylayer.rules.lifecycle import LifecycleError
from complylayer.tenancy import Action, Actor, PermissionDenied, Role, may

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

ANALYST = Actor(id="usr_analyst", role=Role.COMPLIANCE_ANALYST)
OFFICER = Actor(id="usr_officer", role=Role.COMPLIANCE_OFFICER)
OTHER_OFFICER = Actor(id="usr_officer_two", role=Role.COMPLIANCE_OFFICER)
RISK = Actor(id="usr_risk", role=Role.RISK_MANAGER)
ENGINEER = Actor(id="usr_engineer", role=Role.ENGINEER)
AUDITOR = Actor(id="usr_auditor", role=Role.AUDITOR)


@pytest.fixture
def tenant() -> Tenant:
    return Tenant.objects.create(id="tnt_life", name="Lifecycle")


def draft(tenant, actor=OFFICER, expression="amount_minor > 5_000_000", **overrides):
    return rules.create_draft(
        tenant_id=tenant.id,
        actor=actor,
        name=overrides.get("name", "Tier 2 limit"),
        category="kyc",
        expression=expression,
        severity=overrides.get("severity", "block"),
        priority=overrides.get("priority", 10),
    )


class TestTheEngineerRow:
    """The row that is the whole point of the product.

    An engineer who has never read the regulation should not own its
    implementation, and "should not" has to mean "cannot".
    """

    def test_an_engineer_cannot_create_a_rule(self, tenant):
        with pytest.raises(PermissionDenied) as exc:
            draft(tenant, actor=ENGINEER)
        assert "engineer" in str(exc.value)

    def test_an_engineer_cannot_activate_a_rule(self, tenant):
        rule = draft(tenant)
        rules.approve(rule=rule, actor=OTHER_OFFICER)
        with pytest.raises(PermissionDenied):
            rules.activate(rule=rule, actor=ENGINEER)

    def test_an_engineer_cannot_approve(self, tenant):
        rule = draft(tenant)
        with pytest.raises(PermissionDenied):
            rules.approve(rule=rule, actor=ENGINEER)

    def test_an_engineer_can_backtest(self):
        """They need it to investigate a latency complaint, and it changes
        nothing."""
        assert may(Role.ENGINEER, Action.BACKTEST) is True

    def test_an_auditor_can_only_read_and_backtest(self):
        assert may(Role.AUDITOR, Action.BACKTEST) is True
        for action in (Action.CREATE_DRAFT, Action.APPROVE, Action.ACTIVATE, Action.ARCHIVE):
            assert may(Role.AUDITOR, action) is False


class TestSelfApproval:
    def test_an_author_cannot_approve_their_own_rule(self, tenant):
        rule = draft(tenant, actor=OFFICER)
        with pytest.raises(PermissionDenied) as exc:
            rules.approve(rule=rule, actor=OFFICER)
        assert "your own" in str(exc.value)

    def test_a_different_officer_can(self, tenant):
        rule = draft(tenant, actor=OFFICER)
        approved = rules.approve(rule=rule, actor=OTHER_OFFICER)
        assert approved.approved_by == OTHER_OFFICER.id

    def test_a_risk_manager_cannot_self_approve_either(self, tenant):
        """A deliberate departure from §10.2's table, which gives the risk
        manager an unqualified tick.

        B4's acceptance criterion says the author cannot self-approve, full
        stop, and that is the criterion that matches why approvals exist. A
        role that can author and approve alone is a role that can weaken a
        control alone.
        """
        rule = draft(tenant, actor=RISK)
        with pytest.raises(PermissionDenied):
            rules.approve(rule=rule, actor=RISK)

    def test_an_analyst_cannot_approve_at_all(self, tenant):
        rule = draft(tenant, actor=OFFICER)
        with pytest.raises(PermissionDenied):
            rules.approve(rule=rule, actor=ANALYST)


class TestEditingResetsApproval:
    def test_an_edit_after_approval_clears_it(self, tenant):
        """The easiest way to defeat four-eyes review: get something harmless
        approved, then change it. Closed in the state machine rather than in
        the UI, because the UI is not the only client."""
        rule = draft(tenant, actor=ANALYST)
        rules.approve(rule=rule, actor=OFFICER)
        assert rule.approved_by == OFFICER.id

        edited = rules.edit_draft(rule=rule, actor=ANALYST, expression="amount_minor > 50_000_000")
        assert edited.approved_by == ""
        assert edited.approved_at is None

    def test_the_reset_is_recorded(self, tenant):
        rule = draft(tenant, actor=ANALYST)
        rules.approve(rule=rule, actor=OFFICER)
        rules.edit_draft(rule=rule, actor=ANALYST, expression="amount_minor > 50_000_000")

        record = AuditRecord.objects.filter(event_type="rule.edited").latest("recorded_at")
        assert record.payload["approval_reset"] is True

    def test_an_edited_rule_cannot_then_be_activated(self, tenant):
        rule = draft(tenant, actor=ANALYST)
        rules.approve(rule=rule, actor=OFFICER)
        rules.edit_draft(rule=rule, actor=ANALYST, expression="amount_minor > 50_000_000")

        with pytest.raises(LifecycleError) as exc:
            rules.activate(rule=rule, actor=OFFICER)
        assert "not been approved" in str(exc.value)

    def test_an_edit_records_what_changed(self, tenant):
        """The approval diff is the highest-stakes screen in the product."""
        rule = draft(tenant, expression="amount_minor > 5_000_000")
        rules.edit_draft(rule=rule, actor=OFFICER, expression="amount_minor > 50_000_000")

        record = AuditRecord.objects.filter(event_type="rule.edited").latest("recorded_at")
        change = record.payload["changes"][0]
        assert change["field"] == "expression"
        assert change["before"] == "amount_minor > 5_000_000"
        assert change["after"] == "amount_minor > 50_000_000"

    def test_an_edit_that_changes_nothing_is_not_recorded(self, tenant):
        rule = draft(tenant)
        before = AuditRecord.objects.count()
        rules.edit_draft(rule=rule, actor=OFFICER, expression=rule.expression)
        assert AuditRecord.objects.count() == before

    def test_an_active_rule_cannot_be_edited_in_place(self, tenant):
        rule = draft(tenant, actor=ANALYST)
        rules.approve(rule=rule, actor=OFFICER)
        rules.activate(rule=rule, actor=OFFICER)

        with pytest.raises(LifecycleError) as exc:
            rules.edit_draft(rule=rule, actor=OFFICER, expression="amount_minor > 1")
        assert "visible in the trail" in str(exc.value)


class TestActivation:
    def test_an_unapproved_rule_cannot_be_activated(self, tenant):
        rule = draft(tenant)
        with pytest.raises(LifecycleError) as exc:
            rules.activate(rule=rule, actor=OTHER_OFFICER)
        assert "emergency override" in str(exc.value)

    def test_activation_publishes_a_rule_set_version(self, tenant):
        rule = draft(tenant, actor=ANALYST)
        rules.approve(rule=rule, actor=OFFICER)
        activated, version = rules.activate(rule=rule, actor=OFFICER)

        assert activated.state == State.ACTIVE
        assert version.version == 1
        assert version.rules_snapshot[0]["id"] == rule.id

    def test_each_activation_publishes_a_new_version(self, tenant):
        for index in range(3):
            rule = draft(tenant, actor=ANALYST, name=f"Rule {index}")
            rules.approve(rule=rule, actor=OFFICER)
            rules.activate(rule=rule, actor=OFFICER)

        versions = list(
            RuleSetVersion.objects.order_by("version").values_list("version", flat=True)
        )
        assert versions == [1, 2, 3]

    def test_named_lists_are_snapshotted_with_the_rules(self, tenant):
        """D11. Editing a list has to publish a version, or two decisions
        recording the same version would not mean the same control."""
        NamedList.objects.create(
            tenant=tenant, name="high_risk_countries", values=["XX", "YY"], updated_by=OFFICER.id
        )
        rule = draft(tenant, actor=ANALYST)
        rules.approve(rule=rule, actor=OFFICER)
        _, version = rules.activate(rule=rule, actor=OFFICER)

        assert version.lists_snapshot == {"high_risk_countries": ["XX", "YY"]}

    def test_a_shadow_rule_is_in_the_snapshot_but_marked_shadow(self, tenant):
        shadow = draft(tenant, actor=ANALYST, name="Shadow")
        rules.move_to_shadow(rule=shadow, actor=OFFICER)

        live = draft(tenant, actor=ANALYST, name="Live")
        rules.approve(rule=live, actor=OFFICER)
        _, version = rules.activate(rule=live, actor=OFFICER)

        states = {entry["id"]: entry["state"] for entry in version.rules_snapshot}
        assert states[shadow.id] == "shadow"
        assert states[live.id] == "active"

    def test_a_draft_never_reaches_the_snapshot(self, tenant):
        draft(tenant, actor=ANALYST, name="Still a draft")
        live = draft(tenant, actor=ANALYST, name="Live")
        rules.approve(rule=live, actor=OFFICER)
        _, version = rules.activate(rule=live, actor=OFFICER)

        assert [entry["id"] for entry in version.rules_snapshot] == [live.id]


class TestEmergencyOverride:
    def test_it_needs_a_written_reason(self, tenant):
        rule = draft(tenant)
        with pytest.raises(LifecycleError):
            rules.activate(rule=rule, actor=RISK, emergency_reason="")

    def test_only_a_risk_manager_may_use_it(self, tenant):
        rule = draft(tenant, actor=ANALYST)
        with pytest.raises(PermissionDenied):
            rules.activate(rule=rule, actor=OFFICER, emergency_reason="regulator called")

    def test_a_risk_manager_can_activate_without_approval(self, tenant):
        rule = draft(tenant, actor=ANALYST)
        activated, _ = rules.activate(
            rule=rule, actor=RISK, emergency_reason="regulator instruction, effective immediately"
        )
        assert activated.state == State.ACTIVE

    def test_it_is_recorded_as_a_different_event(self, tenant):
        """§11.4 pages the risk lead on any occurrence. A page needs an event it
        can match on, not a field buried in a payload."""
        rule = draft(tenant, actor=ANALYST)
        rules.activate(rule=rule, actor=RISK, emergency_reason="regulator instruction")

        record = AuditRecord.objects.filter(event_type="rule.emergency_activated").first()
        assert record is not None
        assert record.payload["emergency_reason"] == "regulator instruction"


class TestRevert:
    def test_reverting_publishes_a_new_version_rather_than_rewinding(self, tenant):
        """A version somebody's decision references is not a thing that may be
        deleted."""
        first = draft(tenant, actor=ANALYST, name="First")
        rules.approve(rule=first, actor=OFFICER)
        _, version_one = rules.activate(rule=first, actor=OFFICER)

        second = draft(tenant, actor=ANALYST, name="Second")
        rules.approve(rule=second, actor=OFFICER)
        _, version_two = rules.activate(rule=second, actor=OFFICER)

        reverted = rules.revert(tenant_id=tenant.id, actor=OFFICER, to_version=version_one.version)

        assert reverted.version == version_two.version + 1
        assert reverted.rules_snapshot == version_one.rules_snapshot
        assert RuleSetVersion.objects.filter(version=version_one.version).exists()

    def test_reverting_to_a_version_that_does_not_exist(self, tenant):
        with pytest.raises(LifecycleError):
            rules.revert(tenant_id=tenant.id, actor=OFFICER, to_version=99)

    def test_an_analyst_cannot_revert(self, tenant):
        with pytest.raises(PermissionDenied):
            rules.revert(tenant_id=tenant.id, actor=ANALYST, to_version=1)


class TestValidation:
    def test_a_rule_with_a_bad_expression_is_refused_at_creation(self, tenant):
        with pytest.raises(RuleSyntaxError) as exc:
            draft(tenant, expression="customer.kyc_tier > 2")
        assert "dot" in str(exc.value)

    def test_and_on_edit(self, tenant):
        rule = draft(tenant)
        with pytest.raises(RuleSyntaxError):
            rules.edit_draft(rule=rule, actor=OFFICER, expression="().__class__")

    def test_the_bad_rule_is_not_persisted(self, tenant):
        with pytest.raises(RuleSyntaxError):
            draft(tenant, expression="().__class__.__bases__[0]")
        assert Rule.objects.count() == 0


class TestEverythingIsAudited:
    def test_the_whole_lifecycle_leaves_a_verifiable_trail(self, tenant):
        """§9 maps "evidence of which controls were in force" to this trail. A
        regulator's question is answered by these records, not by the current
        state of the rules table."""
        rule = draft(tenant, actor=ANALYST)
        rules.request_approval(rule=rule, actor=ANALYST)
        rules.approve(rule=rule, actor=OFFICER)
        rules.activate(rule=rule, actor=OFFICER)
        rules.archive(rule=rule, actor=OFFICER, reason="superseded")

        events = list(
            AuditRecord.objects.filter(tenant_id=tenant.id)
            .order_by("recorded_at")
            .values_list("event_type", flat=True)
        )
        assert events == [
            "rule.created",
            "rule.approval_requested",
            "rule.approved",
            "rule.activated",
            "rule.archived",
        ]
        assert audit.verify(tenant.id).ok is True

    def test_every_record_names_the_actor_and_their_role(self, tenant):
        rule = draft(tenant, actor=ANALYST)
        rules.approve(rule=rule, actor=OFFICER)

        record = AuditRecord.objects.filter(event_type="rule.approved").first()
        assert record.actor["id"] == OFFICER.id
        assert record.actor["role"] == "compliance_officer"

    def test_the_approval_record_names_the_author_it_approved(self, tenant):
        """So a reviewer can see the two people involved without joining back
        to the rules table, which may have moved on since."""
        rule = draft(tenant, actor=ANALYST)
        rules.approve(rule=rule, actor=OFFICER)

        record = AuditRecord.objects.filter(event_type="rule.approved").first()
        assert record.payload["author"] == ANALYST.id
