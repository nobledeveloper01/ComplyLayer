"""Standalone Django project wrapping the ComplyLayer app.

Phase 0 keeps this deliberately small. The split into separate decision and
management settings modules (D7 in docs/plan-architecture.md) arrives with the
decision endpoint in phase 2 — a decision worker will not load the management
URLconf at all, so a rule-management endpoint is not merely forbidden there, it
does not exist.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# No usable default: a deployment that forgets to set this should fail loudly at
# start rather than run on a key baked into a public repository.
SECRET_KEY = _env("COMPLYLAYER_SECRET_KEY", "insecure-development-key-do-not-deploy")
DEBUG = _env("COMPLYLAYER_DEBUG", "0") == "1"
ALLOWED_HOSTS = [
    h for h in _env("COMPLYLAYER_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h
]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    # Sessions, messages and staticfiles are listed here rather than only in the
    # management settings because migrations run against one database: the
    # session table has to exist whichever workload created the schema. The
    # *middleware* stays management-only, which is the part D7 is about — a
    # decision worker never processes a session, it just shares a database with
    # something that does.
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "complylayer",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    # Resolves the API key, warms this worker's rule cache and attaches the
    # handler. Without it the decision endpoint has nothing to decide with —
    # which is exactly how it shipped un-wired through eight phases of green
    # tests. See complylayer/api/decision_middleware.py.
    "complylayer.api.decision_middleware.DecisionMiddleware",
]

STATIC_URL = "static/"

ROOT_URLCONF = "server.urls"
WSGI_APPLICATION = "server.wsgi.application"
ASGI_APPLICATION = "server.asgi.application"

_DEFAULT_DB = {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": _env("COMPLYLAYER_DB_NAME", "complylayer"),
    "USER": _env("COMPLYLAYER_DB_USER", "complylayer"),
    "PASSWORD": _env("COMPLYLAYER_DB_PASSWORD", "complylayer"),
    "HOST": _env("COMPLYLAYER_DB_HOST", "127.0.0.1"),
    "PORT": _env("COMPLYLAYER_DB_PORT", "5432"),
    # Transaction-mode pooling is what makes `SET LOCAL` safe for row level
    # security (D4).
    "CONN_MAX_AGE": 0,
}

# The replica exists as its own alias even when it points at the same server, so
# that "this query runs on the replica" is a property of the code rather than of
# the deployment. A backtest that reads `default` in development is a backtest
# that will read the primary in production, and nothing would have said so.
_REPLICA_DB = {
    **_DEFAULT_DB,
    "HOST": _env("COMPLYLAYER_REPLICA_HOST", _DEFAULT_DB["HOST"]),
    "PORT": _env("COMPLYLAYER_REPLICA_PORT", _DEFAULT_DB["PORT"]),
    # pytest-django clones `default` for tests; mirroring keeps the replica
    # alias pointing at that same test database rather than a stale one.
    "TEST": {"MIRROR": "default"},
}

DATABASES = {"default": _DEFAULT_DB, "replica": _REPLICA_DB}

DATABASE_ROUTERS = ["complylayer.db_router.ReadReplicaRouter"]

# Per §6.2. Every value here is a contract with the deployment, so each is
# explicit rather than inferred.
COMPLYLAYER = {
    "REDIS_URL": _env("COMPLYLAYER_REDIS_URL", "redis://127.0.0.1:6379/2"),
    # No default fallback anywhere in this product. §10.3 sets the per-severity
    # defaults; a tenant may override, but never implicitly.
    "DEFAULT_FALLBACK": {"block": "closed", "flag": "open"},
    "MAX_EVAL_STEPS": 1000,
    "MAX_RULE_NODES": 200,
    "MAX_RULE_SOURCE_CHARS": 2000,
    "DECISION_TIMEOUT_MS": 150,
    # The background thread that keeps each worker's rule cache current. On by
    # default: without it, propagation waits for a worker restart.
    "WATCH_RULESET_VERSIONS": _env("COMPLYLAYER_WATCH_VERSIONS", "1") == "1",
}

USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": _env("COMPLYLAYER_LOG_LEVEL", "INFO")},
}
