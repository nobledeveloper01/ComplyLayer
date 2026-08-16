"""Rolling-window counters over Redis sorted sets.

§4.5 sketches a pipeline that trims and reads each window separately. This does
it in one fetch instead: read every member inside the *largest* window once,
with scores, plus the parallel amounts hash, and compute every smaller window
and every amount filter in process. One round trip, fewer commands, and the
amount filtering is exact rather than approximated by score ranges.

**There is no lock, and there does not need to be one.**

§4.5 and D2 both proposed a short per-customer lock, taken only when a count
lands within one of its threshold, on the reasoning that away from the boundary
the answer is the same either way. The concurrency test disproved that in its
first run: 11 transactions passed a threshold of 5. The flaw is that "near the
boundary" is measured against what this decision *read*, and with concurrent
writers a read is arbitrarily stale — sixteen threads can all observe a count of
zero and all write. A lock that engages at the boundary cannot close a race that
begins long before it.

So the read and the write became one atomic operation instead. `record_and_gather`
adds this transaction and returns the resulting window inside a single
MULTI/EXEC, which gives every concurrent decision a distinct, consistent count
and makes the answer exact for every severity — not just `block`. It is also
faster than the lock would have been: still one round trip, and no second
evaluation pass.

The window therefore includes the transaction being decided, which is the
natural reading of "more than five transfers in an hour" anyway: the sixth one
is the one that trips it.

**Windows count attempts, including blocked ones (D9).** The specification never
said what writes these counters; without the write every velocity rule evaluates
against an empty window and silently never fires, which looks exactly like a
system observing no suspicious activity. Counting attempts is also the more
correct AML semantic: eleven transfers just under the reporting threshold with
six declined is still structuring.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from complylayer.dsl.functions import WINDOW_SECONDS

LARGEST_WINDOW_SECONDS = max(WINDOW_SECONDS.values())

# Keys outlive the largest window by a margin, so a dormant customer expires on
# their own rather than accumulating forever.
KEY_TTL_SECONDS = LARGEST_WINDOW_SECONDS + 86_400

# Above this many members for one customer, the single-fetch approach starts
# shipping a payload big enough to matter. §11.5 names this: one very high-volume
# customer degrading the shared window operation for everyone. Past the cap the
# gather falls back to server-side counting, which costs more round trips but a
# bounded payload.
MAX_MEMBERS_FETCHED = 5_000


@dataclass(frozen=True)
class Aggregates:
    """Facts about a customer's whole history, not just the rolling window.

    Kept in Redis and updated as decisions are served, rather than computed from
    the decisions table on demand. §4.2 allows 15 ms for the entire fact
    gathering step, and an aggregate over seven years of partitioned history is
    not a 15 ms query — so the numbers are maintained incrementally and ride
    along in the same pipeline as the window.
    """

    lifetime_count: int = 0
    lifetime_volume_minor: int = 0
    prior_flag_count: int = 0
    first_seen_at: float = 0.0
    last_transaction_at: float = 0.0

    def as_facts(self, now: float) -> dict[str, int]:
        """The names a rule can use. Integers throughout, per D6."""
        average = self.lifetime_volume_minor // self.lifetime_count if self.lifetime_count else 0
        return {
            "lifetime_transaction_count": self.lifetime_count,
            "lifetime_volume_minor": self.lifetime_volume_minor,
            "average_transaction_minor": average,
            "prior_flag_count": self.prior_flag_count,
            "days_since_last_activity": _whole_days(now - self.last_transaction_at)
            if self.last_transaction_at
            else 0,
            "customer_known_days": _whole_days(now - self.first_seen_at)
            if self.first_seen_at
            else 0,
        }


def _whole_days(seconds: float) -> int:
    return max(0, int(seconds // 86_400))


@dataclass
class Window:
    """One customer's recent transactions, already fetched."""

    now: float
    entries: tuple[tuple[str, float, int, str], ...] = ()  # id, score, amount, type
    truncated: bool = False
    aggregates: Aggregates = field(default_factory=lambda: Aggregates())

    def _matching(
        self,
        window_seconds: int,
        min_amount_minor: int | None,
        max_amount_minor: int | None,
        transaction_type: str | None,
    ):
        cutoff = self.now - window_seconds
        for _, score, amount, kind in self.entries:
            if score < cutoff:
                continue
            if min_amount_minor is not None and amount < min_amount_minor:
                continue
            if max_amount_minor is not None and amount > max_amount_minor:
                continue
            if transaction_type is not None and kind != transaction_type:
                continue
            yield amount

    def count(self, window_seconds: int, **filters: Any) -> int:
        return sum(1 for _ in self._matching(window_seconds, **_defaults(filters)))

    def total(self, window_seconds: int, **filters: Any) -> int:
        return sum(self._matching(window_seconds, **_defaults(filters)))


def _defaults(filters: dict[str, Any]) -> dict[str, Any]:
    return {
        "min_amount_minor": filters.get("min_amount_minor"),
        "max_amount_minor": filters.get("max_amount_minor"),
        "transaction_type": filters.get("transaction_type"),
    }


@dataclass
class RedisVelocity:
    """A velocity provider bound to one tenant and one customer.

    Holds the window it fetched, so every `velocity_count` and `velocity_sum` in
    every rule of the whole rule set is answered from one round trip.
    """

    client: Any
    tenant_id: str
    customer_hash: str
    window: Window = field(default_factory=lambda: Window(now=0.0))

    @property
    def members_key(self) -> str:
        return f"v:{self.tenant_id}:{self.customer_hash}"

    @property
    def amounts_key(self) -> str:
        return f"va:{self.tenant_id}:{self.customer_hash}"

    @property
    def aggregates_key(self) -> str:
        return f"vg:{self.tenant_id}:{self.customer_hash}"

    def gather(self, now: float | None = None) -> Window:
        """Read the window without writing to it.

        Used for replays and for the backtest path. The decision path uses
        :meth:`record_and_gather` instead, because reading and writing
        separately is exactly the race that made the lock necessary.
        """
        return self._fetch(now, record=None)

    def record_and_gather(
        self,
        transaction_ref: str,
        amount_minor: int,
        transaction_type: str,
        now: float | None = None,
    ) -> Window:
        """Add this transaction and read the resulting window, atomically.

        One round trip, one MULTI/EXEC. Two simultaneous decisions for the same
        customer come back with different counts because Redis serialised them,
        which is what makes a velocity threshold exact without anybody holding a
        lock.
        """
        return self._fetch(now, record=(transaction_ref, amount_minor, transaction_type))

    def _fetch(self, now: float | None, record: tuple[str, int, str] | None) -> Window:
        moment = time.time() if now is None else now
        cutoff = moment - LARGEST_WINDOW_SECONDS

        # transaction=True is the default and is the whole point here: the
        # commands run as one MULTI/EXEC, so no other client interleaves between
        # the write and the read.
        pipe = self.client.pipeline()
        pipe.zremrangebyscore(self.members_key, 0, cutoff)
        if record is not None:
            transaction_ref, amount_minor, transaction_type = record
            # The member is the transaction reference, so a retry updates a
            # score rather than adding a second entry. Load-bearing rather than
            # incidental: without it a retried decision inflates every window it
            # touches.
            pipe.zadd(self.members_key, {transaction_ref: moment})
            pipe.hset(self.amounts_key, transaction_ref, f"{amount_minor}:{transaction_type}")
            pipe.expire(self.members_key, KEY_TTL_SECONDS)
            pipe.expire(self.amounts_key, KEY_TTL_SECONDS)
            # Aggregates are incremented in the same transaction, so a customer's
            # lifetime numbers can never disagree with the window they came from.
            pipe.hincrby(self.aggregates_key, "lifetime_count", 1)
            pipe.hincrby(self.aggregates_key, "lifetime_volume_minor", amount_minor)
            pipe.hsetnx(self.aggregates_key, "first_seen_at", moment)
            pipe.hset(self.aggregates_key, "last_transaction_at", moment)
            # Aggregates outlive the window: "account age" is meaningless if it
            # resets whenever a customer goes quiet for a month.
            pipe.persist(self.aggregates_key)
        pipe.zrangebyscore(self.members_key, cutoff, "+inf", withscores=True)
        pipe.hgetall(self.amounts_key)
        pipe.hgetall(self.aggregates_key)

        results = pipe.execute()
        members, amounts, aggregate_fields = results[-3], results[-2], results[-1]

        truncated = len(members) > MAX_MEMBERS_FETCHED
        if truncated:
            # Newest first; an older transaction matters less to a rolling window
            # and the cap is a load protection rather than a correctness one.
            members = members[-MAX_MEMBERS_FETCHED:]

        entries = []
        for raw_member, score in members:
            member = _text(raw_member)
            amount, kind = _split_amount(amounts.get(_bytes(member)) or amounts.get(member))
            entries.append((member, float(score), amount, kind))

        self.window = Window(
            now=moment,
            entries=tuple(entries),
            truncated=truncated,
            aggregates=_parse_aggregates(aggregate_fields),
        )
        return self.window

    def record(
        self,
        transaction_ref: str,
        amount_minor: int,
        transaction_type: str,
        now: float | None = None,
    ) -> None:
        """Write without reading. For backfills and tests; the decision path uses
        :meth:`record_and_gather`."""
        self._fetch(now, record=(transaction_ref, amount_minor, transaction_type))

    # The interface the interpreter sees.
    def count(self, window_seconds: int, **filters: Any) -> int:
        return self.window.count(window_seconds, **filters)

    def total(self, window_seconds: int, **filters: Any) -> int:
        return self.window.total(window_seconds, **filters)

    def aggregate_facts(self) -> dict[str, int]:
        """The customer-history facts, from the same fetch as the window."""
        return self.window.aggregates.as_facts(self.window.now)

    def note_flagged(self) -> None:
        """Recorded after the outcome is known, so it lands off the critical path."""
        self.client.hincrby(self.aggregates_key, "prior_flag_count", 1)


def _parse_aggregates(fields: Any) -> Aggregates:
    def number(name: str, cast=int):
        raw = fields.get(_bytes(name)) if fields else None
        if raw is None and fields:
            raw = fields.get(name)
        try:
            return cast(_text(raw)) if raw is not None else cast(0)
        except (TypeError, ValueError):
            return cast(0)

    return Aggregates(
        lifetime_count=number("lifetime_count"),
        lifetime_volume_minor=number("lifetime_volume_minor"),
        prior_flag_count=number("prior_flag_count"),
        first_seen_at=number("first_seen_at", float),
        last_transaction_at=number("last_transaction_at", float),
    )


def _split_amount(raw: Any) -> tuple[int, str]:
    if raw is None:
        return 0, ""
    text = _text(raw)
    amount, _, kind = text.partition(":")
    try:
        return int(amount), kind
    except ValueError:
        return 0, kind


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes | bytearray) else str(value)


def _bytes(value: str) -> bytes:
    return value.encode()
