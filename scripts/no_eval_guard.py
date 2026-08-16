#!/usr/bin/env python
"""A permanent guard against the one shortcut that would make this product a shell.

ADR-0001 explains why rules are interpreted over a validated AST. The tempting
one-liner is ``eval(rule.expression, context)``, and the moment it would get
written is under deadline pressure, by someone who has not read the ADR. This
runs in CI so that moment fails the build.

Two notes on the implementation, both learned the hard way:

§11.8 of the specification writes the guard as::

    grep -rn "\\beval(\\|\\bexec(" complylayer/ && exit 1 || true

which can never fail — the ``|| true`` swallows the exit code it just set.

And a plain grep also fires on the word inside comments and strings. The DSL
validator will document why ``eval`` is forbidden, so a grep-based guard flags
its own rationale, and a gate that cries wolf is a gate somebody disables. This
tokenises instead: a bare ``eval(`` or ``exec(`` in real code is caught, and the
same characters in a comment, a docstring or a string literal are not.
"""

from __future__ import annotations

import io
import sys
import tokenize
from pathlib import Path

FORBIDDEN = {"eval", "exec"}

EXPLANATION = """
Rules are interpreted over a validated AST. eval() and exec() are remote code
execution in a product whose whole premise is running user-supplied expressions.

If you need a construct the DSL cannot express, add it to ALLOWED_FUNCTIONS in
complylayer/dsl/functions.py, implemented in Python, reviewed and tested, and
extend the escape corpus. That path is deliberately slower than editing a rule,
because it is a change to the security boundary.

See docs/adr/0001-ast-interpreter-not-eval.md.
"""


def scan_source(source: str, filename: str = "<string>") -> list[tuple[int, str]]:
    """Return (line number, line) for every bare eval(/exec( call in real code."""
    hits: list[tuple[int, str]] = []
    lines = source.splitlines()

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Unparseable Python is the linter's problem, not this guard's. Staying
        # quiet here avoids a confusing second error about the same file.
        return hits

    # Only NAME and OP tokens matter, and an attribute access (self.eval) or a
    # keyword argument is not the thing being guarded against.
    meaningful = [t for t in tokens if t.type in (tokenize.NAME, tokenize.OP)]

    for index, token in enumerate(meaningful):
        if token.type != tokenize.NAME or token.string not in FORBIDDEN:
            continue
        if index + 1 >= len(meaningful):
            continue
        if meaningful[index + 1].string != "(":
            continue
        # `x.eval(...)` is a method call on something else; `ast.literal_eval(...)`
        # is a different name entirely and never reaches here.
        if index > 0 and meaningful[index - 1].string == ".":
            continue
        # `def eval(...)` would be defining one, which is its own problem but not
        # this one, and flagging it here would obscure the message.
        if index > 0 and meaningful[index - 1].string in {"def", "class"}:
            continue

        line_no = token.start[0]
        text = lines[line_no - 1].strip() if line_no <= len(lines) else ""
        hits.append((line_no, text))

    return hits


def scan_path(target: Path) -> list[tuple[Path, int, str]]:
    files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
    found: list[tuple[Path, int, str]] = []
    for path in files:
        for line_no, text in scan_source(path.read_text(encoding="utf-8"), str(path)):
            found.append((path, line_no, text))
    return found


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    target = Path(argv[1]) if len(argv) > 1 else root / "complylayer"

    if not target.exists():
        print(f"no-eval-guard: nothing to scan at {target}", file=sys.stderr)
        return 1

    found = scan_path(target)
    if found:
        print("no-eval-guard: forbidden call found\n", file=sys.stderr)
        for path, line_no, text in found:
            print(f"  {path}:{line_no}: {text}", file=sys.stderr)
        print(EXPLANATION, file=sys.stderr)
        return 1

    print("no-eval-guard: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
