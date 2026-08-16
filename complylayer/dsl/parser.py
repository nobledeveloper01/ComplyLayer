"""Turning rule text into an AST, with the guards that must run before it.

The order here is the point, and it is the correction this project makes to its
own specification. §4.3 caps node count at 200 and evaluation steps at 1000, and
enforces both *after* ``ast.parse`` returns. But the parser is the exposed
surface: CPython parses with recursive descent, so deeply nested parentheses or
unary operators consume the C stack during parsing, long before any node ceiling
gets a chance to apply.

So: length, then structure, then parse, then node count. Each guard is cheaper
than the one after it, which is also the order that returns fastest on hostile
input.

**The textual guards are string-aware, and that is not a detail.** A first
version of this file counted brackets across the raw text, including inside
string literals. A security review broke it in one line: a string packed with
``)`` drives the depth counter down (it floored at zero), so
``(("))"or `` repeated is depth-neutral to the guard while opening two real
brackets each time. A 727-character rule reached a real AST depth of 68 against
an advertised maximum of 20, and the guard contributed nothing — what actually
stopped it was CPython's own tokenizer cap and the node ceiling, two backstops
nobody had planned to rely on. Scanning has to know what a string is.

One further protection lives in the architecture rather than in this file. Rules
are parsed at *publish* time, in the management API, and the frozen rule set
snapshot carries the validated result. The decision endpoint never parses
anything.
"""

from __future__ import annotations

import ast

from complylayer.dsl import errors, limits

_QUOTES = ("'", '"')
_OPENERS = "([{"
_CLOSERS = ")]}"
_UNARY_CHARS = "-+~"


def guard_source(source: str) -> str:
    """Cheap textual checks, run before the tokenizer sees anything.

    Returns the stripped source, or raises ``RuleSyntaxError``.
    """
    if not source or not source.strip():
        raise errors.empty_rule()

    if len(source) > limits.MAX_SOURCE_CHARS:
        raise errors.too_long(len(source), limits.MAX_SOURCE_CHARS)

    _scan(source)
    return source.strip()


def _scan(source: str) -> None:
    """One linear pass counting bracket depth and unary runs, outside strings only.

    String literals and comments are skipped wholesale, so their contents can
    neither inflate the depth (a false rejection) nor deflate it (the bypass).
    """
    depth = 0
    unary_run = 0
    index = 0
    length = len(source)

    while index < length:
        char = source[index]

        if char in _QUOTES:
            index = _skip_string(source, index)
            unary_run = 0
            continue

        if char == "#":
            # A comment runs to the end of the line. `ast.parse` accepts them in
            # an expression, so the scan has to as well.
            newline = source.find("\n", index)
            index = length if newline == -1 else newline + 1
            unary_run = 0
            continue

        if char in _OPENERS:
            depth += 1
            if depth > limits.MAX_NESTING_DEPTH:
                raise errors.too_deep(limits.MAX_NESTING_DEPTH)
            unary_run = 0
            index += 1
            continue

        if char in _CLOSERS:
            # Still floored at zero. Unbalanced text is the tokenizer's problem,
            # and now that strings are skipped, a stray closer cannot launder
            # depth the way it used to.
            depth = max(0, depth - 1)
            unary_run = 0
            index += 1
            continue

        if char in _UNARY_CHARS:
            unary_run += 1
            if unary_run > limits.MAX_UNARY_RUN:
                raise errors.too_many_operators(limits.MAX_UNARY_RUN)
            index += 1
            continue

        if _starts_word(source, index, "not"):
            # Matched on a word boundary rather than on `"not "`. The literal
            # space missed `not\t`, `not\x0b` and `not\x0c`, all of which Python
            # parses identically — the guard was one whitespace character from
            # being decorative.
            unary_run += 1
            if unary_run > limits.MAX_UNARY_RUN:
                raise errors.too_many_operators(limits.MAX_UNARY_RUN)
            index += 3
            continue

        if char.isspace():
            index += 1
            continue

        if not char.isascii():
            # Enforced here rather than on the parsed name, because `ast.parse`
            # NFKC-normalises identifiers, so `abs` written with U+FF41
            # FULLWIDTH LATIN SMALL LETTER A arrives at the
            # validator already spelled `abs`, so the tree looks innocent while
            # the stored rule text does not match what runs. Inside a quoted
            # value non-ASCII is fine — the scan has already skipped those, and
            # normalisation does not touch string contents.
            raise errors.non_ascii_source(char)

        unary_run = 0
        index += 1


def _skip_string(source: str, index: int) -> int:
    """Return the index just past the string literal starting at ``index``."""
    quote = source[index]
    triple = source.startswith(quote * 3, index)
    delimiter = quote * 3 if triple else quote
    index += len(delimiter)
    length = len(source)

    while index < length:
        if source[index] == "\\":
            # An escape consumes the next character, so `'\''` does not end here.
            index += 2
            continue
        if source.startswith(delimiter, index):
            return index + len(delimiter)
        index += 1

    # Unterminated. Hand the whole remainder to the tokenizer, which will produce
    # the syntax error; the scan's job was only to avoid miscounting.
    return length


def _starts_word(source: str, index: int, word: str) -> bool:
    if not source.startswith(word, index):
        return False
    after = index + len(word)
    if after < len(source) and (source[after].isalnum() or source[after] == "_"):
        return False
    return True


def parse(source: str) -> ast.Expression:
    """Parse guarded source into an expression AST.

    ``ast.parse`` in ``eval`` mode accepts exactly one expression, which is the
    first thing that makes a rule a rule: no statements, no semicolons, no
    second line where the interesting part would go.
    """
    guarded = guard_source(source)

    try:
        tree = ast.parse(guarded, mode="eval")
    except (SyntaxError, ValueError) as exc:
        # ValueError as well as SyntaxError: an embedded NUL raises SyntaxError
        # on 3.12 but ValueError on 3.11 and earlier, and this is cheap insurance
        # against that difference ever mattering.
        #
        # A compliance officer must never see a raw Python traceback, and the
        # message CPython produces assumes the reader is writing Python. Before
        # giving the generic message, work out *why* it failed: source that parses
        # as a statement but not as an expression is a specific and common mistake
        # and deserves the specific message.
        if _parses_as_statements(guarded):
            raise errors.statement_not_expression() from exc
        raise errors.malformed() from exc
    except RecursionError as exc:  # pragma: no cover - see the note below
        # Reachable only if MAX_SOURCE_CHARS grows. Measured on CPython 3.12: a
        # bracket-free `1-1-1...` chain parses fine at depth 5,000 and only
        # recurses past the limit around 10,000, which needs roughly 20,000
        # source characters. The 2,000-character cap is what makes this
        # unreachable — *not* the nesting guard, which an earlier version of this
        # comment credited and which cannot see that construct at all.
        raise errors.too_deep(limits.MAX_NESTING_DEPTH) from exc

    node_count = sum(1 for _ in ast.walk(tree))
    if node_count > limits.MAX_NODES:
        raise errors.too_complex(node_count, limits.MAX_NODES)

    return tree


def _parses_as_statements(source: str) -> bool:
    """Whether the source is valid Python, just not a single expression."""
    try:
        ast.parse(source, mode="exec")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return False
    return True
