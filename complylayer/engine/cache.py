"""The in-process rule cache: the reason a decision costs no database round trip.

Each worker holds the compiled rule set in memory, keyed by version. A database
lookup for rules would consume the entire latency budget by itself, so the cache
is not an optimisation — it is what makes the budget possible at all.

Three things here are less obvious than they look.

**A worker, not a pod (D12).** Gunicorn and uvicorn run multiple worker
*processes*, each with its own memory, its own cache and its own subscription.
`complylayer_ruleset_version` labelled per pod would hide skew *inside* a pod,
which is the same silent failure §11.2 built the metric to catch. The cache is
built after fork for the same reason: a Redis connection created before fork is
shared across children, and that fails in ways that take a long day to diagnose.

**The snapshot is re-validated on load (D5).** A frozen rule set is data in a
database, and a database is something an attacker who has got that far can edit.
Re-running the allowlist costs milliseconds once per version and closes the path
where a tampered snapshot smuggles an expression the validator never saw.

**Pub/sub is not a guarantee.** §11.6's runbook exists because a dropped
subscription that never reconnected is the usual cause of version skew, and
nothing errors when it happens: latency is fine, the dashboard looks healthy, and
a fraction of traffic is being evaluated against rules that were retired last
week. So there is a poll as well, and the poll is what the correctness argument
rests on — the pub/sub is only there to make propagation fast.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from complylayer.dsl import RuleSyntaxError, validate_source
from complylayer.engine.evaluation import CompiledRule, RuleSet, Severity, State

logger = logging.getLogger(__name__)

VERSION_CHANNEL = "complylayer:ruleset"

# §3.4 requires activation to propagate within 30 seconds. The poll is set below
# that so the backstop alone satisfies the requirement even if every pub/sub
# message is lost.
POLL_INTERVAL_SECONDS = 10.0


class SnapshotError(ValueError):
    """A stored rule set could not be compiled. The worker keeps serving the
    version it already has rather than swapping to something it cannot trust."""


@dataclass(frozen=True)
class LoadedRuleSet:
    ruleset: RuleSet
    lists: dict[str, tuple]
    loaded_at: float


def compile_snapshot(version: int, rules_snapshot: list[dict[str, Any]]) -> RuleSet:
    """Turn a stored snapshot into an evaluable rule set, re-validating as it goes.

    Every expression goes back through `validate_source`, the same function the
    management API used at publish time. Trusting the snapshot because it was
    validated once would make the guarantee depend on the database never being
    wrong, which is a larger assumption than re-parsing costs to avoid.
    """
    compiled = []
    for entry in rules_snapshot:
        try:
            tree = validate_source(entry["expression"])
        except RuleSyntaxError as exc:
            raise SnapshotError(
                f"rule {entry.get('id', '?')} in version {version} failed re-validation: {exc}"
            ) from exc
        except KeyError as exc:
            raise SnapshotError(f"rule in version {version} is missing {exc}") from exc

        compiled.append(
            CompiledRule(
                id=entry["id"],
                name=entry.get("name", entry["id"]),
                tree=tree,
                severity=Severity(entry["severity"]),
                state=State(entry.get("state", "active")),
                priority=entry.get("priority", 0),
                regulatory_reference=entry.get("regulatory_reference", ""),
                customer_message=entry.get("customer_message", ""),
            )
        )
    return RuleSet(version=version, rules=tuple(compiled))


class RuleSetCache:
    """One tenant's compiled rule set, held in this worker's memory.

    ``load`` is a callable returning ``(version, rules_snapshot, lists_snapshot)``
    or ``None`` — injected so the cache can be tested without a database and so
    the read can go to a replica.
    """

    def __init__(self, tenant_id: str, load: Callable[[], tuple | None]):
        self.tenant_id = tenant_id
        self._load = load
        self._current: LoadedRuleSet | None = None
        self._lock = threading.Lock()

    @property
    def current(self) -> LoadedRuleSet | None:
        """The active rule set.

        Read without a lock on purpose. The swap rebinds this attribute to a new
        immutable object, so a reader either sees the whole old version or the
        whole new one — never a half-built rule set. That is safe in CPython and
        stated here because the next person will reach for mutating in place.
        """
        return self._current

    @property
    def version(self) -> int | None:
        loaded = self._current
        return loaded.ruleset.version if loaded else None

    @property
    def is_warm(self) -> bool:
        """Whether this worker can serve a decision yet.

        A worker answering before its cache is warm produces a latency spike on
        every single deploy, so readiness depends on this rather than on the
        process merely having started (§11.1).
        """
        return self._current is not None

    def refresh(self, force: bool = False) -> bool:
        """Load the published version if it differs from the one in memory.

        Returns True if a swap happened. The lock covers the compile, so two
        threads noticing a new version at once do the work once.
        """
        with self._lock:
            published = self._load()
            if published is None:
                return False

            version, rules_snapshot, lists_snapshot = published
            if not force and self.version == version:
                return False

            ruleset = compile_snapshot(version, rules_snapshot)
            lists = {name: tuple(values) for name, values in (lists_snapshot or {}).items()}

            # The atomic swap: one rebinding of one attribute.
            self._current = LoadedRuleSet(ruleset=ruleset, lists=lists, loaded_at=time.time())

            logger.info(
                "ruleset loaded",
                extra={"tenant": self.tenant_id, "version": version, "worker": os.getpid()},
            )
            return True


class VersionWatcher:
    """Keeps a cache current: a subscription for speed, a poll for correctness.

    The poll is not a fallback in the sense of "if the good thing breaks". It is
    the mechanism the 30-second propagation requirement actually rests on, and
    pub/sub is a latency optimisation on top. Stating it that way round matters,
    because a system whose correctness depends on a subscription staying alive is
    a system that fails silently the first time one does not.
    """

    def __init__(
        self,
        cache: RuleSetCache,
        client: Any = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ):
        self.cache = cache
        self.client = client
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.polls = 0
        self.announcements = 0

    def start(self) -> None:
        """Begin watching. Call this *after* fork, never before (D12)."""
        self._stop.clear()
        self._threads = [threading.Thread(target=self._poll_loop, daemon=True, name="cl-poll")]
        if self.client is not None:
            self._threads.append(
                threading.Thread(target=self._subscribe_loop, daemon=True, name="cl-subscribe")
            )
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads = []

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.poll_interval)

    def poll_once(self) -> bool:
        self.polls += 1
        try:
            return self.cache.refresh()
        except SnapshotError:
            # Keep serving the version already in memory. A rule set that will
            # not compile is a management-side problem, and refusing to decide
            # would turn it into an outage.
            logger.exception("ruleset refresh failed", extra={"tenant": self.cache.tenant_id})
            return False

    def _subscribe_loop(self) -> None:  # pragma: no cover - exercised in integration
        while not self._stop.is_set():
            try:
                pubsub = self.client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(VERSION_CHANNEL)
                for message in pubsub.listen():
                    if self._stop.is_set():
                        break
                    if message.get("type") == "message":
                        self.on_announcement()
            except Exception:
                # A dropped subscription must not take the worker down, and the
                # poll carries propagation while this reconnects.
                logger.warning("ruleset subscription dropped, reconnecting", exc_info=True)
                self._stop.wait(1.0)

    def on_announcement(self) -> bool:
        self.announcements += 1
        try:
            return self.cache.refresh()
        except SnapshotError:
            logger.exception("ruleset refresh failed", extra={"tenant": self.cache.tenant_id})
            return False


def announce(client: Any, tenant_id: str, version: int) -> None:
    """Tell every worker a new version exists. Best effort, by design.

    A failure here is logged and swallowed: the publish already succeeded, and
    the poll will carry the change within its interval. Making activation depend
    on a successful broadcast would mean a Redis blip could leave a rule
    published but not in force, which is the worst of both.
    """
    try:
        client.publish(VERSION_CHANNEL, f"{tenant_id}:{version}")
    except Exception:
        logger.warning("could not announce ruleset version", exc_info=True)
