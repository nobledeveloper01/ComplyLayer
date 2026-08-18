"""Liveness, readiness and metrics.

The distinction between the first two is the one that matters on a deploy.
`healthz` answers "is this process alive"; `readyz` answers "can it serve a
decision", and those are not the same question for the second it takes a new
worker to compile a rule set. A worker reporting ready before its cache is warm
produces a latency spike on every single deploy (§11.1) — the pod starts taking
traffic and every early request pays for the compile.
"""

from __future__ import annotations

import hmac

from django.conf import settings
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
    """Renders this worker's counters plus every worker's shared gauges.

    A scrape reaches exactly one worker. Without the shared half, the metric
    built to detect disagreement between workers could never see more than one
    of them.

    **Every series is labelled by tenant, so this is a customer list.** It is
    served on the same port as `/v1/decisions` and exempt from authentication,
    which meant anyone who could reach the API could read the identifiers of
    every fintech using ComplyLayer, along with each one's rule set version and
    how often it changes. For a compliance vendor that is commercially damaging
    on its own and tells an attacker exactly who to go after.

    A bearer token is required unless one is not configured — a single-process
    self-hoster on a laptop should not have to set one up to see their own
    numbers, and Prometheus supports `bearer_token_file` in a scrape config,
    which is the one line this costs an operator. Compared in constant time
    because the alternative leaks the token a character at a time.
    """
    expected = settings.COMPLYLAYER.get("METRICS_TOKEN") or ""
    if expected:
        presented = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(presented, expected):
            return json_response(
                {
                    "error": "unauthorized",
                    "message": (
                        "Metrics carry tenant identifiers. Send the scrape token as "
                        "`Authorization: Bearer ...` — Prometheus does this with "
                        "`bearer_token_file` in the scrape config."
                    ),
                },
                status=401,
            )

    client = getattr(request, "metrics_client", None)
    return HttpResponse(metrics.render(client), content_type="text/plain; version=0.0.4")
