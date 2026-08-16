# ADR-0001 — Rules are interpreted over a validated AST, never `eval`

**Status:** accepted
**Date:** 2026-08-16

## Context

ComplyLayer lets a compliance officer write a boolean expression that the decision engine evaluates
against every transaction. The expression is stored in a database row and supplied by a user.

Python offers a one-line implementation:

```python
result = eval(rule.expression, {"__builtins__": {}}, facts)
```

That line is remote code execution, sold as a feature. The empty `__builtins__` does not help: the
escape from a restricted `eval` is a well-travelled path that begins with attribute access on any
object the expression can reach and walks `__class__` → `__bases__` → `__subclasses__` until it
finds something that opens a file or starts a process. There is a large published literature of
these escapes and it grows.

The naive implementation is not merely suboptimal here. It hands anyone who can write a rule — or
anyone who can write to the rules table — a shell on the machine that decides whether money moves.

## Decision

Rules are parsed to an AST, validated against an **allowlist** of permitted node types and function
names, and evaluated by an interpreter that walks the tree with no I/O, no imports, no attribute
access, and a step budget.

The permitted grammar is the whole security boundary:

- **Allowlist, not denylist.** Every published Python sandbox escape is a denylist that missed one
  construct. `ALLOWED_NODES` states what a rule may contain; everything else raises.
- **No attribute access.** This one prohibition eliminates the entire `__class__` escape family,
  which is where nearly every published escape begins.
- **No subscripting, lambdas, comprehensions, or generator expressions.** Each closes a distinct
  route, and none is needed to express a compliance rule.
- **No division.** Not a security constraint — a reproducibility one. See ADR-0004.
- **A node ceiling and a step budget**, so a syntactically valid rule cannot become a denial of
  service.
- **Pre-parse guards on source length and nesting depth**, because `ast.parse` itself is the exposed
  surface and a node ceiling applied after parsing protects nothing.
- **The escape corpus is written before the validator**, and is a blocking CI gate. A newly published
  escape is added to the corpus, not discussed.

## Consequences

**Cost.** Rules cannot express arbitrary Python. String manipulation, list indexing, arithmetic on
fractions and user-defined helpers are all unavailable. When a compliance rule genuinely needs
something the grammar cannot express, the answer is a new entry in `ALLOWED_FUNCTIONS`, implemented
in Python, reviewed, tested and released — deliberately slower than editing a rule, because it is a
change to the security boundary rather than a change to a threshold.

**Benefit.** There is no path from a rule expression to the filesystem, the network, the ORM or the
Python runtime. A hostile rule can be wrong; it cannot be dangerous.

**Ongoing obligation.** Every change to `ALLOWED_NODES` or `ALLOWED_FUNCTIONS` requires a
corresponding extension to the escape corpus. This is item three of the Definition of Done for a
reason: the boundary is only as good as the last person who widened it.

## Alternatives considered

| Alternative | Why not |
|---|---|
| `eval` with restricted globals | Remote code execution. The restriction is not a boundary. |
| `asteval`, `simpleeval` and similar libraries | Reasonable, and closer to correct than `eval`. Rejected because the security boundary of this product should not be a transitive dependency whose CVE cadence we do not control, and because the grammar we need is far smaller than what those libraries permit. |
| A custom grammar with its own parser (Lark, PLY) | Defensible. Rejected because `ast.parse` is a battle-tested parser, using it means rules read as Python to anyone reviewing them, and writing a parser adds a component to audit without removing one. The pre-parse guards address the DoS surface it brings. |
| A JSON rule tree with no text syntax at all | Safest of all, and genuinely tempting. Rejected because §4.4's readability property is load-bearing: a compliance officer must be able to read a rule and see the regulation in it. A JSON tree is auditable but not readable, and the visual builder already produces one internally. |

The JSON alternative is worth restating, because the visual builder means the system effectively has
both: the builder emits a structured form for common patterns, and the expression editor exists for
the rules the builder cannot express. The text grammar is what makes the second case possible.
