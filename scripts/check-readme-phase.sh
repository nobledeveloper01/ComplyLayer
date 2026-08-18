#!/usr/bin/env bash
#
# The README goes stale silently. This makes it fail loudly instead.
#
# Asserts that the phase declared in README.md matches the PHASE file, and that
# every phase below the current one is ticked in the README's phase table. A
# phase bump without a README update fails the build, which is exactly the
# moment the update would otherwise be skipped.
#
# Run from the repository root. Exits non-zero on any mismatch.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
README="$ROOT/README.md"
PHASE_FILE="$ROOT/PHASE"

fail() { printf 'check-readme-phase: %s\n' "$1" >&2; exit 1; }

[ -f "$PHASE_FILE" ] || fail "PHASE file not found at $PHASE_FILE"
[ -f "$README" ]     || fail "README.md not found at $README"

current="$(tr -d '[:space:]' < "$PHASE_FILE")"
case "$current" in
  ''|*[!0-9]*) fail "PHASE must contain a single integer, found '$current'" ;;
esac

# The README declares its phase in an HTML comment so it survives rendering.
declared="$(sed -n 's/^<!-- phase: \([0-9]\{1,\}\) -->$/\1/p' "$README" | head -1)"
[ -n "$declared" ] || fail "README.md is missing its '<!-- phase: N -->' marker"

if [ "$declared" != "$current" ]; then
  fail "PHASE says $current but README.md declares $declared.
  The phase advanced and the README did not. Update the status line, the phase
  table, 'What works today' and 'Try it', then set the marker to $current.
  See 'The README is a phase deliverable' in docs/ROADMAP.md."
fi

# Every phase strictly below the current one must be ticked. The current phase
# is in progress, so it is deliberately not required to be ticked yet.
missing=""
n=0
while [ "$n" -lt "$current" ]; do
  if ! grep -qE "^\| $n \| ✅ \|" "$README"; then
    missing="$missing $n"
  fi
  n=$((n + 1))
done

if [ -n "$missing" ]; then
  fail "PHASE is $current but these completed phases are not ticked in README.md:$missing
  A phase is ticked when its exit gate is green, not when it feels finished —
  but a phase we have moved past must be one or the other."
fi

# 'Try it' has to carry something. An empty section is how a README starts
# rotting, and it rots from this section first.
if [ "$current" -gt 0 ]; then
  # The heading may carry a section number ("## 4. Try it"); the requirement is
  # that the section exists and has something runnable in it.
  if ! awk '/^## ([0-9]+\. )?Try it$/{f=1;next} /^## /{f=0} f && NF {found=1} END{exit !found}' "$README"; then
    fail "README.md's 'Try it' section is empty at phase $current.
  Every phase from 0 onward must leave behind a command sequence that runs."
  fi
fi

echo "check-readme-phase: ok (phase $current)"
