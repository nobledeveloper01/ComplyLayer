"""The structured form a visual rule builder produces.

The premise of this product is that a compliance officer changes a control
without an engineer. That premise is only true if the visual builder can express
the rules people actually need. If it cannot, the expression editor becomes
where the real rules live, an engineer is back in the loop, and the product is a
rules engine with a nice front end rather than a change in who holds the pen.

So the builder's grammar is settled here, in phase 1, alongside the function set
rather than after it. Both are one decision.

The structured form is JSON. It is what the builder emits, what the API accepts
from it, and what gets rendered back into an expression for validation. The
expression remains the stored, versioned, evaluated artefact — this is a way to
*write* one, not a second representation to keep in step.

    {"all": [
        {"fact": "amount_minor", "op": ">", "value": 5000000},
        {"call": "velocity_count", "args": {"window": "1h"}, "op": ">", "value": 5}
    ]}

Rendering to text rather than straight to an AST is deliberate: the officer sees
the expression the builder produced, the same one an auditor will read later, and
it goes through exactly the same validator as a hand-written rule. One code path,
one set of guarantees.
"""

from __future__ import annotations

from typing import Any

from complylayer.dsl import errors

COMPARISONS = {">", ">=", "<", "<=", "==", "!=", "in", "not in"}
GROUPS = {"all": "and", "any": "or"}


def to_expression(node: Any) -> str:
    """Render a structured rule into rule text.

    The result still goes through ``validate_source``. Nothing here is trusted
    on the grounds that a builder produced it — the builder is a client, and a
    client is something an attacker can also be.
    """
    return _render(node, top=True)


def _render(node: Any, top: bool = False) -> str:
    if not isinstance(node, dict):
        raise errors.malformed("A rule part must be an object.")

    for group, joiner in GROUPS.items():
        if group in node:
            return _render_group(node[group], joiner, top)

    if "not" in node:
        return f"not ({_render(node['not'])})"

    if "call" in node:
        return _render_comparison(_render_call(node), node)

    if "fact" in node:
        return _render_comparison(_identifier(node["fact"]), node)

    raise errors.malformed("A rule part needs one of: all, any, not, fact, call.")


def _render_group(parts: Any, joiner: str, top: bool) -> str:
    if not isinstance(parts, list) or not parts:
        raise errors.malformed("A group needs at least one condition.")

    rendered = [_render(part) for part in parts]
    if len(rendered) == 1:
        return rendered[0]

    joined = f" {joiner} ".join(rendered)
    return joined if top else f"({joined})"


def _render_call(node: dict) -> str:
    name = _identifier(node["call"])
    args = node.get("args", {})
    if not isinstance(args, dict):
        raise errors.malformed(f"The settings for {name} must be an object.")

    rendered = []
    for key, value in args.items():
        if isinstance(value, dict):
            # A nested call, as in the structuring template where the lower
            # bound is percent_of(reporting_threshold_minor, 90).
            rendered.append(f"{_identifier(key)}={_render_value(value)}")
        else:
            rendered.append(f"{_identifier(key)}={_literal(value)}")

    positional = node.get("positional", [])
    if not isinstance(positional, list):
        raise errors.malformed(f"The arguments for {name} must be a list.")
    parts = [_render_value(arg) for arg in positional] + rendered

    return f"{name}({', '.join(parts)})"


def _render_comparison(left: str, node: dict) -> str:
    if "op" not in node:
        # A bare fact or call used as a truth test, e.g. an existing boolean.
        return left

    op = node["op"]
    if op not in COMPARISONS:
        raise errors.malformed(f"{op!r} is not a comparison.")
    if "value" not in node:
        raise errors.malformed(f"A comparison with {op} needs a value.")

    return f"{left} {op} {_render_value(node['value'])}"


def _render_value(value: Any) -> str:
    if isinstance(value, dict):
        if "call" in value:
            return _render_call(value)
        if "fact" in value:
            return _identifier(value["fact"])
        raise errors.malformed("A value must be a number, text, list, fact or call.")
    return _literal(value)


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        raise errors.malformed("Use a comparison rather than true or false.")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # D6: no floats anywhere. Amounts are minor units, and a threshold that
        # needs a fraction is a threshold in the wrong unit.
        raise errors.malformed("Amounts are whole numbers of minor units (kobo, cents).")
    if isinstance(value, str):
        return _quoted(value)
    if isinstance(value, list):
        return f"[{', '.join(_literal(item) for item in value)}]"
    raise errors.malformed("A value must be a number, text or list.")


def _quoted(value: str) -> str:
    """Quote a string literal safely.

    The builder is a client, so this string is untrusted input. Anything that
    could close the quote and continue the expression is refused outright rather
    than escaped — escaping invites a subtle mistake, and no legitimate value
    here contains a quote or a backslash.
    """
    if any(char in value for char in "'\"\\\n\r") or len(value) > 100:
        raise errors.malformed("Text values cannot contain quotes, backslashes or line breaks.")
    return f"'{value}'"


def _identifier(name: Any) -> str:
    """Fact and function names must be plain identifiers.

    The last line of defence for the builder path: without it a crafted `fact`
    could inject arbitrary text into the rendered expression. The validator
    would still catch the result, but a defence that relies on the next layer
    noticing is one layer thinner than it looks.
    """
    if not isinstance(name, str) or not name.isidentifier() or name.startswith("_"):
        raise errors.malformed(f"{name!r} is not a valid name.")
    return name
