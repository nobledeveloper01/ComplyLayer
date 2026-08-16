"""The escape corpus. The single most important test file in this project.

Written before the validator exists, deliberately. Every test in here failed on
the day it was committed, and the validator was built until they passed. That
ordering turns the security requirement into the specification rather than
something checked afterwards.

Each entry is a construct from the published literature on escaping restricted
Python. The comment above each says what it would reach if it ran. None of them
should ever evaluate — every one must raise ``RuleSyntaxError`` at validation,
before any interpreter sees it.

Two rules for maintaining this file:

1. A newly published escape gets added here. It is not discussed first.
2. Any change to ``ALLOWED_NODES`` or ``ALLOWED_FUNCTIONS`` requires a
   corresponding entry, because the boundary is only as good as the last person
   who widened it.

The escapes are public. Keeping them in the repository costs nothing and makes
the guarantee auditable, which is worth more than the imagined secrecy.
"""

from __future__ import annotations

import pytest

from complylayer.dsl import RuleSyntaxError, validate_source

# ---------------------------------------------------------------------------
# The attribute-access family.
#
# Nearly every published Python sandbox escape begins here: reach any object,
# walk to its type, walk to `object`, enumerate every subclass loaded in the
# process, and find one that opens a file or starts a process. Blocking
# attribute access closes the entire family in one prohibition, which is why it
# is the single most valuable rule in the validator.
# ---------------------------------------------------------------------------

ATTRIBUTE_ESCAPES = [
    # The canonical chain: from a literal to every class in the interpreter.
    "().__class__.__bases__[0].__subclasses__()",
    "''.__class__.__mro__[1].__subclasses__()",
    "[].__class__.__base__.__subclasses__()",
    "{}.__class__.__bases__[0].__subclasses__()",
    "(1).__class__.__bases__[0].__subclasses__()",
    "(1.0).__class__.__mro__[-1].__subclasses__()",
    "True.__class__.__mro__[1].__subclasses__()",
    "b''.__class__.__bases__[0].__subclasses__()",
    "set().__class__.__base__.__subclasses__()",
    # Reaching the builtins through a function's globals.
    "velocity_count.__globals__",
    "velocity_count.__globals__['__builtins__']",
    "velocity_count.__class__.__call__.__globals__",
    "velocity_count.__code__.co_consts",
    "velocity_count.__self__.__class__",
    # Reaching builtins directly.
    "__builtins__",
    "__import__('os')",
    "__builtins__.__import__('os').system('id')",
    "__loader__.load_module('os')",
    # The object protocol as a lever.
    "().__class__.__reduce__",
    "().__reduce_ex__(2)",
    "().__class__.__init__.__globals__",
    "amount_minor.__class__.__dict__",
    "amount_minor.__doc__",
    "amount_minor.__sizeof__()",
    "type(amount_minor).mro()",
    # Attribute access hidden mid-expression, where a denylist might miss it.
    "amount_minor > 5 and ''.__class__",
    "min(amount_minor, ''.__class__.__bases__[0])",
    "velocity_count(window='1h') > (1).__class__.__bases__[0].__subclasses__()",
]

# ---------------------------------------------------------------------------
# Indexing.
#
# Subscripting is what turns `__subclasses__()` from a list into a specific
# class, and what turns `__globals__` from a mapping into a callable. No
# compliance rule needs it, so it is blocked outright rather than filtered.
# ---------------------------------------------------------------------------

SUBSCRIPT_ESCAPES = [
    "high_risk_countries[0]",
    "().__class__.__bases__[0]",
    "velocity_count.__globals__['__builtins__']['eval']",
    "[c for c in ()][0]",
    "(lambda: 0).__globals__['__builtins__']",
]

# ---------------------------------------------------------------------------
# Lambdas, comprehensions and generators.
#
# Each provides a fresh scope, and a fresh scope is somewhere to build an
# object the validator never inspected. Comprehension scope leaks and generator
# frame access are both well-documented routes out of naive sandboxes.
# ---------------------------------------------------------------------------

SCOPE_ESCAPES = [
    "(lambda: ().__class__)()",
    "(lambda x: x.__class__)(amount_minor)",
    "[x for x in ().__class__.__bases__]",
    "[x.__subclasses__() for x in ().__class__.__bases__]",
    "{k: v for k, v in {}.items()}",
    "{x for x in ()}",
    "(x for x in ())",
    "(x for x in ()).gi_frame.f_globals",
    "[y := amount_minor]",
    "list(filter(lambda c: c, []))",
]

# ---------------------------------------------------------------------------
# String formatting.
#
# `"{0.__class__}".format(obj)` performs attribute access inside the format
# machinery rather than in the expression, which is exactly the sort of indirect
# route a denylist misses. f-strings do the same in the grammar itself.
# ---------------------------------------------------------------------------

FORMAT_ESCAPES = [
    "'{0.__class__}'.format(amount_minor)",
    "'{0.__class__.__bases__[0].__subclasses__}'.format(())",
    "'{.__class__}'.format('')",
    "f'{amount_minor.__class__}'",
    "f'{().__class__.__bases__}'",
    "'%s' % amount_minor.__class__",
    "'{}'.format_map({})",
]

# ---------------------------------------------------------------------------
# Direct execution and imports.
#
# The thing the whole design exists to prevent, stated plainly.
# ---------------------------------------------------------------------------

EXECUTION_ESCAPES = [
    "eval('1+1')",
    "exec('x=1')",
    "compile('1', '<s>', 'eval')",
    "open('/etc/passwd')",
    "globals()",
    "locals()",
    "vars()",
    "dir()",
    "getattr(amount_minor, '__class__')",
    "setattr(amount_minor, 'x', 1)",
    "input()",
    "breakpoint()",
    "help()",
    "exit()",
    "print(amount_minor)",
    # Unknown callables must be rejected even when they look harmless, because
    # the allowlist is the boundary, not a judgement about the name.
    "os_system('id')",
    "requests_get('http://example.com')",
]

# ---------------------------------------------------------------------------
# Statements and control flow.
#
# A rule is one boolean expression. Anything that is a statement is not a rule,
# and accepting statements would mean accepting assignment, import and function
# definition along with them.
# ---------------------------------------------------------------------------

STATEMENT_ESCAPES = [
    "import os",
    "from os import system",
    "x = 1",
    "def f(): pass",
    "class C: pass",
    "assert amount_minor",
    "del amount_minor",
    "raise ValueError",
    "return amount_minor",
    "yield amount_minor",
    "await amount_minor",
    "with open('x') as f: pass",
    "for i in range(3): pass",
    "while True: pass",
    "if amount_minor: pass",
    "try: pass\nexcept: pass",
    "lambda: 0",
    "global x",
    "nonlocal x",
    "pass",
    # Two expressions is not one expression, and the second is where the
    # interesting thing would go.
    "amount_minor > 5; ().__class__",
    "amount_minor > 5\n().__class__",
]

# ---------------------------------------------------------------------------
# Indirection.
#
# Constructs whose danger is not visible in the text of the expression itself.
# ---------------------------------------------------------------------------

INDIRECTION_ESCAPES = [
    "velocity_count(**{'window': '1h'})",
    "min(*[1, 2])",
    "[*high_risk_countries]",
    "{**{}}",
    "amount_minor if ().__class__ else 0",
    "(amount_minor).__class__ if True else 0",
    "not ().__class__",
    "-().__class__.__bases__[0].__subclasses__()[0]",
]

ALL_ESCAPES = [
    *ATTRIBUTE_ESCAPES,
    *SUBSCRIPT_ESCAPES,
    *SCOPE_ESCAPES,
    *FORMAT_ESCAPES,
    *EXECUTION_ESCAPES,
    *STATEMENT_ESCAPES,
    *INDIRECTION_ESCAPES,
]


@pytest.mark.parametrize("source", ALL_ESCAPES, ids=lambda s: s[:60])
def test_every_escape_is_rejected(source: str):
    """Rejected means *raises*. Returning false would be a rule that silently never fires."""
    with pytest.raises(RuleSyntaxError):
        validate_source(source)


def test_the_corpus_has_not_been_quietly_emptied():
    """A corpus that shrinks is how this protection would actually be lost.

    Not a magic number for its own sake — it is a tripwire on the one change
    nobody would notice in review: deleting entries to make a validator change
    pass.
    """
    assert len(ALL_ESCAPES) >= 90
    assert len(set(ALL_ESCAPES)) == len(ALL_ESCAPES), "duplicate entries inflate the count"


# The specific things a rejection may name. A message that matches none of these
# is a generic complaint, which is the failure mode this test exists to catch.
NAMED_CONSTRUCTS = ("dot", "square bracket", "underscore", "no function called", "directly by name")


@pytest.mark.parametrize("source", ATTRIBUTE_ESCAPES, ids=lambda s: s[:60])
def test_attribute_family_errors_name_a_specific_construct(source: str):
    """The largest escape family deserves the clearest messages.

    A compliance officer who writes `customer.kyc_tier` is not attacking
    anything, and the error they get should tell them what to write instead.

    Not every entry here is rejected *for* its dot — in
    `velocity_count.__globals__['x']` the outermost blocked construct is the
    square bracket, so that is what gets named. Which is correct: the message
    should describe what the reader wrote, not the deepest thing wrong with it.
    What matters is that the complaint is always concrete.
    """
    with pytest.raises(RuleSyntaxError) as exc:
        validate_source(source)
    message = str(exc.value).lower()
    assert any(construct in message for construct in NAMED_CONSTRUCTS), message


def test_a_plain_dotted_fact_gets_the_dot_message():
    """The case a real compliance officer actually hits, as opposed to an attacker."""
    with pytest.raises(RuleSyntaxError) as exc:
        validate_source("customer.kyc_tier > 2")
    assert "dot" in str(exc.value).lower()

    with pytest.raises(RuleSyntaxError) as exc:
        validate_source("''.join(high_risk_countries)")
    assert "dot" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Findings from the phase 1 security review.
#
# Two independent reviewers went at the sandbox after it was built. The node
# allowlist itself held — an exhaustive sweep of every node type reachable from
# `ast.parse(mode='eval')` found no hole. Everything below got through anyway,
# by attacking what a node *contains* rather than what it *is*, or by defeating
# a textual guard the parser ran before it.
#
# Each entry is a payload that was ACCEPTED by the validator as originally
# written, with the confirmed behaviour recorded alongside it.
# ---------------------------------------------------------------------------

REVIEW_FINDINGS = [
    # D6 removed ast.Div so a rule could not produce a float, and left open the
    # door that simply writes one. None and Ellipsis are worse than untidy:
    # comparing them raises TypeError at decision time, on the critical path,
    # for a rule that published cleanly.
    "amount_minor > 1.5",
    "amount_minor > 1e400",
    "amount_minor > .1 + .2",
    "amount_minor == 1j",
    "amount_minor == b'\\x00'",
    "amount_minor == ...",
    "amount_minor == None",
    # Permitted nodes, one evaluation step, one gigabyte. MAX_STEPS counts
    # steps and cannot see an allocation.
    "'a' * 999999999 == 'x'",
    "'a' * 99999 * 99999 * 99999 == 'x'",
    "[0] * 999999999 == []",
    "[0, 1] * 99999 * 99999 == []",
    # ast.Mod on a string is printf formatting, not arithmetic.
    "'%s' % amount_minor == 'x'",
    # U+0430 CYRILLIC SMALL LETTER A. Renders identically to `amount_minor` in
    # every dashboard, diff and audit export, and resolves to a different fact —
    # so an approver signs off a control that does not exist. Caught in the
    # source scan, because ast.parse NFKC-normalises identifiers and the tree
    # would look innocent by the time the validator saw it.
    "аmount_minor > 5000000",
    "ａbs(1) > 1",
    "\U0001d41a\U0001d41b\U0001d42c(1) > 1",
    # SPECS carried the arity all along; the check was never written. These
    # published cleanly and would have raised TypeError at decision time.
    "abs(1, 2, 3, 4, 5) > 1",
    "abs() > 1",
    "hour_of_day(1, 2, 3) > 1",
    "min() > 1",
    "in_list() > 1",
    "percent_of(amount_minor, 90, 1, 2) > 1",
    # CPython rejects a repeated keyword at compile time, which this pipeline
    # deliberately never runs. Without the check, which window applies is
    # decided by iteration order.
    "velocity_count(window='1h', window='2h') > 5",
    # Division by a literal zero, and rules that are not conditions at all.
    "amount_minor // 0 > 1",
    "amount_minor % 0 > 1",
    "True",
    "1",
    "'always'",
]

# The guard bypasses. Kept separate because these are about the textual scan
# that runs before parsing, not about the grammar.
GUARD_BYPASSES = [
    # `not\t`, `not\x0b`, `not\x0c` all parse exactly as `not `. The guard
    # matched the literal string "not " and so was one whitespace character
    # away from being decorative.
    ("not\t" * 400) + "amount_minor",
    ("not\x0b" * 400) + "amount_minor",
    ("not\x0c" * 400) + "amount_minor",
    # Paren laundering. The depth counter scanned raw text and floored at zero,
    # so a string full of `)` reset it while real brackets stayed open.
    # `(("))"or ` is depth-neutral to the guard and +2 real depth each time.
    # This reached a real AST depth of 68 against an advertised maximum of 20.
    ('(("))"or ' * 66) + "1" + ("))" * 66),
    ('(("))"or ' * 180) + "1" + ("))" * 180),
]


@pytest.mark.parametrize("source", REVIEW_FINDINGS, ids=lambda s: s[:48])
def test_security_review_findings_stay_closed(source: str):
    with pytest.raises(RuleSyntaxError):
        validate_source(source)


@pytest.mark.parametrize("source", GUARD_BYPASSES, ids=lambda s: repr(s[:32]))
def test_guard_bypasses_stay_closed(source: str):
    with pytest.raises(RuleSyntaxError):
        validate_source(source)


def test_legitimate_rules_survived_the_hardening():
    """Every fix above narrows the grammar, and narrowing can go too far.

    A validator that rejects everything passes every test in this file and
    leaves the product with no rule language.
    """
    for source in SPEC_EXAMPLES_FOR_REGRESSION:
        validate_source(source)


SPEC_EXAMPLES_FOR_REGRESSION = [
    "amount_minor > tier_daily_limit_minor",
    "velocity_count(window='1h', min_amount_minor=50_000_000) > 5",
    "days_since(last_transaction_at) > 90 and amount_minor > 20_000_000",
    "in_list(destination_country, high_risk_countries) and kyc_tier < 3",
    "percent_of(balance_minor, 90) < amount_minor",
    "amount_minor // 100 > 5",
    "not (kyc_tier == 3)",
    # Accented text inside a quoted value is fine. Only names are ASCII-only,
    # because NFKC normalisation does not touch string contents.
    "destination_country == 'Côte'",
]
