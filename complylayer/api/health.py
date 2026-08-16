"""Liveness, readiness and metrics.

The distinction between the first two is the one that matters on a deploy.
`healthz` answers "is this process alive"; `readyz` answers "can it serve a
decision", and those are not the same question for the second it takes a new
worker to compile a rule set. A worker reporting ready before its cache is warm
produces a latency spike on every single deploy (§11.1) — the pod starts taking
traffic and every early request pays for the compile.
"""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse

from complylayer.api.decision import json_response
from complylayer.engine import metrics


def healthz(request: HttpRequest) -> HttpResponse:
    """Alive. Deliberately does not touch Redis, Postgres or the cache.

    A liveness probe that checks dependencies restarts the process when a
    dependency is down, which turns one outage into two.
    """
    return json_response({"status": "ok"})


def readyz(request: HttpRequest) -> HttpResponse:
    cache = getattr(request, "ruleset_cache", None)

    if cache is None or not cache.is_warm:
        return json_response(
            {
                "status": "warming",
                "detail": "the rule set is still compiling; this worker is not taking traffic yet",
            },
            status=503,
        )

    return json_response({"status": "ready", "ruleset_version": cache.version})


def metrics_view(request: HttpRequest) -> HttpResponse:
    return HttpResponse(metrics.render(), content_type="text/plain; version=0.0.4")
