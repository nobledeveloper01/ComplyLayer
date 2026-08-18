"""Metrics, in Prometheus text format.

Small on purpose. A metrics client is a dependency on the decision path, and the
handful of numbers §11.2 asks for do not need one.

**Every series is labelled by worker, not by pod.** That is the whole point of
`complylayer_ruleset_version`: the failure it exists to catch is one worker
serving decisions from a rule set that was retired last week, and nothing else
reports it. Latency is fine, no errors are raised, the dashboard looks healthy,
and a fraction of traffic is being evaluated against the wrong controls. Labelled
per pod, a pod whose four workers disagree looks like a single healthy value.

**Which is why cross-worker gauges are published to Redis, not held in memory.**
Found by running gunicorn with two workers and scraping `/metrics` six times: two
scrapes returned the deciding worker's gauge and four returned nothing at all,
because a scrape reaches exactly one worker and each worker has its own registry.
The metric built to detect skew across workers could not see across workers. It
would have flickered between values scrape to scrape and read as flapping.

Counters and histograms stay in process — they aggregate correctly when Prometheus
sums across scrapes, and their per-worker sampling is a statistical detail rather
than a correctness one. Gauges whose *disagreement* is the signal go to Redis,
keyed by worker, with a TTL so a dead worker stops reporting rather than
lingering as permanent false skew.

Redis rather than `prometheus_client`'s multiprocess mode because that needs a
writable directory, and the container runs on a read-only root filesystem.
"""

from __future__ import annotations

import os
import threading
from collections import defaultdict

_lock = threading.Lock()
_counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
_gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
_histograms: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(list)

# Per §4.2, the stages the budget is itemised by. A p99 alert without a stage
# breakdown is undiagnosable at 3am, which is the entire reason this exists.
STAGES = ("auth", "facts", "eval", "serialize")

BUCKETS_MS = (1, 2, 5, 10, 20, 50, 100, 250, 500)


# Long enough that a slow scrape interval does not lose a live worker, short
# enough that a worker killed mid-deploy stops reporting quickly.
SHARED_GAUGE_TTL_SECONDS = 120
SHARED_GAUGE_KEY = "cl:metrics:gauge"


def publish_gauge(client, name: str, value: float, labels: dict[str, str] | None = None) -> None:
    """Publish a gauge every worker must be able to see.

    Best effort: a metrics write must never be the reason a decision fails. If
    Redis is unreachable the decision path is already degrading for reasons that
    matter more, and that degradation is recorded on the decision itself.
    """
    try:
        field = _render_key(_key(name, labels))
        pipe = client.pipeline()
        pipe.hset(SHARED_GAUGE_KEY, field, value)
        pipe.expire(SHARED_GAUGE_KEY, SHARED_GAUGE_TTL_SECONDS)
        pipe.execute()
    except Exception:  # nosec B110 - see the comment below
        # Deliberately silent rather than logged. This runs on the decision path
        # for every request; a Redis outage would write one line per decision at
        # 2,000 a second, burying the degraded-decision records that actually
        # matter. The outage is already visible on the decisions themselves,
        # which is where somebody will look.
        pass


def shared_gauges(client) -> dict[str, float]:
    """Every worker's published gauges, for a scrape that must see all of them."""
    try:
        raw = client.hgetall(SHARED_GAUGE_KEY) or {}
    except Exception:
        # what this worker knows rather than failing. An empty metrics response
        # would page somebody about monitoring instead of about the outage.
        return {}

    rendered: dict[str, float] = {}
    for field, value in raw.items():
        key = field.decode() if isinstance(field, bytes | bytearray) else str(field)
        text = value.decode() if isinstance(value, bytes | bytearray) else str(value)
        try:
            rendered[key] = float(text)
        except ValueError:
            continue
    return rendered


def _key(name: str, labels: dict[str, str] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
    labels = dict(labels or {})
    labels.setdefault("worker", str(os.getpid()))
    return name, tuple(sorted(labels.items()))


def increment(name: str, labels: dict[str, str] | None = None, by: float = 1.0) -> None:
    with _lock:
        _counters[_key(name, labels)] += by


def set_gauge(name: str, value: float, labels: dict[str, str] | None = None) -> None:
    with _lock:
        _gauges[_key(name, labels)] = value


def observe(name: str, milliseconds: float, labels: dict[str, str] | None = None) -> None:
    with _lock:
        _histograms[_key(name, labels)].append(milliseconds)


def reset() -> None:
    """For tests. Never called in a running process."""
    with _lock:
        _counters.clear()
        _gauges.clear()
        _histograms.clear()


def snapshot() -> dict[str, dict[str, float]]:
    """The current values, for assertions and for the readiness endpoint."""
    with _lock:
        return {
            "counters": {_render_key(key): value for key, value in _counters.items()},
            "gauges": {_render_key(key): value for key, value in _gauges.items()},
            "histograms": {_render_key(key): len(values) for key, values in _histograms.items()},
        }


def render(client=None) -> str:
    """Prometheus text exposition.

    `client` is the Redis connection. Without it this renders only what this
    worker has seen, which is correct for a single-process deployment and
    misleading for any other — see the module docstring.
    """
    lines: list[str] = []
    if client is not None:
        for field, value in sorted(shared_gauges(client).items()):
            lines.append(f"{field} {value:g}")
    with _lock:
        for key, value in sorted(_counters.items()):
            lines.append(f"{_render_key(key)} {value:g}")
        for key, value in sorted(_gauges.items()):
            lines.append(f"{_render_key(key)} {value:g}")
        for key, values in sorted(_histograms.items()):
            name, labels = key
            ordered = sorted(values)
            for bucket in BUCKETS_MS:
                count = sum(1 for value in ordered if value <= bucket)
                lines.append(
                    f"{_render_key((name + '_bucket', (*labels, ('le', str(bucket)))))} {count}"
                )
            lines.append(f"{_render_key((name + '_count', labels))} {len(ordered)}")
            lines.append(f"{_render_key((name + '_sum', labels))} {sum(ordered):g}")
    return "\n".join(lines) + "\n"


def _render_key(key: tuple[str, tuple[tuple[str, str], ...]]) -> str:
    name, labels = key
    if not labels:
        return name
    rendered = ",".join(f'{label}="{value}"' for label, value in labels)
    return f"{name}{{{rendered}}}"
