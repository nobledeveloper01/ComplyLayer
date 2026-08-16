"""The rule language: parse, validate, and one day evaluate.

Rules are interpreted over a validated AST and never with ``eval``. The reasoning
is in docs/adr/0001-ast-interpreter-not-eval.md, and the proof is in
tests/test_dsl_escapes.py.
"""

from __future__ import annotations

import ast

from complylayer.dsl import errors, functions, limits
from complylayer.dsl.errors import RuleSyntaxError
from complylayer.dsl.parser import parse
from complylayer.dsl.validator import ALLOWED_NODES, validate

__all__ = [
    "ALLOWED_NODES",
    "RuleSyntaxError",
    "errors",
    "functions",
    "limits",
    "parse",
    "validate",
    "validate_source",
]


def validate_source(source: str) -> ast.Expression:
    """Parse and validate rule text, raising ``RuleSyntaxError`` if it is not a legal rule.

    This is the only entry point anything outside this package should use. It is
    called at rule *publish* time, never on the decision path — see the module
    docstring in parser.py for why that separation matters.
    """
    return validate(parse(source))
