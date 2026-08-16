"""The AST allowlist. The security core of the product.

``ALLOWED_NODES`` is the entire permitted grammar of the rule language.
Everything not named there raises. That direction matters more than any
individual entry: the history of Python sandbox escapes is a history of
denylists that missed one construct, and a denylist is only ever as complete as
its author's imagination on the day they wrote it.

Read ``ALLOWED_NODES`` as the specification of what a rule may be. If a
construct is absent, a rule cannot contain it, and the escape corpus in
``tests/test_dsl_escapes.py`` is the proof.
"""

from __future__ import annotations

import ast

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

# Named so the rejection can say something useful rather than "not permitted".
EXPLICITLY_REJECTED = {
    ast.Attribute,
    ast.Subscript,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.JoinedStr,
    ast.FormattedValue,
    ast.Starred,
    ast.NamedExpr,
    ast.IfExp,
    ast.Await,
    ast.Dict,
    ast.Set,
    ast.Slice,
}


class RuleValidator(ast.NodeVisitor):
    """Reject anything not explicitly permitted."""

    def generic_visit(self, node: ast.AST) -> None:
        if type(node) not in ALLOWED_NODES:
            raise self._rejection(node)
        super().generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """The single most valuable prohibition in the validator.

        Blocking the dot closes the whole ``__class__`` -> ``__bases__`` ->
        ``__subclasses__`` family, which is where nearly every published escape
        begins. It is also the rejection a compliance officer is most likely to
        meet honestly, so the message is written for them.
        """
        raise errors.attribute_access(self._describe_attribute(node))

    def visit_Subscript(self, node: ast.Subscript) -> None:
        raise errors.subscript()

    def visit_Name(self, node: ast.Name) -> None:
        """Reserved names are refused even though they could not do anything.

        A bare ``__builtins__`` is inert here: the interpreter resolves names
        only from the supplied fact context, so it would simply be an unknown
        fact. Rejecting it at authoring time anyway buys two things — a clear
        message at the moment the rule is written instead of a puzzling one
        later, and a boundary that does not depend on the interpreter's name
        resolution staying exactly as careful as it is today.
        """
        if node.id.startswith("_"):
            raise errors.reserved_name(node.id)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute):
            # `''.join(...)` and `().__class__.__subclasses__()` both land here,
            # and for both the honest complaint is the dot rather than the call.
            # Reporting the indirection instead would describe the mechanism to
            # someone who only wants to know why their rule was refused.
            raise errors.attribute_access(self._describe_attribute(node.func))
        if not isinstance(node.func, ast.Name):
            # `something(...)(...)`: the callee must be a plain name, so there is
            # no way to produce a callable at run time.
            raise errors.indirect_call()

        name = node.func.id
        if name not in ALLOWED_FUNCTIONS:
            raise errors.unknown_function(name, sorted(ALLOWED_FUNCTIONS))

        spec = SPECS[name]
        for keyword in node.keywords:
            if keyword.arg is None:
                # `f(**mapping)` — the keywords are not knowable until run time.
                raise errors.construct_not_allowed("Starred")
            if keyword.arg not in spec.keywords:
                raise errors.unknown_keyword(name, keyword.arg, sorted(spec.keywords))

        self.generic_visit(node)

    def _rejection(self, node: ast.AST) -> errors.RuleSyntaxError:
        return errors.construct_not_allowed(type(node).__name__)

    @staticmethod
    def _describe_attribute(node: ast.Attribute) -> str | None:
        """Quote back what they wrote, when it is short enough to be a help."""
        try:
            base = ast.unparse(node)
        except Exception:  # pragma: no cover - unparse is total for valid trees
            return None
        return base if len(base) <= 60 else None


def validate(tree: ast.Expression) -> ast.Expression:
    """Raise ``RuleSyntaxError`` unless every node in the tree is permitted."""
    RuleValidator().visit(tree)
    return tree
