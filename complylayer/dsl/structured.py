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

import re
from typing import Any

from complylayer.dsl import errors, limits

COMPARISONS = {">", ">=", "<", "<=", "==", "!=", "in", "not in"}
GROUPS = {"all": "and", "any": "or"}

# parser.py argues that guards must run before the expensive stage. This module
# is the same exposed surface and had none: the renderer recursed with no depth
# counter, so a 1 KB JSON body — well inside what `json.loads` decodes happily —
# reached Python's recursion limit and raised RecursionError. That is not a
# RuleSyntaxError, so a handler mapping rule errors to 400 would have returned a
# 500 instead, on a publicly reachable publish endpoint.
MAX_STRUCTURED_DEPTH = limits.MAX_NESTING_DEPTH

# Names are plain ASCII here for the same reason as in the validator: a Cyrillic
# lookalike renders identically to an approver and resolves to a different fact.
_ASCII_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")

# Text values are restricted to printable ASCII. Quotes and backslashes could
# close the literal; U+2028 and U+2029 are line terminators in JavaScript, so a
# dashboard rendering rule text into a script context would inherit the problem.
_SAFE_TEXT = re.compile(r"[ -~]*\Z")


def to_expression(node: Any) -> str:
    """Render a structured rule into rule text.

    The result still goes through ``validate_source``. Nothing here is trusted
    on the grounds that a builder produced it — the builder is a client, and a
    client is something an attacker can also be.
    """
    rendered = _render(node, top=True)
    if len(rendered) > limits.MAX_SOURCE_CHARS:
        # Checked here as well as in the parser: a 980 KB body rendered a 420 KB
        # expression before the source-length guard ever saw it.
        raise errors.too_much_input(limits.MAX_SOURCE_CHARS)
    return rendered


def _render(node: Any, top: bool = False, depth: int = 0) -> str:
    if depth > MAX_STRUCTURED_DEPTH:
        raise errors.too_deep(MAX_STRUCTURED_DEPTH)

    if not isinstance(node, dict):
        raise errors.malformed("A rule part must be an object.")

    for group, joiner in GROUPS.items():
        if group in node:
            return _render_group(node[group], joiner, top, depth)

    if "not" in node:
        return f"not ({_render(node['not'], depth=depth + 1)})"

    if "call" in node:
        return _render_comparison(_render_call(node, depth), node, depth)

    if "fact" in node:
        return _render_comparison(_identifier(node["fact"]), node, depth)

    raise errors.malformed("A rule part needs one of: all, any, not, fact, call.")


def _render_group(parts: Any, joiner: str, top: bool, depth: int = 0) -> str:
    if not isinstance(parts, list) or not parts:
        raise errors.malformed("A group needs at least one condition.")

    rendered = [_render(part, depth=depth + 1) for part in parts]
    if len(rendered) == 1:
        return rendered[0]

    joined = f" {joiner} ".join(rendered)
    return joined if top else f"({joined})"


def _render_call(node: dict, depth: int = 0) -> str:
    if depth > MAX_STRUCTURED_DEPTH:
        raise errors.too_deep(MAX_STRUCTURED_DEPTH)

    name = _identifier(node["call"])
    args = node.get("args", {})
    if not isinstance(args, dict):
        raise errors.malformed(f"The settings for {name} must be an object.")

    rendered = []
    for key, value in args.items():
        if isinstance(value, dict):
            # A nested call, as in the structuring template where the lower
            # bound is percent_of(reporting_threshold_minor, 90).
            rendered.append(f"{_identifier(key)}={_render_value(value, depth + 1)}")
        else:
            rendered.append(f"{_identifier(key)}={_literal(value)}")

    positional = node.get("positional", [])
    if not isinstance(positional, list):
        raise errors.malformed(f"The arguments for {name} must be a list.")
    parts = [_render_value(arg, depth + 1) for arg in positional] + rendered

    return f"{name}({', '.join(parts)})"


def _render_comparison(left: str, node: dict, depth: int = 0) -> str:
    if "op" not in node:
        # A bare fact or call used as a truth test, e.g. an existing boolean.
        return left

    op = node["op"]
    # isinstance before membership: `{"op": ["&gt;"]}` made this raise an unhandled
    # TypeError ("unhashable type: 'list'") from a 34-byte body.
    if not isinstance(op, str) or op not in COMPARISONS:
        raise errors.malformed("That is not a comparison.")
    if "value" not in node:
        raise errors.malformed(f"A comparison with {op} needs a value.")

    return f"{left} {op} {_render_value(node['value'], depth + 1)}"


def _render_value(value: Any, depth: int = 0) -> str:
    if depth > MAX_STRUCTURED_DEPTH:
        raise errors.too_deep(MAX_STRUCTURED_DEPTH)

    if isinstance(value, dict):
        if "call" in value:
            return _render_call(value, depth)
        if "fact" in value:
            return _identifier(value["fact"])
        raise errors.malformed("A value must be a number, text, list, fact or call.")
    return _literal(value, depth)


def _literal(value: Any, depth: int = 0) -> str:
    if depth > MAX_STRUCTURED_DEPTH:
        raise errors.too_deep(MAX_STRUCTURED_DEPTH)

    if isinstance(value, bool):
        raise errors.malformed("Use a comparison rather than true or false.")
    if isinstance(value, int):
        # Bounded before stringifying: CPython refuses to render an integer past
        # 4,300 digits and raises ValueError, which is not a RuleSyntaxError.
        # json.loads happens to reject such a body first, but a caller passing an
        # already-decoded dict (an SDK, a JSONField round-trip) reaches this.
        if value.bit_length() > 512:
            # Digits estimated from the bit length rather than measured with
            # str(): stringifying is the very operation whose 4,300-digit ceiling
            # this guard exists to stay under, so measuring that way would raise
            # the ValueError it is meant to prevent. log10(2) is about 0.301.
            raise errors.number_too_large(value.bit_length() * 3 // 10 + 1, 40)
        return str(value)
    if isinstance(value, float):
        # D6: no floats anywhere. Amounts are minor units, and a threshold that
        # needs a fraction is a threshold in the wrong unit.
        raise errors.malformed("Amounts are whole numbers of minor units (kobo, cents).")
    if isinstance(value, str):
        return _quoted(value)
    if isinstance(value, list):
        return f"[{', '.join(_literal(item, depth + 1) for item in value)}]"
    raise errors.malformed("A value must be a number, text or list.")


def _quoted(value: str) -> str:
    """Quote a string literal safely.

    The builder is a client, so this string is untrusted input. Anything that
    could close the quote and continue the expression is refused outright rather
    than escaped — escaping invites a subtle mistake, and no legitimate value
    here contains a quote or a backslash.
    """
    if len(value) > 100 or not _SAFE_TEXT.match(value) or "'" in value or "\\" in value:
        raise errors.malformed(
            "Text values use plain letters, digits and spaces, without quotes or backslashes."
        )
    return f"'{value}'"


def _identifier(name: Any) -> str:
    """Fact and function names must be plain identifiers.

    The last line of defence for the builder path: without it a crafted `fact`
    could inject arbitrary text into the rendered expression. The validator
    would still catch the result, but a defence that relies on the next layer
    noticing is one layer thinner than it looks.
    """
    if not isinstance(name, str):
        raise errors.malformed("A name must be text.")
    if not _ASCII_IDENTIFIER.match(name):
        # `str.isidentifier()` accepts the whole Unicode XID set, so it let
        # through Cyrillic and mathematical-alphanumeric lookalikes. It also runs
        # before Python's NFKC normalisation, so it and the validator disagreed
        # about what a name even is.
        raise errors.non_ascii_name(name) if not name.isascii() else errors.reserved_name(name)
    return name
