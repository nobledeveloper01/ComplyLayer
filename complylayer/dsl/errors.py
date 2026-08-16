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


def non_ascii_name(name: str) -> RuleSyntaxError:
    """A rule that reads as one thing and evaluates as another is worse than a rejected one.

    Replace the first letter of ``amount_minor`` with U+0430 CYRILLIC SMALL
    LETTER A and it renders identically in every dashboard, diff and audit
    export, while resolving to an entirely different fact.
    An approver reading the rule would be approving a control that does not
    exist, which defeats the four-eyes review the product is sold on.
    """
    return RuleSyntaxError(
        problem=f"{name!r} contains characters that are not plain English letters.",
        fix="Fact and function names use a-z, 0-9 and underscores only. If you copied this "
        "from elsewhere, retype it.",
        reason="letters from other alphabets can look identical to English ones, so a rule "
        "could read one way and behave another",
    )


def wrong_arity(function: str, given: int, expected: int) -> RuleSyntaxError:
    plural = "value" if expected == 1 else "values"
    return RuleSyntaxError(
        problem=f"{function} needs {expected} {plural}, but was given {given}.",
        fix=f"Write {function}({', '.join('...' for _ in range(expected))})."
        if expected
        else f"Write {function}() with nothing in the brackets.",
    )


def duplicate_keyword(function: str, keyword: str) -> RuleSyntaxError:
    return RuleSyntaxError(
        problem=f"{function} was given {keyword} twice.",
        fix="Remove one of them.",
        reason="with two values there is no way to say which one the rule meant",
    )


def bad_literal(value: object) -> RuleSyntaxError:
    """Whole numbers and text only. See D6 in docs/plan-architecture.md."""
    kind = {
        float: "a decimal number",
        complex: "an imaginary number",
        bytes: "raw bytes",
        type(None): "nothing",
        type(...): "...",
    }.get(type(value), f"a {type(value).__name__}")
    return RuleSyntaxError(
        problem=f"Rules cannot contain {kind}.",
        fix="Amounts are whole numbers of minor units — write 1500000 for ₦15,000.00, "
        "not 15000.00.",
        reason="decimals cannot be compared exactly, and a decision has to reproduce "
        "identically years later",
    )


def non_numeric_operand() -> RuleSyntaxError:
    """`'a' * 999999999` is one step and a gigabyte. A step budget cannot see it."""
    return RuleSyntaxError(
        problem="Arithmetic in a rule works on whole numbers only.",
        fix="Compare amounts and counts, not text or lists.",
        reason="multiplying text or a list repeats it, which can exhaust memory in a single step",
    )


def division_by_zero() -> RuleSyntaxError:
    return RuleSyntaxError(
        problem="The rule divides by zero.",
        fix="Use a number other than zero.",
    )


def not_a_condition() -> RuleSyntaxError:
    return RuleSyntaxError(
        problem="This rule is a fixed value, so it would give the same answer every time.",
        fix="A rule asks a question about the transaction, such as amount_minor > 5_000_000.",
        reason="a rule that always fires blocks everything, and one that never fires is "
        "a control that is not there",
    )


def number_too_large(digits: int, maximum: int) -> RuleSyntaxError:
    return RuleSyntaxError(
        problem=f"That number has {digits} digits, and the limit is {maximum}.",
        fix="Amounts are in minor units, so even large limits are far shorter than this.",
    )


def too_much_input(maximum: int) -> RuleSyntaxError:
    return RuleSyntaxError(
        problem="The rule has too many parts to build.",
        fix="Split it into two rules.",
        reason=f"a built rule cannot exceed {maximum} characters once written out",
    )


def non_ascii_source(char: str) -> RuleSyntaxError:
    """Non-ASCII outside a text value, caught before parsing rather than after.

    ``ast.parse`` NFKC-normalises identifiers, so ``abs`` written with
    U+FF41 FULLWIDTH LATIN SMALL LETTER A becomes
    ``abs`` in the tree. Checking the parsed name is therefore too late: by then
    the character is gone and the validator sees a perfectly ordinary ASCII
    name, while the stored rule text still reads as something else. The only
    place the divergence is visible is the source.
    """
    return RuleSyntaxError(
        problem=f"The rule contains {char!r}, which is not a plain English character.",
        fix="Retype the rule using a-z, 0-9 and the usual symbols. Accented characters are "
        "fine inside quoted text, just not in names.",
        reason="characters from other alphabets can look identical to English ones, and the "
        "rule would then read one way and behave another",
    )


class RuleEvaluationError(ValueError):
    """A rule was valid but could not be evaluated against this transaction.

    Deliberately a different type from :class:`RuleSyntaxError`. A syntax error
    is an authoring problem, caught at publish time, with the compliance officer
    who wrote it standing right there. An evaluation error happens on the
    decision path, months later, against a transaction nobody is watching — so
    it is not a message to a person, it is an event that has to apply the
    tenant's configured fallback for that rule's severity and mark the decision
    degraded.

    Conflating the two would let a missing fact read as "the rule did not
    match", which turns a broken control into an absent one and reports nothing.
    """

    def __init__(self, reason: str, rule_id: str | None = None):
        self.reason = reason
        self.rule_id = rule_id
        super().__init__(reason)


def unknown_fact(name: str) -> RuleEvaluationError:
    return RuleEvaluationError(f"no value was supplied for {name}")


def step_budget_exceeded(budget: int) -> RuleEvaluationError:
    return RuleEvaluationError(f"evaluation exceeded {budget} steps")


def uncomparable(left: object, right: object) -> RuleEvaluationError:
    return RuleEvaluationError(f"cannot compare {type(left).__name__} with {type(right).__name__}")


def not_a_number(value: object) -> RuleEvaluationError:
    return RuleEvaluationError(f"arithmetic needs whole numbers, got {type(value).__name__}")


def result_too_large(bits: int, maximum: int) -> RuleEvaluationError:
    return RuleEvaluationError(f"arithmetic produced a {bits}-bit number, limit is {maximum}")
