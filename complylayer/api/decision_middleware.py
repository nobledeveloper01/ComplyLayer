"""Wiring the decision path.

**This module exists because the endpoint did not work.**

Every test of `POST /v1/decisions` attached `request.decision_handler` itself, so
832 of them passed against an endpoint that raised `AttributeError` on the first
real request. Nothing in production ever attached it. The README said "it serves
decisions"; running the server and calling it said otherwise.

That is the exact failure this project keeps writing tests against — something
that looks complete and is silently absent — and it survived eight phases because
the seam every test used was the seam nobody built. The test that would have
caught it goes through the real middleware stack, and is now in
`tests/test_decision_wiring.py`.

What this assembles, per request:

- the API key resolved to exactly one tenant (§8.1's first isolation layer),
- that tenant's compiled rule set from this worker's cache (D12: per worker, not
  per pod), and
- a velocity provider bound to this customer.
"""

from __future__ import annotations

import threading
from typing import Any

from django.conf import settings
from django.http import JsonResponse

from complylayer.api.auth import AuthenticationFailed, authenticate
from complylayer.api.handler import DecisionHandler
from complylayer.api.store import DatabaseStore
from complylayer.engine import RuleSetCache, VersionWatcher, metrics
from complylayer.tenancy import tenant_scope

EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})

# One cache per tenant per *worker*. Gunicorn forks, so each child builds its own
# — which is why `complylayer_ruleset_version` is labelled by worker rather than
# by pod: four workers disagreeing inside one pod is the silent failure §11.2
# exists to catch.
_caches: dict[str, RuleSetCache] = {}
_watchers: dict[str, VersionWatcher] = {}
_lock = threading.Lock()


def _load_published(tenant_id: str):
    """Read the current published version. **From the primary, deliberately.**

    This was written against the replica first, on the reasoning that §11.1 keeps
    heavy reads off the database serving decisions. That was wrong, and wrong in
    a way worth recording: a replica lags, and this is the one read where lag
    turns into a compliance problem rather than a stale dashboard. A worker that
    loads version 46 because the replica has not caught up is serving decisions
    against a control that was retired — which is exactly the version skew §11.6
    pages on, arriving by design instead of by accident.

    §11.1 is about backtests, reports and dashboard queries: big, frequent, and
    tolerant of a second's lag. This is one indexed row per version change per
    worker. It belongs on the primary.

    **Scoped here rather than by the middleware**, because the middleware is not
    the only caller: `VersionWatcher` polls this from a background thread that
    has no request and therefore no request-level scope. A read that works on
    the request path and returns nothing on the poller would mean a worker
    silently stopped picking up new versions — the exact skew §11.6 pages on,
    and invisible because an empty result is not an error.
    """
    from complylayer.models import RuleSetVersion

    with tenant_scope(tenant_id):
        snapshot = RuleSetVersion.objects.filter(tenant_id=tenant_id).order_by("-version").first()
        if snapshot is None:
            return None
        return snapshot.version, snapshot.rules_snapshot, snapshot.lists_snapshot


def cache_for(tenant_id: str, client: Any = None) -> RuleSetCache:
    """This worker's cache for one tenant, warmed on first use.

    Warmed here rather than at import because a Redis or database connection
    created before fork is shared across children, and that fails in ways that
    take a long day to find (D12).
    """
    with _lock:
        cache = _caches.get(tenant_id)
        if cache is None:
            cache = RuleSetCache(tenant_id, lambda: _load_published(tenant_id))
            _caches[tenant_id] = cache

            # The background watcher is what keeps propagation inside the 30
            # seconds §3.4 asks for. It is a setting rather than unconditional
            # because a test wants the cache without a thread polling a database
            # it is about to roll back — and because a self-hoster running a
            # single process on a laptop has a legitimate reason to turn it off.
            if settings.COMPLYLAYER.get("WATCH_RULESET_VERSIONS", True):
                watcher = VersionWatcher(cache, client=client)
                watcher.start()
                _watchers[tenant_id] = watcher

    if not cache.is_warm:
        cache.refresh()
    return cache


def shutdown() -> None:
    """Stop the watchers. Called on SIGTERM so a drain leaves no threads behind."""
    with _lock:
        for watcher in _watchers.values():
            watcher.stop()
        _watchers.clear()
        _caches.clear()


class DecisionMiddleware:
    """Attaches everything `POST /v1/decisions` needs, or explains why it cannot."""

    def __init__(self, get_response):
        self.get_response = get_response
        self._redis = None

    @property
    def redis(self):
        # Lazily, which means after fork.
        if self._redis is None:
            import redis

            self._redis = redis.Redis.from_url(settings.COMPLYLAYER["REDIS_URL"])
        return self._redis

    def __call__(self, request):
        request.decision_handler = None
        request.ruleset_cache = None
        request.metrics_client = self.redis

        if request.path in EXEMPT_PATHS:
            # `readyz` still needs the cache, because readiness *is* the cache
            # being warm (§11.1). It must not create one on demand, though —
            # that would make every probe report ready by building the very
            # thing it was asked to check.
            with _lock:
                request.ruleset_cache = next(iter(_caches.values()), None)
            return self.get_response(request)

        try:
            credentials = authenticate(request.headers.get("Authorization", ""))
        except AuthenticationFailed as exc:
            return JsonResponse({"error": "unauthorized", "message": str(exc)}, status=401)

        cache = cache_for(credentials.tenant_id, self.redis)
        request.ruleset_cache = cache

        loaded = cache.current
        if loaded is None:
            # No published rule set. Not an error: a tenant that has not
            # activated a rule allows everything, and saying so plainly beats a
            # 500 on their first integration test.
            return JsonResponse(
                {
                    "error": "no_ruleset",
                    "message": (
                        "This tenant has no published rule set yet. Activate a rule and "
                        "every decision will be evaluated against it."
                    ),
                },
                status=409,
            )

        # Published to Redis, not just to this worker's registry: a scrape hits
        # one worker, and a gauge whose whole purpose is showing disagreement
        # between workers has to be visible from any of them.
        metrics.publish_gauge(
            self.redis,
            "complylayer_ruleset_version",
            loaded.ruleset.version,
            {"tenant": credentials.tenant_id},
        )

        request.decision_handler = DecisionHandler(
            tenant_id=credentials.tenant_id,
            ruleset=loaded.ruleset,
            store=DatabaseStore(),
            velocity_factory=lambda customer_hash: _velocity(
                self.redis, credentials.tenant_id, customer_hash
            ),
            # No fallback. This used to default to the tenant id, which is
            # stored on every row the hash protects — pseudonymisation whose key
            # travels with the data it pseudonymises. Refused at boot instead;
            # see server/settings.py and complylayer/checks.py.
            salt=settings.COMPLYLAYER["CUSTOMER_SALT"],
        )

        # **Layer three is switched on here, and was switched on nowhere.**
        # `tenant_scope` had no caller outside the test suite, so
        # `current_setting('complylayer.tenant_id')` was NULL on every real
        # request. As a superuser that goes unnoticed, because policies are
        # skipped; as `complylayer_app` it means every policy matches nothing
        # and the endpoint authenticates and then finds no rule set. Row level
        # security was not merely inert, it could not be turned on.
        #
        # Wrapping the request costs one short transaction — this path writes
        # the decision and its idempotency record at the end anyway, and the
        # measured cost is in tests/test_decision_benchmark.py.
        with tenant_scope(credentials.tenant_id):
            return self.get_response(request)


def _velocity(client, tenant_id: str, customer_hash: str):
    from complylayer.velocity import RedisVelocity

    return RedisVelocity(client, tenant_id, customer_hash)
