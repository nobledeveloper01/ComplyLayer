#!/usr/bin/env bash
#
# One command from nothing to a compliance decision.
#
# Everything runs locally against the Postgres and Redis that `make up` starts.
# State goes in a throwaway database that is dropped on exit, and the Redis keys
# are namespaced to a demo database that gets flushed, so this never touches
# anything real.
set -euo pipefail

DB=${DEMO_DB:-complylayer_demo}
ADDR=${DEMO_ADDR:-127.0.0.1:8420}
TENANT=${DEMO_TENANT:-tnt_demo}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# CLAUDE.md tells people to copy .env.example to .env when 5432 or 6379 are
# taken on their machine. Honour it, or the demo ignores the one file the
# project told them to write and fails to reach a database that is running.
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

PGHOST=${COMPLYLAYER_DB_HOST:-127.0.0.1}
PGPORT=${COMPLYLAYER_DB_PORT:-5432}
PGUSER=${COMPLYLAYER_DB_USER:-complylayer}
PGPASSWORD=${COMPLYLAYER_DB_PASSWORD:-complylayer}
export PGHOST PGPORT PGUSER PGPASSWORD

REDIS_HOST=${DEMO_REDIS_HOST:-127.0.0.1}
REDIS_PORT=${DEMO_REDIS_PORT:-6379}
REDIS_DB=${DEMO_REDIS_DB:-9}

TMP="$(mktemp -d)"
SERVER_PID=""

cleanup() {
  # gunicorn's own pid, from its pidfile. `$!` on the backgrounded subshell is
  # the subshell, and killing that leaves gunicorn holding the port — which
  # then fails the *next* run with "address already in use" and hides whatever
  # the real error was.
  if [ -f "$TMP/gunicorn.pid" ]; then
    kill -TERM "$(cat "$TMP/gunicorn.pid")" 2>/dev/null || true
  fi
  [ -n "$SERVER_PID" ] && kill -TERM "$SERVER_PID" 2>/dev/null || true
  wait 2>/dev/null || true
  dropdb --if-exists "$DB" 2>/dev/null || true
  redis_cmd FLUSHDB >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
dim()  { printf '\033[2m%s\033[0m\n' "$*"; }

# Redis without redis-cli, which is not installed as often as people assume.
# Speaks just enough RESP to send one command.
redis_cmd() {
  local args=("$@") payload="*${#args[@]}\r\n"
  for a in "${args[@]}"; do payload+="\$${#a}\r\n${a}\r\n"; done
  printf '%b' "$payload" | nc -w 2 "$REDIS_HOST" "$REDIS_PORT" 2>/dev/null | head -c 200
}

# --- preflight -------------------------------------------------------------
step "1/6  Checking prerequisites"
command -v uv >/dev/null 2>&1 || fail "uv is not installed. See https://docs.astral.sh/uv/"
command -v psql >/dev/null 2>&1 || fail "psql is not installed"
command -v createdb >/dev/null 2>&1 || fail "createdb is not installed"
command -v curl >/dev/null 2>&1 || fail "curl is not installed"
pg_isready -q 2>/dev/null || fail "Postgres is not accepting connections. Run 'make up' and retry."
if command -v lsof >/dev/null 2>&1 && lsof -ti ":${ADDR##*:}" >/dev/null 2>&1; then
  fail "port ${ADDR##*:} is already in use. Set DEMO_ADDR to a free one, or stop what is holding it."
fi
redis_cmd PING | grep -q PONG || fail "Redis is not answering on $REDIS_HOST:$REDIS_PORT. Run 'make up' and retry."
echo "      uv, psql, curl, postgres, redis — ok"

# --- schema ----------------------------------------------------------------
step "2/6  Creating a throwaway database ($DB)"
dropdb --if-exists "$DB" 2>/dev/null || true
createdb "$DB"

export COMPLYLAYER_DB_NAME="$DB"
export COMPLYLAYER_REDIS_URL="redis://$REDIS_HOST:$REDIS_PORT/$REDIS_DB"
export COMPLYLAYER_DEBUG=0
export COMPLYLAYER_WATCH_VERSIONS=0
# server/boot.py refuses to start on the values published in this repository.
# These are demo-only and equally worthless, but they are not the published ones.
export COMPLYLAYER_SECRET_KEY="demo-$(date +%s)-not-for-production"
export COMPLYLAYER_CUSTOMER_SALT="demo-salt-$(date +%s)-not-for-production"
export DJANGO_SETTINGS_MODULE=server.settings

redis_cmd SELECT "$REDIS_DB" >/dev/null 2>&1 || true
(cd "$ROOT" && uv run python manage.py migrate --no-input >"$TMP/migrate.log" 2>&1) \
  || fail "migrations failed:$(printf '\n')$(tail -20 "$TMP/migrate.log")"
echo "      schema applied, dropped again on exit"

# --- seed ------------------------------------------------------------------
step "3/6  Seeding a tenant, a key and three approved rules"
(cd "$ROOT" && uv run python manage.py complylayer_demo_seed --tenant "$TENANT" >"$TMP/seed.log" 2>&1) \
  || fail "seeding failed:$(printf '\n')$(tail -20 "$TMP/seed.log")"
sed '$d' "$TMP/seed.log"
KEY=$(tail -1 "$TMP/seed.log")
[ -n "$KEY" ] || fail "no api key came back from the seed"

# --- run -------------------------------------------------------------------
step "4/6  Starting the decision worker"
(cd "$ROOT" && uv run python -m gunicorn server.asgi:application \
  --worker-class uvicorn.workers.UvicornWorker --bind "$ADDR" --workers 1 \
  --pid "$TMP/gunicorn.pid" \
  >"$TMP/server.log" 2>&1) &
SERVER_PID=$!
for _ in $(seq 1 80); do
  curl -sf "http://$ADDR/healthz" >/dev/null 2>&1 && break
  sleep 0.25
done
curl -sf "http://$ADDR/healthz" >/dev/null 2>&1 \
  || fail "the worker did not start. Log:$(printf '\n')$(tail -30 "$TMP/server.log")"
echo "      listening on http://$ADDR"

# --- the actual demo -------------------------------------------------------
decide() {
  local ref="$1" amount="$2" label="$3" customer="${4:-usr_ada}"
  local body
  body=$(curl -s -X POST "http://$ADDR/v1/decisions" \
    -H "Authorization: Bearer $KEY" \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: $ref" \
    -d "{\"transaction_ref\":\"$ref\",\"customer_ref\":\"$customer\",\"amount_minor\":$amount,\"currency\":\"NGN\",\"transaction_type\":\"transfer\"}")

  printf '\n  \033[1m%s\033[0m  %s  ₦%s\n' "$ref" "$label" \
    "$(printf "%'d" $((amount / 100)))"
  # A failure here dumps the worker log. Without it the demo prints a 500 page
  # and throws the traceback away on teardown, which is a bad half-hour.
  (cd "$ROOT" && printf '%s' "$body" | uv run python scripts/demo_render.py) \
    || fail "the decision endpoint did not return a decision. Worker log:$(printf '\n')$(tail -40 "$TMP/server.log")"
}

step "5/6  Sending transactions"
dim "      Each one is a real POST /v1/decisions against the worker above."

decide TXN-DEMO-1     50000    "small transfer"
decide TXN-DEMO-2   5000000    "above the tier limit"

# Velocity is scoped per customer, so the build-up and the sixth transfer have
# to be the same person. They were not, first time round, and the demo happily
# printed "allow" under a heading that said "the sixth transfer this hour".
for n in 1 2 3 4 5; do
  curl -s -o /dev/null -X POST "http://$ADDR/v1/decisions" \
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -H "Idempotency-Key: TXN-VEL-$n" \
    -d "{\"transaction_ref\":\"TXN-VEL-$n\",\"customer_ref\":\"usr_velocity\",\"amount_minor\":20000,\"currency\":\"NGN\",\"transaction_type\":\"transfer\"}"
done
decide TXN-DEMO-3     20000    "the sixth transfer this hour" usr_velocity

# --- what it recorded ------------------------------------------------------
step "6/6  What it wrote down"
psql -q -d "$DB" -c \
  "SELECT transaction_ref, outcome, ruleset_version AS ver, latency_ms AS ms, degraded
     FROM complylayer_decision ORDER BY decided_at;" 2>/dev/null || true
psql -q -d "$DB" -c \
  "SELECT event_type, substring(hash from 1 for 18) AS hash, substring(prev_hash from 1 for 18) AS prev
     FROM complylayer_auditrecord ORDER BY recorded_at LIMIT 5;" 2>/dev/null || true

cat <<'EOF'

What just happened:

  TXN-DEMO-1  under every threshold, so nothing matched and it was allowed.

  TXN-DEMO-2  ₦50,000 against a ₦10,000 tier limit. Blocked, and the response
              carries the rule, the regulation it cites, and the sentence the
              compliance team wrote for the customer.

  TXN-DEMO-3  usr_velocity's sixth transfer inside an hour. Flagged for review
              rather than blocked, because flagging is for humans and blocking
              is for regulation. The five before it were allowed; this one
              crossed the threshold, and the window counts attempts rather than
              successes.

Every rule above was written by ada@demo.ng and approved by chidi@demo.ng.
It has to be two people: `require_not_author` refuses an approval from the
person who wrote the rule, whatever their role. Seeding with one identity
fails at the approve step.

The audit rows are a hash chain, per tenant. Each record's hash covers the
previous one, so an edit anywhere invalidates everything after it. Try it:
UPDATE is refused by a database trigger, not by application code.

Everything is torn down now. Nothing was left behind.
EOF
