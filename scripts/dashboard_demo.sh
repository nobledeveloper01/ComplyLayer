#!/usr/bin/env bash
#
# Stand the dashboard up against a seeded throwaway database and hold it open.
#
# `make demo` proves the engine works and tears everything down. This one is for
# looking at: the rule builder and the approval diff are the parts that show
# what a compliance officer actually does, and they cannot be printed to a
# terminal. Used to capture the screenshots in the README, and useful on its own
# when working on the dashboard.
#
# Ctrl-C to stop. The database is dropped on the way out.
set -euo pipefail

DB=${DEMO_DB:-complylayer_dashboard_demo}
ADDR=${DEMO_ADDR:-127.0.0.1:8421}
TENANT=${DEMO_TENANT:-tnt_demo}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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
REDIS_PORT=${COMPLYLAYER_REDIS_PORT:-6379}
REDIS_DB=${DEMO_REDIS_DB:-10}

TMP="$(mktemp -d)"
SERVER_PID=""

cleanup() {
  # runserver --noreload is a single process, so the subshell pid is enough;
  # kill the group too in case uv wrapped it.
  [ -n "$SERVER_PID" ] && kill -TERM "-$SERVER_PID" 2>/dev/null || true
  [ -n "$SERVER_PID" ] && kill -TERM "$SERVER_PID" 2>/dev/null || true
  pkill -f "manage.py runserver --noreload $ADDR" 2>/dev/null || true
  wait 2>/dev/null || true
  dropdb --if-exists "$DB" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT

step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

pg_isready -q 2>/dev/null || fail "Postgres is not accepting connections. Run 'make up' and retry."
if command -v lsof >/dev/null 2>&1 && lsof -ti ":${ADDR##*:}" >/dev/null 2>&1; then
  fail "port ${ADDR##*:} is already in use. Set DEMO_ADDR to a free one."
fi

step "Creating a throwaway database ($DB)"
dropdb --if-exists "$DB" 2>/dev/null || true
createdb "$DB"

export COMPLYLAYER_DB_NAME="$DB"
export COMPLYLAYER_REDIS_URL="redis://$REDIS_HOST:$REDIS_PORT/$REDIS_DB"
# DEBUG on, deliberately: the management settings mark the session cookie
# `Secure` outside debug, and a browser will not send a Secure cookie over the
# plain http this serves. Signing in would silently fail at the second step.
export COMPLYLAYER_DEBUG=1
export COMPLYLAYER_WATCH_VERSIONS=0
export DJANGO_SETTINGS_MODULE=server.settings_management

(cd "$ROOT" && uv run python manage.py migrate --no-input >"$TMP/migrate.log" 2>&1) \
  || fail "migrations failed:$(printf '\n')$(tail -20 "$TMP/migrate.log")"

step "Seeding a tenant, rules, two accounts and a rule awaiting approval"
(cd "$ROOT" && uv run python manage.py complylayer_demo_seed \
  --tenant "$TENANT" --dashboard >"$TMP/seed.log" 2>&1) \
  || fail "seeding failed:$(printf '\n')$(tail -30 "$TMP/seed.log")"
sed '$d' "$TMP/seed.log"

# `runserver`, not gunicorn, and that is the whole reason DEBUG is on above.
# There is no STATIC_ROOT and nothing serves `/static/` under gunicorn, so the
# dashboard would render with no stylesheet — which is a useless screenshot and
# a confusing five minutes. runserver serves static from the finders in debug.
step "Starting the dashboard"
(cd "$ROOT" && uv run python manage.py runserver --noreload "$ADDR" \
  >"$TMP/server.log" 2>&1) &
SERVER_PID=$!
for _ in $(seq 1 80); do
  curl -sf "http://$ADDR/healthz" >/dev/null 2>&1 && break
  sleep 0.25
done
curl -sf "http://$ADDR/healthz" >/dev/null 2>&1 \
  || fail "the dashboard did not start. Log:$(printf '\n')$(tail -30 "$TMP/server.log")"

cat <<EOF

  Dashboard  http://$ADDR/dashboard/sign-in

  The TOTP code changes every 30 seconds. Generate one with:
    uv run python -c "import pyotp; print(pyotp.TOTP('JBSWY3DPEHPK3PXP').now())"

  Ctrl-C to stop. The database is dropped on the way out.

EOF

wait "$SERVER_PID"
