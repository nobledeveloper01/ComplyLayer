"""Per-rule analytics: which rules are worth their noise.

§3.2's D2 asks for fire rate, false-positive rate and latency contribution per
rule. The middle one is the reason this exists: a rule that fires 400 times a
week and is cleared 390 times is not catching anything, it is spending a
reviewer's week — and nothing else in the product would show that.

The false-positive rate is derived from recorded review outcomes rather than
guessed, which is why §3.2's D1 insists the review queue record an outcome at
all. A cleared flag is a rule being wrong; a confirmed flag is a rule being
right. Without the outcome there is no denominator.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RulePerformance:
    rule_id: str
    name: str
    fired: int
    reviewed: int
    cleared: int
    confirmed: int

    @property
    def false_positive_rate(self) -> float | None:
        """Cleared reviews over reviewed flags.

        None rather than zero when nothing has been reviewed. A rule with no
        reviews has an unknown rate, and reporting 0% would rank it as the best
        performing rule in the tenant.
        """
        if self.reviewed == 0:
            return None
        return self.cleared / self.reviewed

    @property
    def verdict(self) -> str:
        """The sentence a risk manager acts on."""
        rate = self.false_positive_rate
        if rate is None:
            return f"Fired {self.fired:,} times. None reviewed yet, so its accuracy is unknown."
        if rate >= 0.9:
            return (
                f"Fired {self.fired:,} times and {rate:.0%} were cleared on review. "
                "This rule is spending reviewer time rather than catching anything — "
                "consider tightening it or moving it to shadow."
            )
        if rate >= 0.6:
            return (
                f"Fired {self.fired:,} times, {rate:.0%} cleared. Worth tightening if "
                "the review queue is under pressure."
            )
        return f"Fired {self.fired:,} times, {rate:.0%} cleared. This rule is earning its place."


def performance(decisions: Iterable[Any], names: dict[str, str]) -> list[RulePerformance]:
    """Rank rules by how much reviewer time they cost against what they catch.

    Ordered worst-first: the page exists to answer "which rule is drowning my
    queue", and that rule should not be somewhere on page two.
    """
    fired: dict[str, int] = {}
    reviewed: dict[str, int] = {}
    cleared: dict[str, int] = {}

    for decision in decisions:
        for matched in decision.matched_rules or []:
            rule_id = matched["id"] if isinstance(matched, dict) else matched
            fired[rule_id] = fired.get(rule_id, 0) + 1

            if not decision.review_status:
                continue
            reviewed[rule_id] = reviewed.get(rule_id, 0) + 1
            if decision.review_status == "cleared":
                cleared[rule_id] = cleared.get(rule_id, 0) + 1

    results = [
        RulePerformance(
            rule_id=rule_id,
            name=names.get(rule_id, rule_id),
            fired=count,
            reviewed=reviewed.get(rule_id, 0),
            cleared=cleared.get(rule_id, 0),
            confirmed=reviewed.get(rule_id, 0) - cleared.get(rule_id, 0),
        )
        for rule_id, count in fired.items()
    ]

    # Worst first: unknown accuracy sorts below a known-bad rule, because a rule
    # nobody has reviewed is not evidence of anything.
    return sorted(
        results,
        key=lambda r: (-(r.false_positive_rate or 0), -r.fired),
    )
