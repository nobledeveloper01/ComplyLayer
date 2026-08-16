# Security review — phase 1, the rule sandbox

Two independent reviewers went at `complylayer/dsl/` after it was built: one
hunting sandbox escapes, one hunting resource limits. Both worked from the code
and confirmed every claim by executing it rather than by reading.

**The node allowlist held.** An exhaustive sweep of every AST node type reachable
from `ast.parse(mode='eval')` found the validator accepts exactly the intended
set and nothing else. Every classic escape — `().__class__`, `''.join`,
`__import__`, `getattr`, `x()()`, walrus, comprehensions, f-strings — was
already refused.

**Fourteen findings anyway**, because a node allowlist answers what a node *is*
and says nothing about what it *contains*, or about the textual guards that run
before parsing. All fourteen are fixed, and each has an entry in
`tests/test_dsl_escapes.py` so it cannot come back.

## What was wrong

| # | Finding | Why it mattered |
|---|---|---|
| 1 | The nesting guard was bypassable in one line | `_guard_nesting` scanned raw text and floored at zero, so a string full of `)` reset the counter while real brackets stayed open. `(("))"or ` repeated is depth-neutral to the guard and +2 real depth each time. A 727-character rule reached a real AST depth of **68** against an advertised maximum of 20. The guard contributed nothing; CPython's tokenizer cap and the node ceiling were the only things stopping it, and neither was planned for. |
| 2 | Unicode homoglyphs defeated rule review | `amount_minor` with a Cyrillic first letter renders identically in every dashboard, diff and audit export, and resolves to a different fact. An approver would sign off a control that does not exist — which defeats the separation of duties the product is sold on. Fullwidth and mathematical variants were worse: `ast.parse` NFKC-normalises them, so the stored text and the evaluated name genuinely differed. |
| 3 | Sequence multiplication was a memory bomb | `'a' * 999999999` is permitted nodes, one evaluation step, and a gigabyte. `MAX_STEPS` counts steps and cannot see an allocation. This is the finding that becomes an outage rather than a 500. |
| 4 | Unbounded recursion in the builder renderer | A 1 KB JSON body — comfortably decodable by `json.loads` — reached Python's recursion limit. `RecursionError` is not a `RuleSyntaxError`, so a handler mapping rule errors to 400 returns a 500 instead. `parser.py` argues at length that guards must precede the expensive stage; `structured.py` was the same surface with no guards at all. |
| 5 | `{"op": ["&gt;"]}` raised `TypeError` from a 34-byte body | Set membership tested against attacker-supplied JSON. |
| 6 | Function arity was never checked | `abs(1, 2, 3, 4, 5)` published cleanly. The arity was already in `SPECS`; the check was simply never written. It would have raised `TypeError` at *decision* time, on the hot path — the exact failure the publish/decide split exists to prevent. |
| 7 | Duplicate keywords survived | `velocity_count(window='1h', window='2h')` is rejected by CPython's *compile* stage, which this pipeline deliberately never runs. Which window applied would have been decided by iteration order. |
| 8 | Float, complex, bytes, `None` and `Ellipsis` literals were accepted | D6 removed `ast.Div` so a rule could not *produce* a float, and left open the door that *writes* one. `None` and `Ellipsis` are worse than untidy: comparing them raises `TypeError` at decision time for a rule that published cleanly. |
| 9 | The unary-run guard missed any whitespace but a space | It matched the literal `"not "`. `not\t`, `not\x0b` and `not\x0c` parse identically in Python and all sailed past. |
| 10 | An enormous integer raised `ValueError` | CPython refuses to render an integer past 4,300 digits. Reachable by any caller passing an already-decoded dict — an SDK, a `JSONField` round-trip. |
| 11 | `EXPLICITLY_REJECTED` was dead code | It read like a security control, sat in the file whose docstring calls itself the security core, and enforced nothing. Someone would eventually have added a construct to it believing they had blocked it. |
| 12 | `// 0` and `% 0` validated | `ZeroDivisionError` at evaluation. |
| 13 | `True` and `1` were valid whole rules | One blocks everything, the other is a control that is not there. |
| 14 | Control characters passed through text values | Including U+2028, a line terminator in JavaScript, which a dashboard rendering rule text into a script context would inherit. |

## Two corrections to comments, which matter more than they look

The `MemoryError`/`RecursionError` branch in `parser.py` carried a comment saying
the nesting and unary guards made it unreachable. Measured: CPython 3.12 parses
a bracket-free `1-1-1...` chain fine at depth 5,000 and only recurses past the
limit around 10,000, needing roughly 20,000 source characters. The branch is
unreachable because of `MAX_SOURCE_CHARS = 2000` — **not** because of the guards,
which cannot see that construct at all. Anyone raising the length cap would have
been pointed at the wrong reason for it being safe.

The parser docstring claimed nothing hostile reaches `ast.parse`. Before finding
1 was fixed, nesting up to about 200 did.

## What held

Recorded because negative results are the cheap half of a security review.

- **No escape.** The allowlist is exactly what it claims to be.
- **No text injection through the builder.** Every string field was attacked with
  quote-closing, comment-injection and expression fragments. `_identifier` and
  `_quoted` genuinely close it.
- **No timing attack.** Worst case 1.29 ms on a 2,000-character hostile input.
- **No validator stack overflow.** The deepest accepted tree used ~273 frames
  against a limit of 1,000 — a margin that depends on `MAX_NODES` staying at 200.
- **`visit_Name` skipping `generic_visit` is benign.** `ast.Name`'s only child is
  its `ctx`, always `Load` in eval-mode source. Now stated in a comment so the
  next reader does not have to re-derive it.
- **Comparison and boolean chains flatten** into a single node and never recurse
  deeply. Only `BinOp` and `IfExp` build deep trees.

## The lesson worth keeping

The allowlist was right and the product was still exploitable, because a node
type is only half of what an expression is. The other half is what it contains:
which literal types, how large a number, how deep a structure, which alphabet a
name is written in. Every one of the fourteen findings lives in that second half.
