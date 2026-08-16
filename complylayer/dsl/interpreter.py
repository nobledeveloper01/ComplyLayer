"""Evaluating a validated rule against one transaction.

The interpreter walks the AST the validator approved. It has no imports, no
attribute access, no I/O, and resolves names only from the fact mapping it is
handed. There is no path from here to the filesystem, the network, the ORM or
the Python runtime — and there is no ``eval`` anywhere in the chain, which is
the whole argument of ADR-0001.

**Two of the guards below exist because a security review found them missing.**

The validator checks *constants*, so it can refuse ``'a' * 999999999`` at publish
time. It cannot refuse ``some_fact * other_fact``, because the types of facts are
not known until a transaction arrives. If either turns out to be a string, that
multiplication is repetition, and repetition is one evaluation step and a
gigabyte of memory. A step budget counts steps; it cannot see an allocation. So
arithmetic re-checks its operands here, at run time, where the values are real.

The same reasoning applies to comparisons. ``amount_minor > destination_country``
is a perfectly valid rule that raises ``TypeError`` the moment a real transaction
supplies a string for the second one. Unhandled, that becomes a traceback on the
decision path.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from complylayer.dsl import errors, limits

# Arithmetic results are bounded. Money in minor units needs perhaps 60 bits;
# 512 leaves enormous headroom while keeping a chain of multiplications from
# building a number whose *printing* costs more than its computation.
MAX_RESULT_BITS = 512

# What a fact is allowed to be. Anything else means the fact provider handed the
# interpreter something it was not designed to reason about, which is a bug
# worth surfacing rather than coercing.
FactValue = int | str | bool | tuple


@dataclass(frozen=True)
class EvaluationContext:
    """Everything a rule may see. Nothing else is reachable.

    ``facts`` is the entire namespace: if a name is not in here, evaluation
    fails rather than resolving to anything. ``functions`` holds the
    implementations of the allowlisted call names, bound to whatever they need —
    which for the velocity family is a Redis-backed provider supplied in phase 3.
    """

    facts: Mapping[str, FactValue]
    functions: Mapping[str, Any] = field(default_factory=dict)


class Interpreter:
    """A single evaluation. Not reusable — the step count belongs to one run."""

    def __init__(self, context: EvaluationContext, max_steps: int = limits.MAX_STEPS):
        self._context = context
        self._max_steps = max_steps
        self._steps = 0

    @property
    def steps(self) -> int:
        return self._steps

    def run(self, tree: ast.Expression) -> bool:
        """Evaluate to a plain boolean: did this rule match this transaction?"""
        return bool(self._eval(tree.body))

    def _tick(self) -> None:
        self._steps += 1
        if self._steps > self._max_steps:
            raise errors.step_budget_exceeded(self._max_steps)

    def _eval(self, node: ast.AST) -> Any:
        self._tick()

        if isinstance(node, ast.Constant):
            return node.value

        if isinstance(node, ast.Name):
            try:
                return self._context.facts[node.id]
            except KeyError:
                # Never a default. A rule referring to a fact nobody supplied is
                # a control that cannot run, and the caller has to decide what
                # that means per severity — silently reading as "no match" would
                # turn a broken control into an absent one.
                raise errors.unknown_fact(node.id) from None

        if isinstance(node, ast.BoolOp):
            return self._eval_boolop(node)

        if isinstance(node, ast.UnaryOp):
            return self._eval_unaryop(node)

        if isinstance(node, ast.Compare):
            return self._eval_compare(node)

        if isinstance(node, ast.BinOp):
            return self._eval_binop(node)

        if isinstance(node, ast.Call):
            return self._eval_call(node)

        if isinstance(node, ast.List | ast.Tuple):
            return tuple(self._eval(element) for element in node.elts)

        # Unreachable for a validated tree. Kept because "the validator would
        # have caught it" is an assumption, and this is the one place where
        # being wrong about it would mean executing something unexamined.
        raise errors.RuleEvaluationError(f"cannot evaluate {type(node).__name__}")

    def _eval_boolop(self, node: ast.BoolOp) -> bool:
        """Short-circuits, which is both correct and the cheaper path.

        `and` stopping at the first false operand means a rule whose expensive
        velocity lookup sits behind a cheap amount check pays for the lookup
        only when it matters. Rule authors can order clauses for cost, and the
        step budget rewards them for it.
        """
        if isinstance(node.op, ast.And):
            for operand in node.values:
                if not self._truthy(self._eval(operand)):
                    return False
            return True

        for operand in node.values:
            if self._truthy(self._eval(operand)):
                return True
        return False

    def _eval_unaryop(self, node: ast.UnaryOp) -> Any:
        value = self._eval(node.operand)
        if isinstance(node.op, ast.Not):
            return not self._truthy(value)
        # USub is the only other permitted unary operator.
        if not _is_number(value):
            raise errors.not_a_number(value)
        return -value

    def _eval_compare(self, node: ast.Compare) -> bool:
        """Handles chained comparisons, and refuses to compare unlike things.

        Python is happy to order two strings and refuses to order a string
        against an int. Left alone, `amount_minor > destination_country` becomes
        a TypeError traceback on the decision path the first time a real
        transaction disagrees with the rule author's assumption.
        """
        left = self._eval(node.left)

        for operator, comparator_node in zip(node.ops, node.comparators, strict=True):
            right = self._eval(comparator_node)

            if isinstance(operator, ast.In | ast.NotIn):
                if not isinstance(right, tuple | str):
                    raise errors.uncomparable(left, right)
                contains = left in right
                result = contains if isinstance(operator, ast.In) else not contains
            else:
                result = self._compare(operator, left, right)

            if not result:
                return False
            left = right

        return True

    def _compare(self, operator: ast.cmpop, left: Any, right: Any) -> bool:
        if isinstance(operator, ast.Eq):
            return left == right
        if isinstance(operator, ast.NotEq):
            return left != right

        # Ordering. Both sides must be the same kind of thing, and bool is
        # excluded from the numeric side deliberately: `True > 0` is a question
        # nobody meant to ask.
        if not (_same_orderable_kind(left, right)):
            raise errors.uncomparable(left, right)

        if isinstance(operator, ast.Lt):
            return left < right
        if isinstance(operator, ast.LtE):
            return left <= right
        if isinstance(operator, ast.Gt):
            return left > right
        return left >= right

    def _eval_binop(self, node: ast.BinOp) -> int:
        left = self._eval(node.left)
        right = self._eval(node.right)

        # The run-time half of the memory-bomb fix. The validator rejected
        # `'a' * 999999999` because it could see the constant; only here can we
        # see that a *fact* is a string, and `text * count` is repetition.
        if not _is_number(left):
            raise errors.not_a_number(left)
        if not _is_number(right):
            raise errors.not_a_number(right)

        if isinstance(node.op, ast.Add):
            result = left + right
        elif isinstance(node.op, ast.Sub):
            result = left - right
        elif isinstance(node.op, ast.Mult):
            result = self._guarded_multiply(left, right)
        elif isinstance(node.op, ast.FloorDiv):
            if right == 0:
                raise errors.RuleEvaluationError("cannot divide by zero")
            result = left // right
        else:  # Mod
            if right == 0:
                raise errors.RuleEvaluationError("cannot take a remainder by zero")
            result = left % right

        _guard_magnitude(result)
        return result

    def _guarded_multiply(self, left: int, right: int) -> int:
        """Checked before multiplying, not after.

        Two 400-bit facts multiply to 800 bits, which is cheap. The reason to
        check first is the chain: repeated multiplication doubles the bit length
        each time, so five steps of a 512-bit number is a 16,384-bit number, and
        the cost of *computing* it arrives before any check of the result could
        run.
        """
        bits = left.bit_length() + right.bit_length()
        if bits > MAX_RESULT_BITS:
            raise errors.result_too_large(bits, MAX_RESULT_BITS)
        return left * right

    def _eval_call(self, node: ast.Call) -> Any:
        # The validator guarantees a plain Name callee from the allowlist, so
        # there is no lookup here that could resolve to something else.
        name = node.func.id  # type: ignore[union-attr]
        function = self._context.functions.get(name)
        if function is None:
            raise errors.RuleEvaluationError(f"{name} is not available in this context")

        args = [self._eval(argument) for argument in node.args]
        kwargs = {
            keyword.arg: self._eval(keyword.value)
            for keyword in node.keywords
            if keyword.arg is not None
        }
        return function(*args, **kwargs)

    @staticmethod
    def _truthy(value: Any) -> bool:
        return bool(value)


def _is_number(value: Any) -> bool:
    """Whole numbers only, and a bool is not one.

    ``isinstance(True, int)`` is true in Python, so without the second clause a
    boolean fact would quietly take part in arithmetic.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _same_orderable_kind(left: Any, right: Any) -> bool:
    if _is_number(left) and _is_number(right):
        return True
    return isinstance(left, str) and isinstance(right, str)


def _guard_magnitude(value: int) -> None:
    if value.bit_length() > MAX_RESULT_BITS:
        raise errors.result_too_large(value.bit_length(), MAX_RESULT_BITS)


def evaluate(
    tree: ast.Expression,
    context: EvaluationContext,
    max_steps: int = limits.MAX_STEPS,
) -> bool:
    """Evaluate a validated rule. Raises ``RuleEvaluationError`` if it cannot."""
    return Interpreter(context, max_steps).run(tree)
