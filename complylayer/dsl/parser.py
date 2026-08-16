"""Turning rule text into an AST, with the guards that must run before it.

The order here is the point, and it is the correction this project makes to its
own specification. §4.3 caps node count at 200 and evaluation steps at 1000, and
enforces both *after* ``ast.parse`` returns. But the parser is the exposed
surface: CPython parses with recursive descent, so deeply nested parentheses or
unary operators consume the C stack during parsing, long before any node ceiling
gets a chance to apply. A 200-node limit does not protect a parser that has
already been handed fifty thousand open brackets.

So: length, then nesting, then parse, then node count. Each guard is cheaper
than the one after it, which is also the order that returns fastest on hostile
input.

One further protection lives in the architecture rather than in this file. Rules
are parsed at *publish* time, in the management API, and the frozen rule set
snapshot carries the validated result. The decision endpoint never parses
anything. The most dangerous component in the product is not reachable from the
endpoint that handles untrusted volume.
"""

from __future__ import annotations

import ast

from complylayer.dsl import errors, limits


def guard_source(source: str) -> str:
    """Cheap textual checks, run before the tokenizer sees anything.

    Returns the stripped source, or raises ``RuleSyntaxError``.
    """
    if not source or not source.strip():
        raise errors.empty_rule()

    if len(source) > limits.MAX_SOURCE_CHARS:
        raise errors.too_long(len(source), limits.MAX_SOURCE_CHARS)

    _guard_nesting(source)
    _guard_unary_runs(source)
    return source.strip()


def _guard_nesting(source: str) -> None:
    """A single linear scan of bracket depth.

    Deliberately counts opens without requiring balance: unbalanced input still
    costs the tokenizer, so the guard cannot wait for a well-formed expression.
    """
    depth = 0
    for char in source:
        if char in "([{":
            depth += 1
            if depth > limits.MAX_NESTING_DEPTH:
                raise errors.too_deep(limits.MAX_NESTING_DEPTH)
        elif char in ")]}":
            depth = max(0, depth - 1)


def _guard_unary_runs(source: str) -> None:
    """Consecutive unary operators recurse in the parser exactly as brackets do."""
    run = 0
    index = 0
    length = len(source)

    while index < length:
        char = source[index]
        if char in "-+~":
            run += 1
            if run > limits.MAX_UNARY_RUN:
                raise errors.too_many_operators(limits.MAX_UNARY_RUN)
            index += 1
            continue
        if source.startswith("not ", index):
            run += 1
            if run > limits.MAX_UNARY_RUN:
                raise errors.too_many_operators(limits.MAX_UNARY_RUN)
            index += 4
            continue
        if char.isspace():
            index += 1
            continue
        run = 0
        index += 1


def parse(source: str) -> ast.Expression:
    """Parse guarded source into an expression AST.

    ``ast.parse`` in ``eval`` mode accepts exactly one expression, which is the
    first thing that makes a rule a rule: no statements, no semicolons, no
    second line where the interesting part would go.
    """
    guarded = guard_source(source)

    try:
        tree = ast.parse(guarded, mode="eval")
    except SyntaxError as exc:
        # A compliance officer must never see a raw Python traceback, and the
        # message CPython produces assumes the reader is writing Python.
        #
        # Before giving the generic message, work out *why* it failed. Source
        # that parses as a statement but not as an expression is a specific and
        # common mistake — `x = 1`, `import os`, two lines where one was wanted —
        # and it deserves the specific message rather than "not written
        # correctly". The second parse only ever runs on the failure path, and
        # the guards above have already bounded what can reach it.
        if _parses_as_statements(guarded):
            raise errors.statement_not_expression() from exc
        raise errors.malformed() from exc
    except (MemoryError, RecursionError) as exc:  # pragma: no cover - guards prevent this
        # The guards above exist so this cannot be reached. If it ever is, the
        # guards have a hole, and failing loudly is how it gets found.
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
