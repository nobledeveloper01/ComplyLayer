#!/usr/bin/env bash
#
# Zero to a working install, counted.
#
# §6.1 promises a first decision inside ten minutes. A promise with nothing
# enforcing it drifts, one convenient extra step at a time, and nobody notices
# until a customer does. So this script IS the promise: it runs every step a new
# user runs, in order, and fails if the count exceeds the budget.
#
# When a phase needs a new step, the budget below is raised deliberately, in a
# diff someone reviews. That is the whole mechanism — not preventing growth, but
# making it visible.
#
#   ./scripts/hello-world.sh          run it
#   ./scripts/hello-world.sh --count  print the step count and exit

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# A local .env, if there is one. Not a step of its own — copying .env.example is
# part of "install", and a machine that already has a Postgres on 5432 needs to
# say so somewhere.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# Raise this only alongside the step it pays for.
STEP_BUDGET=4

PHASE="$(tr -d '[:space:]' < PHASE)"
STEP=0

step() {
  STEP=$((STEP + 1))
  printf '\n\033[36m[step %d] %s\033[0m\n' "$STEP" "$1"
}

if [ "${1:-}" = "--count" ]; then
  echo "$STEP_BUDGET"
  exit 0
fi

echo "ComplyLayer — zero to a working install (phase $PHASE)"

step "Install Python 3.12 and every dependency"
uv sync --frozen

step "Start Postgres and Redis"
docker compose up -d --wait

step "Create the schema"
uv run python manage.py migrate --no-input

step "Preflight the failure modes that are silent"
uv run python manage.py complylayer_doctor

if [ "$STEP" -gt "$STEP_BUDGET" ]; then
  cat >&2 <<MSG

hello-world: $STEP steps, budget is $STEP_BUDGET.

Getting started got longer. That is allowed, but not quietly — raise
STEP_BUDGET in this script in the same commit as the step that needs it,
so the cost shows up in a diff somebody reviews.
MSG
  exit 1
fi

printf '\n\033[32mHealthy install in %d steps (budget %d).\033[0m\n' "$STEP" "$STEP_BUDGET"

if [ "$PHASE" -lt 2 ]; then
  cat <<'MSG'

There is no decision to serve yet — the endpoint arrives in phase 2 and the
rules that feed it in phase 1. What this proves today is that the install is
sound and this host can actually meet the contracts: Postgres 16, Redis close
enough for the latency budget, and clocks that agree.

That last one matters more than it looks. Velocity windows are trimmed by
timestamp, so a drifting clock evaluates the wrong window and never errors.
MSG
fi
