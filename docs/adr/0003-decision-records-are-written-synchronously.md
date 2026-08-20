# ADR-0003 — The decision record is written synchronously, and D3's queue was never built

**Status:** records a divergence, not a decision. `docs/plan-architecture.md`
D3 chose a durable local queue; the shipped code writes straight to Postgres.
The queue is outstanding and its justification is unmeasured.
**Date:** 2026-08-20, phase 8, at the first release

## Context

D3 asked where the decision record goes, and answered with a table:

| Option | Durability | Failure coupling |
|---|---|---|
| Synchronous Postgres write | Perfect | Puts the database on the critical path — budget gone |
| Redis Stream, AOF `everysec` | ≤ 1 s loss | Velocity and audit die together |
| **Local append-only file per pod, batched fsync, background drainer** | ≤ 200 ms loss on ungraceful node loss | Independent of both Redis and Postgres |

It chose the third: append to a local segment, fsync on a 200 ms or 256-record
boundary, drain into Postgres in the background, drain again on start before
reporting ready.

`docs/plan-architecture.md` says of that choice: *"Recorded in ADR-0003."* This
is that file, written two phases after it should have been, and it does not
record what the sentence promised.

## What actually shipped

`DecisionHandler.record` calls `DatabaseStore.save`, which opens a transaction
and does `Decision.objects.create(...)`. There is no segment file, no fsync
boundary, no drainer, and nothing to drain on start. The first option in D3's
table is what runs — the row the table describes as *"budget gone"*.

Its own docstring says so, and says when it would be fixed:

> §4.2 makes this asynchronous through a durable local queue (D3). Phase 2
> writes synchronously so the behaviour is settled and testable; the queue
> arrives with the latency work in phase 4, where it can be measured rather
> than assumed.

That was a reasonable sequencing decision. Phase 4 then shipped, and so did
phases 5, 6, 7 and 8, and the queue did not arrive. Nothing failed, because
nothing was watching: the commitment lived in a docstring, and a docstring is
not a gate.

## Why this is worth a file rather than a ticket

This project's README has a table of *controls that were configured and did
nothing* — RLS policies matching no rows, a velocity provider that returned the
wrong attribute, an image that had never been built. Each was found by running
the system rather than reading it, and each is now held down by a test.

This is the same failure with the sign flipped. Not a control that looks present
and is absent, but a **decision that was made, written down, argued for, and
never built** — while two documents went on describing the system as though it
had been. Someone reading `plan-architecture.md` to understand the durability
story would get an answer that is confidently wrong about the code.

## What is actually true now

**Postgres is on the decision critical path.** That is precisely what D3 existed
to prevent, so the trade-off it quantified now runs the other way:

- **Durability is better than D3 asked for.** The 200 ms audit gap on ungraceful
  node loss does not exist. A decision returned to a customer has its record
  committed, or the request failed.
- **Latency is worse, by an amount nobody has measured.** D3 estimated 8–15 ms
  per decision for a synchronous write. The contract is 100 ms and
  `tests/test_latency_benchmark.py` asserts only the evaluation stage, which is
  pure CPU and does not touch the store. So the number D3 was defending against
  is the one number the benchmark cannot see.
- **An outage now blocks decisions instead of losing records.** D3's option 2 was
  rejected partly because velocity and audit would die together. Under what
  shipped, Postgres going away takes the decision path with it.

## The measurement that would settle it

ADR-0002 is the precedent worth copying. It also began as a plausible latency
argument — that DRF was too expensive for the budget — and it stayed in the
repository as a second code path for three phases because nobody could measure
it. A benchmark written at the same time as the decision eventually refuted it
in seventy-two microseconds.

D3 deserves the same treatment before anybody builds a segment-file queue:

1. Extend the benchmark past the evaluation stage to a full `POST /v1/decisions`
   against a real Postgres, p50 and p99.
2. Compare against the same path with `record` stubbed out, which isolates the
   write.
3. If the write is a small fraction of 100 ms — as D3's own 8–15 ms estimate
   suggests it might be — then D3 is refuted the way D1 was, the synchronous
   write is correct, and this ADR closes by deleting a plan paragraph rather
   than by writing a drainer.
4. If it is not, build the queue, and the estimate becomes a measurement.

**The queue is not the deliverable. The measurement is.** Building a local
append-only segment store with fsync boundaries, crash recovery and a
start-up drain is a substantial piece of machinery with its own failure modes,
and D3 justified it on an estimate that no longer needs to be an estimate.

## The lesson worth keeping

A commitment that lives only in a docstring has no expiry and no owner. This one
named the phase it would be honoured in, that phase passed, and the sentence
stayed true-looking for five more phases because nothing reads docstrings for
overdue promises.

The two other places this project made a dated commitment — the escape corpus
and the README phase marker — are both enforced by scripts that fail the build.
That is the difference between a plan and an intention.
