"""Every bound the rule language enforces, in one place.

Collected here rather than scattered through the parser because these numbers
are the security argument. A reviewer should be able to read the whole budget on
one screen, and a test asserts each one is actually applied.
"""

from __future__ import annotations

# Long enough for the most involved rule in §4.4 several times over, short
# enough that the tokenizer is never handed anything interesting.
MAX_SOURCE_CHARS = 2000

# Bracket depth. Real rules nest two or three deep; the guard exists for the
# ten-thousand-deep case that reaches CPython's C stack.
MAX_NESTING_DEPTH = 20

# Consecutive unary operators (`not not not`, `---`). Recurses in the parser the
# same way brackets do, and has no legitimate use past one or two.
MAX_UNARY_RUN = 5

# Node ceiling, applied after parsing. §4.3 of the specification.
MAX_NODES = 200

# Evaluation step budget, applied at run time by the interpreter in phase 2.
MAX_STEPS = 1000
