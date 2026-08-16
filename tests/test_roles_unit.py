"""The permission matrix and the hash chain, without a database.

`tests/test_lifecycle.py` and `tests/test_audit.py` prove these work against real
Postgres. Both modules are pure enough to test on a clean checkout with nothing
running, and the permission matrix in particular deserves a test that reads like
§10.2's table — because that is what it is.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from complylayer.audit.chain import GENESIS, VerificationResult, compute_hash, verify_chain
from complylayer.tenancy import Action, Actor, PermissionDenied, Role, may, require
from complylayer.tenancy.roles import PERMISSIONS, require_not_author

# §10.2's table, transcribed. Rewriting it here rather than importing PERMISSIONS
# is the point: a test that derives its expectation from the code under test
# passes no matter what the code says.
MATRIX = {
    #                        create  backtest  shadow  approve  activate  override
    Role.COMPLIANCE_ANALYST: (True, True, True, False, False, False),
    Role.COMPLIANCE_OFFICER: (True, True, True, True, True, False),
    Role.RISK_MANAGER: (True, True, True, True, True, True),
    Role.ENGINEER: (False, True, False, False, False, False),
    Role.AUDITOR: (False, True, False, False, False, False),
}

COLUMNS = (
    Action.CREATE_DRAFT,
    Action.BACKTEST,
    Action.SHADOW,
    Action.APPROVE,
    Action.ACTIVATE,
    Action.EMERGENCY_OVERRIDE,
)


class TestThePermissionMatrix:
    @pytest.mark.parametrize("role", list(MATRIX), ids=lambda role: str(role))
    def test_each_row_matches_the_specification(self, role: Role):
        expected = MATRIX[role]
        for action, allowed in zip(COLUMNS, expected, strict=True):
            assert may(role, action) is allowed, f"{role} / {action}"

    def test_the_engineer_row_is_the_point_of_the_product(self):
        """An engineer who has never read the regulation should not own its
        implementation, and "should not" has to mean "cannot"."""
        assert may(Role.ENGINEER, Action.CREATE_DRAFT) is False
        assert may(Role.ENGINEER, Action.ACTIVATE) is False
        assert may(Role.ENGINEER, Action.APPROVE) is False

    def test_everybody_can_backtest(self):
        """It reads history and changes nothing, and an engineer investigating a
        latency complaint needs it as much as a compliance officer does."""
        for role in Role:
            assert may(role, Action.BACKTEST) is True

    def test_only_the_risk_manager_holds_the_emergency_override(self):
        holders = [role for role in Role if may(role, Action.EMERGENCY_OVERRIDE)]
        assert holders == [Role.RISK_MANAGER]

    def test_every_role_has_an_entry(self):
        """A role missing from the matrix would raise a KeyError on its first
        permission check, in production, on somebody's first login."""
        assert set(PERMISSIONS) == set(Role)

    def test_every_action_is_granted_to_somebody(self):
        """An action nobody can perform is either dead code or a feature that
        cannot be used."""
        for action in Action:
            assert any(may(role, action) for role in Role), f"nobody can {action}"


class TestRequire:
    def test_it_passes_for_a_permitted_action(self):
        require(Actor("usr", Role.COMPLIANCE_OFFICER), Action.ACTIVATE)

    def test_it_refuses_with_a_readable_message(self):
        with pytest.raises(PermissionDenied) as exc:
            require(Actor("usr", Role.ENGINEER), Action.ACTIVATE)
        assert "engineer cannot activate" in str(exc.value)

    def test_the_refusal_carries_the_actor_and_action(self):
        """So the audit record for a refused attempt has something to say."""
        actor = Actor("usr", Role.AUDITOR)
        with pytest.raises(PermissionDenied) as exc:
            require(actor, Action.CREATE_DRAFT)
        assert exc.value.actor is actor
        assert exc.value.action is Action.CREATE_DRAFT


class TestSelfApproval:
    def test_the_author_is_refused_whatever_their_role(self):
        for role in (Role.COMPLIANCE_OFFICER, Role.RISK_MANAGER):
            with pytest.raises(PermissionDenied) as exc:
                require_not_author(Actor("usr_a", role), "usr_a", Action.APPROVE)
            assert "your own" in str(exc.value)

    def test_anybody_else_passes(self):
        require_not_author(Actor("usr_b", Role.COMPLIANCE_OFFICER), "usr_a", Action.APPROVE)

    def test_the_message_explains_why_rather_than_just_refusing(self):
        with pytest.raises(PermissionDenied) as exc:
            require_not_author(Actor("usr_a", Role.RISK_MANAGER), "usr_a", Action.APPROVE)
        assert "single person" in str(exc.value)


class TestAuditActor:
    def test_it_carries_everything_an_audit_record_needs(self):
        actor = Actor("usr_adaeze", Role.COMPLIANCE_OFFICER, ip="10.0.0.1")
        recorded = actor.as_audit_actor()
        assert recorded == {
            "type": "user",
            "id": "usr_adaeze",
            "role": "compliance_officer",
            "ip": "10.0.0.1",
        }


class TestChainVerificationWithoutADatabase:
    """`verify_chain` takes anything with the right attributes, so the walk
    itself is testable without persisting a single row."""

    class Record:
        def __init__(self, index: int, prev_hash: str, **overrides):
            self.id = f"aud_{index}"
            self.tenant_id = "tnt_1"
            self.event_type = overrides.get("event_type", "rule.activated")
            self.occurred_at = datetime(2026, 8, 16, 12, index, tzinfo=UTC)
            self.actor = overrides.get("actor", {"id": "usr"})
            self.subject = {"id": "rul_1"}
            self.payload = overrides.get("payload", {})
            self.prev_hash = prev_hash
            self.hash = compute_hash(
                record_id=self.id,
                tenant_id=self.tenant_id,
                event_type=self.event_type,
                occurred_at=self.occurred_at,
                actor=self.actor,
                subject=self.subject,
                payload=self.payload,
                prev_hash=self.prev_hash,
            )

    def build(self, count: int) -> list:
        records = []
        prev = GENESIS
        for index in range(count):
            record = self.Record(index, prev)
            records.append(record)
            prev = record.hash
        return records

    def test_an_intact_chain_verifies(self):
        result = verify_chain(self.build(5))
        assert result == VerificationResult(ok=True, checked=5)

    def test_an_empty_chain_verifies(self):
        assert verify_chain([]).ok is True

    def test_a_chain_that_does_not_start_at_genesis_is_refused(self):
        """Which is what a truncated chain looks like — the oldest records gone."""
        records = self.build(3)
        result = verify_chain(records[1:])
        assert result.ok is False
        assert result.checked == 0

    def test_content_tampering_is_caught(self):
        records = self.build(4)
        records[2].payload = {"threshold": "changed"}
        result = verify_chain(records)
        assert result.broken_at == records[2].id
        assert "changed after it was written" in result.detail

    def test_a_broken_link_is_caught_even_when_content_is_intact(self):
        """Re-pointing a record at a different predecessor, without touching
        anything else."""
        records = self.build(4)
        records[2].prev_hash = GENESIS
        result = verify_chain(records)
        assert result.ok is False
        assert records[2].id == result.broken_at

    def test_verification_stops_at_the_first_break(self):
        records = self.build(10)
        records[3].payload = {"a": 1}
        records[7].payload = {"b": 2}
        assert verify_chain(records).checked == 3
