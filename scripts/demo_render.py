"""Render one decision response for a human reading a terminal.

Separate from `demo.sh` because formatting JSON in bash means jq, and jq is one
more thing a reviewer has to install before they can see the product work. The
whole point of the demo is that it runs on what is already there.

Reads the response body on stdin, prints the outcome, the rules that matched and
the regulation each cites. Exits non-zero on anything it cannot parse, so the
demo fails loudly rather than printing a blank and continuing.
"""

from __future__ import annotations

import json
import sys

COLOURS = {"allow": "\033[32m", "flag": "\033[33m", "block": "\033[31m"}
RESET = "\033[0m"
DIM = "\033[2m"


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print("      (empty response)", file=sys.stderr)
        return 1

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        print(f"      (not JSON) {raw[:200]}", file=sys.stderr)
        return 1

    if "outcome" not in body:
        # An error response is still a legitimate thing to show — a 401 or a 409
        # here is far more useful printed than swallowed.
        print(f"      {body.get('error', 'unknown')}: {body.get('message', raw[:160])}")
        return 1

    outcome = body["outcome"]
    colour = COLOURS.get(outcome, "")
    matched = body.get("matched_rules", [])

    print(
        f"    → {colour}{outcome}{RESET}"
        f"   ({body.get('evaluated_rules', 0)} rules evaluated, "
        f"{len(matched)} matched, {body.get('latency_ms', 0)} ms)"
    )

    for rule in matched:
        reference = rule.get("regulatory_reference") or "no reference recorded"
        print(f'      {rule["id"]}  "{rule["name"]}"   {DIM}{reference}{RESET}')

    if body.get("customer_message"):
        print(f'      {DIM}customer:{RESET} "{body["customer_message"]}"')

    if body.get("degraded"):
        print(f"      {DIM}degraded — a fact source was unavailable{RESET}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
