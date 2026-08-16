"""The regulator-ready export.

§3.2's D3: the rule set, its complete change history, decision volumes, and an
attestation that the audit chain verifies. One file a supervisor can be handed.

**CSV formula injection is defused.** A cell beginning `=`, `+`, `-` or `@` is
executed as a formula by Excel and Sheets, so an export containing a rule named
`=cmd|...` runs on the reviewer's machine. Prefixed with a single quote, which
Excel treats as "this is text" and displays without it. Cheap, and the kind of
thing that is embarrassing to learn about from a customer.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from typing import Any

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe(value: Any) -> str:
    text = "" if value is None else str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def rules_csv(rules: Iterable[Any]) -> str:
    """The rule set as it stands, with the regulation each rule claims."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "rule_id",
            "name",
            "state",
            "severity",
            "expression",
            "regulatory_reference",
            "created_by",
            "approved_by",
            "activated_at",
        ]
    )
    for rule in rules:
        writer.writerow(
            [
                _safe(rule.id),
                _safe(rule.name),
                _safe(rule.state),
                _safe(rule.severity),
                _safe(rule.expression),
                _safe(rule.regulatory_reference),
                _safe(rule.created_by),
                _safe(rule.approved_by),
                _safe(rule.activated_at),
            ]
        )
    return buffer.getvalue()


def history_csv(records: Iterable[Any]) -> str:
    """Every change to every control, in order, with who made it.

    This is the artefact §9 maps "evidence of which controls were in force at a
    given time" to. It is the audit trail, exported.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["recorded_at", "event", "actor", "role", "subject", "hash"])
    for record in records:
        writer.writerow(
            [
                _safe(record.recorded_at),
                _safe(record.event_type),
                _safe((record.actor or {}).get("id")),
                _safe((record.actor or {}).get("role")),
                _safe((record.subject or {}).get("id")),
                _safe(record.hash),
            ]
        )
    return buffer.getvalue()


def attestation(tenant_id: str, verification, volumes: dict[str, int]) -> str:
    """The cover note. Plain text, because a supervisor reads it, not a parser."""
    lines = [
        f"ComplyLayer compliance export — tenant {tenant_id}",
        "",
        "Audit chain",
        "-----------",
    ]
    if verification.ok:
        lines.append(
            f"  Verified. {verification.checked:,} records form an unbroken hash chain, "
            "each covering its own contents and the record before it."
        )
    else:
        # Stated plainly and first. An export whose chain does not verify is a
        # more important fact than anything else in the file.
        lines.append(f"  FAILED at record {verification.broken_at}. {verification.detail}")
        lines.append("  This export should not be relied upon until that is explained.")

    lines += ["", "Decision volumes", "----------------"]
    for outcome, count in sorted(volumes.items()):
        lines.append(f"  {outcome:<10} {count:>12,}")

    return "\n".join(lines) + "\n"
