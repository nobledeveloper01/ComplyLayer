"""``POST /v1/decisions`` — the endpoint the whole product is built around.

A plain Django view rather than a DRF one (D1). The latency budget allows 3 ms
for auth and request validation and 2 ms for serialisation, and a DRF request
cycle — Request wrapping, content negotiation, a Serializer validating field by
field, a renderer — routinely costs more than that before any compliance logic
runs. The budget was written as though the framework were free.

The input schema is small, closed and versioned, which is what makes hand-written
validation reasonable here rather than merely faster. It also lets unknown fields
be *rejected* rather than ignored, which §8.4 requires: a payload carrying a
field ComplyLayer does not know about is a payload that may be carrying data
ComplyLayer must never store.

This decision is provisional and gets measured. `tests/test_decision_benchmark.py`
runs the same endpoint both ways; if DRF lands inside the budget with headroom,
this file goes away and ADR-0002 records why.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import orjson
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from complylayer.api import validation
from complylayer.api.errors import ApiError
from complylayer.api.handler import public_body

# Rounded to 5 ms in the response (D8). Reporting the exact figure hands an
# attacker the most convenient channel for inferring how many rules matched, and
# costs nothing to remove. Padding the *actual* response was rejected: a floor
# high enough to hide anything would breach the p50 target, and §10's request for
# it was written without being read against §3.4.
LATENCY_QUANTUM_MS = 5


def json_response(payload: dict[str, Any], status: int = 200) -> HttpResponse:
    return HttpResponse(
        orjson.dumps(payload),
        status=status,
        content_type="application/json",
    )


# Correct here and only here. This endpoint authenticates with a Bearer key
# and reads no cookie, so a browser cannot be tricked into making an
# authenticated request on somebody's behalf — there is no ambient authority
# to ride. The dashboard's forms are session-authenticated and keep CSRF.
@csrf_exempt  # nosemgrep: python.django.security.audit.csrf-exempt.no-csrf-exempt
def decisions(request: HttpRequest) -> HttpResponse:
    started = time.perf_counter()

    if request.method != "POST":
        return json_response({"error": "method_not_allowed"}, status=405)

    try:
        payload = validation.parse_body(request.body)
        idempotency_key = validation.require_idempotency_key(request.headers)
        transaction = validation.parse_transaction(payload)
    except ApiError as error:
        return json_response(error.as_dict(), status=error.status)

    handler = request.decision_handler  # type: ignore[attr-defined]

    replay = handler.replay(idempotency_key)
    if replay is not None:
        # Returned verbatim, including the original timestamp and latency. A
        # retry that reported today's time would be a different decision wearing
        # the same id (A4).
        return json_response(replay)

    result = handler.decide(transaction, idempotency_key)

    elapsed_ms = (time.perf_counter() - started) * 1000
    result["latency_ms"] = _quantise(elapsed_ms)
    handler.record(result, transaction, idempotency_key)

    return json_response(public_body(result))


def _quantise(milliseconds: float) -> int:
    return int(round(milliseconds / LATENCY_QUANTUM_MS) * LATENCY_QUANTUM_MS)


def now() -> datetime:
    return datetime.now(UTC)
