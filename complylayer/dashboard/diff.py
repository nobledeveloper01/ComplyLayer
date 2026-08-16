"""The approval diff.

Its own module, because the plan review found it getting one line in the roadmap
and it is the screen where a risk manager either catches a weakened control or
does not.

**It is not a text diff.** `- amount_minor > 5_000_000` above
`+ amount_minor > 50_000_000` is technically complete and practically useless: a
reviewer scanning red and green lines sees a one-character change and approves
it. The number moved by ten times and the diff said so in one digit.

What this produces instead is the change in the unit a human thinks in, the
direction it moved, and by how much — because a limit going *up* is a control
being weakened, and that is the single most important fact on the screen.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from decimal import Decimal

from complylayer.dsl import parse

# Minor units per major unit. Naira and most currencies here are 100; the
# exceptions matter because rendering ¥1,000 as ¥10.00 is the kind of error that
# gets noticed by a customer rather than a test.
MINOR_UNITS = {"NGN": 100, "USD": 100, "EUR": 100, "GBP": 100, "KES": 100, "JPY": 1, "GHS": 100}
SYMBOLS = {"NGN": "₦", "USD": "$", "EUR": "€", "GBP": "£", "KES": "KSh", "JPY": "¥", "GHS": "₵"}

# Fact names whose values are money. A threshold compared against one of these
# is a limit, and gets rendered as currency rather than as a bare integer.
AMOUNT_FACTS = re.compile(r"(_minor|amount|balance|limit|threshold)$")


@dataclass(frozen=True)
class ThresholdChange:
    fact: str
    before: int
    after: int
    currency: str = "NGN"

    @property
    def is_amount(self) -> bool:
        return bool(AMOUNT_FACTS.search(self.fact))

    @property
    def looser(self) -> bool:
        """A higher limit lets more through. That is a control being weakened.

        Stated as its own property because the whole screen hangs off it, and
        because "looser" is the word a reviewer thinks in — not "increased".
        """
        return self.after > self.before

    @property
    def factor(self) -> Decimal | None:
        if self.before == 0:
            return None
        return (Decimal(self.after) / Decimal(self.before)).quantize(Decimal("0.01"))

    def render(self, value: int) -> str:
        if not self.is_amount:
            return f"{value:,}"
        divisor = MINOR_UNITS.get(self.currency, 100)
        symbol = SYMBOLS.get(self.currency, "")
        if divisor == 1:
            return f"{symbol}{value:,}"
        major = Decimal(value) / Decimal(divisor)
        return f"{symbol}{major:,.2f}"

    @property
    def magnitude(self) -> str:
        """The sentence a reviewer actually reads.

        `10× higher`, not `+45,000,000`. A multiple is the form a human weighs;
        a delta in minor units is a number they have to do arithmetic on while
        deciding whether to approve.
        """
        factor = self.factor
        if factor is None:
            return "higher" if self.looser else "lower"

        direction = "higher" if self.looser else "lower"
        if not self.looser:
            factor = Decimal(1) / factor if factor else factor
            factor = factor.quantize(Decimal("0.01"))

        if factor >= 2:
            return f"{_trim(factor)}× {direction}"

        percent = abs(Decimal(self.after - self.before) / Decimal(self.before) * 100)
        return f"{percent.quantize(Decimal('1'))}% {direction}"


def _trim(value: Decimal) -> str:
    """Render 2.50 as 2.5 and 3.00 as 3, without mangling 10.

    Written after `"10".rstrip("0")` produced `"1"` — on the one screen whose
    entire job is showing that a limit moved tenfold, which it would have
    understated by an order of magnitude. Only strip inside a fraction.
    """
    text = f"{value:f}"
    if "." not in text:
        return text
    return text.rstrip("0").rstrip(".")


@dataclass(frozen=True)
class RuleDiff:
    before: str
    after: str
    threshold: ThresholdChange | None
    changed_tokens: tuple[str, ...]
    regulatory_reference: str = ""
    author: str = ""
    reason: str = ""

    @property
    def is_threshold_only(self) -> bool:
        """The common case, and the dangerous one.

        A rule whose *structure* changed is visibly different. A rule where one
        number moved looks almost identical, which is exactly why the number
        gets rendered at display size.
        """
        return self.threshold is not None and len(self.changed_tokens) == 1


def _numbers(expression: str) -> list[tuple[str, int]]:
    """Every comparison of a name against an integer, in order."""
    found: list[tuple[str, int]] = []
    tree = parse(expression)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        name = left.id if isinstance(left, ast.Name) else None
        if name is None and isinstance(left, ast.Call) and isinstance(left.func, ast.Name):
            name = left.func.id
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, int):
                found.append((name or "", comparator.value))
    return found


def compare(
    before: str,
    after: str,
    *,
    currency: str = "NGN",
    regulatory_reference: str = "",
    author: str = "",
    reason: str = "",
) -> RuleDiff:
    """Work out what actually changed between two versions of a rule."""
    before_numbers = _numbers(before)
    after_numbers = _numbers(after)

    threshold: ThresholdChange | None = None
    if len(before_numbers) == len(after_numbers):
        differences = [
            (name, old, new)
            for (name, old), (_, new) in zip(before_numbers, after_numbers, strict=True)
            if old != new
        ]
        if len(differences) == 1:
            name, old, new = differences[0]
            threshold = ThresholdChange(fact=name, before=old, after=new, currency=currency)

    before_tokens = before.split()
    after_tokens = after.split()
    changed = tuple(token for token in after_tokens if token not in before_tokens)

    return RuleDiff(
        before=before,
        after=after,
        threshold=threshold,
        changed_tokens=changed,
        regulatory_reference=regulatory_reference,
        author=author,
        reason=reason,
    )


@dataclass(frozen=True)
class BacktestImpact:
    """What the change would have done, over real history.

    A reviewer weighing "10× higher" is weighing an abstraction until they see
    that it means 1,204 transactions rather than 118.
    """

    total: int
    before_matches: int
    after_matches: int

    @property
    def delta(self) -> int:
        return self.after_matches - self.before_matches

    @property
    def sentence(self) -> str:
        verb = "more" if self.delta > 0 else "fewer"
        return (
            f"Would have matched {self.after_matches:,} of {self.total:,} transactions, "
            f"{abs(self.delta):,} {verb} than the current rule."
        )
