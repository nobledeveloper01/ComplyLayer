"""The AST allowlist. The security core of the product.

``ALLOWED_NODES`` is the entire permitted grammar of the rule language.
Everything not named there raises. That direction matters more than any
individual entry: the history of Python sandbox escapes is a history of
denylists that missed one construct, and a denylist is only ever as complete as
its author's imagination on the day they wrote it.

A node allowlist is necessary and not sufficient. A security review swept every
node type reachable from ``ast.parse(mode='eval')`` and found no hole in the set
below — and then broke the validator anyway on *values* rather than node types:
``'a' * 999999999`` is three permitted nodes and a gigabyte of memory in a single
evaluation step. So this module checks what a node contains as well as what it
is.
"""

from __future__ import annotations

import ast
import re

from complylayer.dsl import errors
from complylayer.dsl.functions import ALLOWED_FUNCTIONS, SPECS

# The complete grammar. Additions here require a matching entry in the escape
# corpus — item three of the Definition of Done, and the reason that item exists.
ALLOWED_NODES: frozenset[type[ast.AST]] = frozenset(
    {
        ast.Expression,
        # Boolean and comparison logic: what a rule is made of.
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.UnaryOp,
        ast.Not,
        ast.USub,
        ast.Compare,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        # Arithmetic, integer only. ast.Div is deliberately absent — see D6:
        # division produces floats, and floats and a 100% reproducibility
        # requirement do not sit comfortably together in a system that must
        # explain a decision made six months ago.
        ast.BinOp,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Mod,
        ast.FloorDiv,
        # Names, values, and calls to the allowlisted functions.
        ast.Name,
        ast.Load,
        ast.Constant,
        ast.Call,
        ast.keyword,
        # A literal list, so `in_list(country, ['NG', 'GH'])` can be written
        # inline. Note that indexing into it is still refused.
        ast.List,
        ast.Tuple,
        # `country in high_risk_countries` reads better than the function call
        # for anyone who has met a spreadsheet.
        ast.In,
        ast.NotIn,
    }
)

# Removing ast.Div from the allowlist closed the door that *produces* a float and
# left open the one that *writes* one: `amount_minor > 1.5` validated happily, as
# did None, Ellipsis, complex and bytes. Constants carry whatever the parser
# produced, so the permitted types are named here too.
#
# bool is listed because a fact can legitimately be true or false. It is checked
# before int, since bool is a subclass of int.
ALLOWED_CONSTANT_TYPES = (bool, int, str)

# Identifiers are ASCII. Swap the first letter of `amount_minor` for U+0430
# CYRILLIC SMALL LETTER A and it renders identically in every dashboard and audit
# export while resolving to a different fact, which turns rule review — the
# control this product sells — into theatre. Python's own identifier rules accept
# the whole Unicode XID set and NFKC-normalise it, so the text an auditor reads
# need not even be the text that runs.
_ASCII_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")

# A digit ceiling well above any real money amount. Guards against the cost of
# converting an enormous literal, and against CPython's own 4,300-digit limit
# raising a ValueError somewhere less convenient.
MAX_NUMBER_DIGITS = 40


class RuleValidator(ast.NodeVisitor):
    """Reject anything not explicitly permitted."""

    def generic_visit(self, node: ast.AST) -> None:
        if type(node) not in ALLOWED_NODES:
            raise errors.construct_not_allowed(type(node).__name__)
        super().generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """The single most valuable prohibition in the validator.

        Blocking the dot closes the whole ``__class__`` -> ``__bases__`` ->
        ``__subclasses__`` family, which is where nearly every published escape
        begins. It is also the rejection a compliance officer is most likely to
        meet honestly, so the message is written for them.
        """
        raise errors.attribute_access(self._describe(node))

    def visit_Subscript(self, node: ast.Subscript) -> None:
        raise errors.subscript()

    def visit_Name(self, node: ast.Name) -> None:
        """Names must be plain ASCII identifiers.

        Deliberately does not call ``generic_visit``. ``ast.Name``'s only child
        is its ``ctx``, which is always ``Load`` in eval-mode source — ``Store``
        and ``Del`` need assignment or ``:=``, both rejected before reaching
        here. Stated rather than left for the next reader to re-derive.
        """
        if not _ASCII_IDENTIFIER.match(node.id):
            if node.id.isascii():
                # Leading underscore, or otherwise not a plain name.
                raise errors.reserved_name(node.id)
            raise errors.non_ascii_name(node.id)

    def visit_Constant(self, node: ast.Constant) -> None:
        value = node.value
        if not isinstance(value, ALLOWED_CONSTANT_TYPES):
            raise errors.bad_literal(value)
        if isinstance(value, int) and not isinstance(value, bool):
            digits = len(str(abs(value)))
            if digits > MAX_NUMBER_DIGITS:
                raise errors.number_too_large(digits, MAX_NUMBER_DIGITS)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        """Arithmetic is integer arithmetic, and the operands are checked as well.

        ``'a' * 999999999`` and ``[0] * 999999999`` are both permitted node
        types, both one evaluation step, and both a memory exhaustion. A step
        budget counts steps, not bytes, so the check has to live here — at
        publish time, where it is free — rather than in the interpreter, where
        it would be on the decision path.

        ``'%s' % amount_minor`` is the same shape: ``ast.Mod`` on a string is
        printf formatting rather than arithmetic.
        """
        for operand in (node.left, node.right):
            if isinstance(operand, ast.Constant) and not _is_plain_int(operand.value):
                raise errors.non_numeric_operand()
            if isinstance(operand, ast.List | ast.Tuple):
                raise errors.non_numeric_operand()

        if isinstance(node.op, ast.FloorDiv | ast.Mod):
            right = node.right
            if isinstance(right, ast.Constant) and right.value == 0:
                raise errors.division_by_zero()

        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute):
            # `''.join(...)` and `().__class__.__subclasses__()` both land here,
            # and for both the honest complaint is the dot rather than the call.
            raise errors.attribute_access(self._describe(node.func))
        if not isinstance(node.func, ast.Name):
            # `something(...)(...)`: the callee must be a plain name, so there is
            # no way to produce a callable at run time.
            raise errors.indirect_call()

        name = node.func.id
        if name not in ALLOWED_FUNCTIONS:
            raise errors.unknown_function(name, sorted(ALLOWED_FUNCTIONS))

        spec = SPECS[name]

        if len(node.args) != spec.positional:
            # The arity data was already in SPECS; the check was simply never
            # written, so `abs(1, 2, 3, 4, 5)` published cleanly and would have
            # raised TypeError at decision time — on the hot path, which is the
            # exact failure the publish/decide split exists to prevent.
            raise errors.wrong_arity(name, len(node.args), spec.positional)

        seen: set[str] = set()
        for keyword in node.keywords:
            if keyword.arg is None:
                # `f(**mapping)` — the keywords are not knowable until run time.
                raise errors.construct_not_allowed("Starred")
            if keyword.arg not in spec.keywords:
                raise errors.unknown_keyword(name, keyword.arg, sorted(spec.keywords))
            if keyword.arg in seen:
                # `velocity_count(window='1h', window='2h')` is caught by
                # CPython's *compile* stage, which this pipeline never runs.
                # Without this, which window applies is decided by iteration
                # order.
                raise errors.duplicate_keyword(name, keyword.arg)
            seen.add(keyword.arg)

        self.generic_visit(node)

    @staticmethod
    def _describe(node: ast.AST) -> str | None:
        """Quote back what they wrote, when it is short enough to be a help."""
        try:
            text = ast.unparse(node)
        except Exception:  # pragma: no cover - unparse is total for valid trees
            return None
        return text if len(text) <= 60 else None


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate(tree: ast.Expression) -> ast.Expression:
    """Raise ``RuleSyntaxError`` unless every node in the tree is permitted."""
    RuleValidator().visit(tree)

    # A rule has to be able to answer differently for different transactions.
    # `True` and `1` are valid expressions and complete nonsense as rules: one
    # blocks everything, the other is a control that is not there. Both would
    # pass every other check in this file.
    if isinstance(tree.body, ast.Constant):
        raise errors.not_a_condition()

    return tree
