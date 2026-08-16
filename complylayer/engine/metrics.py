"""Metrics, in Prometheus text format.

Small on purpose. A metrics client is a dependency on the decision path, and the
handful of numbers §11.2 asks for do not need one.

**Every series is labelled by worker, not by pod.** That is the whole point of
`complylayer_ruleset_version`: the failure it exists to catch is one worker
serving decisions from a rule set that was retired last week, and nothing else
reports it. Latency is fine, no errors are raised, the dashboard looks healthy,
and a fraction of traffic is being evaluated against the wrong controls. Labelled
per pod, a pod whose four workers disagree looks like a single healthy value.
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


def render() -> str:
    """Prometheus text exposition."""
    lines: list[str] = []
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
