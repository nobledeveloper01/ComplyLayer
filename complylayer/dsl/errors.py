"""The error catalogue.

These messages are read by a compliance officer, not by the person who wrote the
validator. That distinction is the entire design of this module.

``'Attribute' is not permitted in rules`` is a correct sentence and a useless
one. It names an implementation detail, offers no alternative, and leaves
somebody who was trying to raise a transaction limit staring at a word they have
no reason to know. The rule builder is the product's centre of gravity, so these
errors are the product's main error surface — and every one of them has to say
three things: what is wrong, why the rule cannot do that, and what to write
instead.
"""

from __future__ import annotations


class RuleSyntaxError(ValueError):
    """A rule expression was rejected.

    Carries the three parts separately as well as joined, so the dashboard can
    render them with different weight while the API and the logs get one string.
    """

    def __init__(self, problem: str, fix: str = "", reason: str = "", offset: int | None = None):
        self.problem = problem
        self.fix = fix
        self.reason = reason
        self.offset = offset
        super().__init__(self._render())

    def _render(self) -> str:
        parts = [self.problem]
        if self.fix:
            parts.append(self.fix)
        if self.reason:
            parts.append(f"({self.reason})")
        return " ".join(parts)

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "problem": self.problem,
            "fix": self.fix,
            "reason": self.reason,
            "offset": self.offset,
        }


def attribute_access(name: str | None = None) -> RuleSyntaxError:
    """The most common rejection by far, and the one worth writing most carefully.

    A compliance officer reaching for ``customer.kyc_tier`` is not attacking
    anything. They are writing the thing that would obviously work.
    """
    wrote = f"You wrote {name}. " if name else ""
    return RuleSyntaxError(
        problem=f"Rules cannot use a dot (.). {wrote}".strip(),
        fix="Facts are available by name on their own — write kyc_tier, not customer.kyc_tier. "
        "See the function reference for everything available.",
        reason="a dot is the main route a rule could use to break out of its sandbox",
    )


def subscript() -> RuleSyntaxError:
    return RuleSyntaxError(
        problem="Rules cannot use square brackets to pick an item out of a list.",
        fix="To test whether a value is in a list, use in_list(country, high_risk_countries).",
        reason="indexing is one of the steps an attacker needs, and no compliance rule needs it",
    )


def unknown_function(name: str, available: list[str]) -> RuleSyntaxError:
    suggestion = _closest(name, available)
    fix = f"Did you mean {suggestion}? " if suggestion else ""
    return RuleSyntaxError(
        problem=f"There is no function called {name}.",
        fix=f"{fix}Available functions: {', '.join(sorted(available))}.",
        reason="rules may only call functions ComplyLayer provides",
    )


def indirect_call() -> RuleSyntaxError:
    return RuleSyntaxError(
        problem="Rules can only call a function directly by name.",
        fix="Write velocity_count(window='1h'), not something that produces a function first.",
    )


def construct_not_allowed(construct: str) -> RuleSyntaxError:
    """The catch-all, still phrased for someone who did not write the parser."""
    friendly = CONSTRUCT_NAMES.get(construct, construct)
    return RuleSyntaxError(
        problem=f"Rules cannot use {friendly}.",
        fix="A rule is a single yes-or-no question about one transaction — a comparison, "
        "possibly several joined with and/or. Anything more belongs in a new function, "
        "which an engineer adds and reviews.",
        reason=f"{construct} is outside the permitted rule grammar",
    )


def statement_not_expression() -> RuleSyntaxError:
    return RuleSyntaxError(
        problem="A rule must be a single question, not an instruction.",
        fix="Write a comparison such as amount_minor > tier_daily_limit_minor. "
        "Assignments, imports and loops are not part of the rule language.",
    )


def empty_rule() -> RuleSyntaxError:
    return RuleSyntaxError(
        problem="The rule is empty.",
        fix="Write a comparison, for example amount_minor > 5_000_000.",
    )


def too_long(length: int, maximum: int) -> RuleSyntaxError:
    return RuleSyntaxError(
        problem=f"The rule is too long: {length} characters, and the limit is {maximum}.",
        fix="Split it into two rules. Two rules that each say one thing are also easier to "
        "approve, and easier to explain to a regulator.",
    )


def too_deep(maximum: int) -> RuleSyntaxError:
    return RuleSyntaxError(
        problem=f"The rule nests brackets more than {maximum} deep.",
        fix="Split it into two rules.",
        reason="deeply nested expressions are a denial-of-service risk before they are even read",
    )


def too_many_operators(maximum: int) -> RuleSyntaxError:
    return RuleSyntaxError(
        problem=f"The rule has a run of more than {maximum} operators in a row.",
        fix="Remove the repetition — 'not not x' is just 'x'.",
    )


def too_complex(nodes: int, maximum: int) -> RuleSyntaxError:
    return RuleSyntaxError(
        problem=f"The rule is too complex: {nodes} parts, and the limit is {maximum}.",
        fix="Split it into two rules.",
        reason="every rule shares one evaluation budget, so one large rule slows every decision",
    )


def malformed(detail: str = "") -> RuleSyntaxError:
    """Never let a raw Python SyntaxError reach a compliance officer."""
    return RuleSyntaxError(
        problem="The rule is not written correctly and could not be read.",
        fix=f"Check for a missing bracket or comparison. {detail}".strip(),
    )


CONSTRUCT_NAMES = {
    "Lambda": "an inline function (lambda)",
    "ListComp": "a list comprehension",
    "SetComp": "a set comprehension",
    "DictComp": "a dictionary comprehension",
    "GeneratorExp": "a generator expression",
    "JoinedStr": "a formatted string (f-string)",
    "FormattedValue": "a formatted string (f-string)",
    "Dict": "a dictionary",
    "Set": "a set literal",
    "Starred": "argument unpacking (*)",
    "IfExp": "an inline if/else",
    "Await": "await",
    "Yield": "yield",
    "YieldFrom": "yield from",
    "NamedExpr": "an assignment inside an expression (:=)",
    "Slice": "a slice",
    "Attribute": "a dot (.)",
    "Subscript": "square-bracket indexing",
}


def _closest(name: str, candidates: list[str]) -> str | None:
    """A cheap suggestion for a mistyped function name.

    Deliberately conservative: a wrong suggestion is worse than none, because it
    sends somebody looking for a function that does not do what they assumed.
    """
    import difflib

    matches = difflib.get_close_matches(name, candidates, n=1, cutoff=0.75)
    return matches[0] if matches else None


def unknown_keyword(function: str, keyword: str, available: list[str]) -> RuleSyntaxError:
    return RuleSyntaxError(
        problem=f"{function} has no setting called {keyword}.",
        fix=f"{function} accepts: {', '.join(available)}."
        if available
        else f"{function} takes no settings.",
    )


def reserved_name(name: str) -> RuleSyntaxError:
    """Names beginning with an underscore are Python's, not the rule language's."""
    return RuleSyntaxError(
        problem=f"{name} is not a fact you can use in a rule.",
        fix="Facts have plain names such as amount_minor, kyc_tier or destination_country. "
        "Names starting with an underscore belong to Python itself.",
        reason="reserved names are refused so a rule can never reach the runtime behind it",
    )
