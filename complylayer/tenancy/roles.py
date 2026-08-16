"""Who may do what to a compliance rule.

§10.2's table, in code. The row that matters most is the engineer's: **an
engineer cannot create or activate a compliance rule.** That is not an oversight
in the permission matrix, it is the entire point of the product — the person who
owns the regulatory risk should be able to change the control, and the person who
does not own it should not.

**One deliberate deviation from the specification.** §10.2's table gives the Risk
Manager an unqualified tick for Approve, while the Compliance Officer's is marked
"(not own)". Read literally, a risk manager could approve their own rule change.
That contradicts B4's acceptance criterion — *"The author cannot self-approve"* —
and it contradicts the reason approvals exist at all: so that no single person
can unilaterally weaken a control.

The acceptance criterion wins. Nobody approves their own change, whatever their
role. A risk manager who needs a change approved asks a compliance officer, which
is a five-minute inconvenience against the alternative of a control that one
person can move on their own. The emergency override exists for the case where
five minutes is genuinely too long, and it pages the risk lead precisely because
it is the exception.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    COMPLIANCE_ANALYST = "compliance_analyst"
    COMPLIANCE_OFFICER = "compliance_officer"
    RISK_MANAGER = "risk_manager"
    ENGINEER = "engineer"
    AUDITOR = "auditor"


class Action(StrEnum):
    CREATE_DRAFT = "create_draft"
    EDIT_DRAFT = "edit_draft"
    BACKTEST = "backtest"
    SHADOW = "shadow"
    REQUEST_APPROVAL = "request_approval"
    APPROVE = "approve"
    ACTIVATE = "activate"
    ARCHIVE = "archive"
    REVERT = "revert"
    EMERGENCY_OVERRIDE = "emergency_override"
    REVIEW_DECISION = "review_decision"


# Everyone can backtest. It is a read of history that changes nothing, and an
# engineer investigating a latency complaint needs it as much as a compliance
# officer testing a threshold does.
_EVERYONE = frozenset({Action.BACKTEST})

PERMISSIONS: dict[Role, frozenset[Action]] = {
    Role.COMPLIANCE_ANALYST: _EVERYONE
    | {Action.CREATE_DRAFT, Action.EDIT_DRAFT, Action.SHADOW, Action.REQUEST_APPROVAL},
    Role.COMPLIANCE_OFFICER: _EVERYONE
    | {
        Action.CREATE_DRAFT,
        Action.EDIT_DRAFT,
        Action.SHADOW,
        Action.REQUEST_APPROVAL,
        Action.APPROVE,
        Action.ACTIVATE,
        Action.ARCHIVE,
        Action.REVERT,
        Action.REVIEW_DECISION,
    },
    Role.RISK_MANAGER: _EVERYONE
    | {
        Action.CREATE_DRAFT,
        Action.EDIT_DRAFT,
        Action.SHADOW,
        Action.REQUEST_APPROVAL,
        Action.APPROVE,
        Action.ACTIVATE,
        Action.ARCHIVE,
        Action.REVERT,
        Action.EMERGENCY_OVERRIDE,
        Action.REVIEW_DECISION,
    },
    # Read and backtest. Nothing that changes a control.
    Role.ENGINEER: _EVERYONE,
    Role.AUDITOR: _EVERYONE,
}


@dataclass(frozen=True)
class Actor:
    """Who is asking. Carried explicitly rather than read from a thread local,
    because every audit record needs it and an implicit actor is an actor
    somebody eventually forgets to record."""

    id: str
    role: Role
    ip: str = ""

    def as_audit_actor(self) -> dict[str, str]:
        return {"type": "user", "id": self.id, "role": str(self.role), "ip": self.ip}


class PermissionDenied(PermissionError):
    """Refused on role, or refused because the actor is the author."""

    def __init__(self, message: str, actor: Actor, action: Action):
        self.actor = actor
        self.action = action
        super().__init__(message)


def may(role: Role, action: Action) -> bool:
    return action in PERMISSIONS[role]


def require(actor: Actor, action: Action) -> None:
    if not may(actor.role, action):
        raise PermissionDenied(
            f"a {actor.role.replace('_', ' ')} cannot {action.replace('_', ' ')}",
            actor,
            action,
        )


def require_not_author(actor: Actor, author_id: str, action: Action) -> None:
    """Separation of duties, enforced rather than documented.

    Applies to every role including risk manager — see the module docstring for
    why that departs from §10.2's table.
    """
    if actor.id == author_id:
        raise PermissionDenied(
            "you cannot approve your own change; approval exists so that no single "
            "person can weaken a control on their own",
            actor,
            action,
        )
