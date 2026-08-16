# ADR-0002 — The decision endpoint is a plain Django view

**Status:** superseded by its own benchmark. The latency argument is refuted;
consolidation onto DRF is the follow-up.
**Date:** 2026-08-16 (measured phase 2), settled phase 5

## Context

The latency budget allows 2 ms to read and validate a decision request and 2 ms
to serialise the response. D1 in `docs/plan-architecture.md` argued that a DRF
request cycle costs more than that on its own, and that the budget had been
written as though the framework were free.

That was an argument, not a measurement. The plan committed to settling it here
rather than leaving a second HTTP path in the repository on the strength of an
assumption. This ADR now records both halves of that measurement.

## The measurements

`make bench`, after a warm-up, 100 active rules, identical handler and store
behind both paths, Python 3.12.13 / Django 5.2.17 on an M-series laptop.

**Phase 2 — the plain path alone:**

| | p50 | p99 |
|---|---|---|
| Rule evaluation (100 rules) | 0.285 ms | 0.301 ms |
| Full plain-Django request cycle | 0.323 ms | 0.406 ms |
| Validation + serialisation in isolation | **3.0 µs** | **3.2 µs** |

Three microseconds against a five *millisecond* budget. That was already a weak
result for the argument: DRF would have to be roughly 1,700 times slower to
breach it. The ADR recorded D1 as *survivable, not proven*, and owed the
comparison the day DRF became a dependency.

**Phase 5 — the comparison, DRF now installed:**

| | p50 | p99 |
|---|---|---|
| Plain Django | 0.324 ms | 0.375 ms |
| DRF | 0.352 ms | 0.447 ms |
| **DRF overhead** | +0.028 ms | **+0.072 ms** |

## The verdict

**Seventy-two microseconds, against a hundred-millisecond contract. D1's latency
argument is refuted.**

DRF is not free, and on a tighter budget the difference could matter. It does not
matter here. The framework overhead this ADR chose a second HTTP path to avoid is
0.07% of the contract, and about 0.3% of the budget line it was defending.

## What survives, and what does not

**Does not survive:** the reason for two view implementations. Nothing in the
measurement justifies maintaining a hand-written request path alongside a DRF
one.

**Survives, on other grounds:**

- **D7's workload separation.** Decision workers and management workers run the
  same image with different settings modules, so a decision worker has no route
  to rule management at all. That is a URLconf decision, not a view-framework
  one, and it holds whichever framework the decision endpoint uses.
- **Rejecting unknown fields.** §8.4 wants a payload carrying a PAN to fail
  rather than be stored and redacted. DRF serialisers ignore unknown fields by
  default, so consolidating means writing that check explicitly rather than
  inheriting it. It is a few lines, and it must not be lost in the move.

## The follow-up, scoped

Consolidate the decision endpoint onto DRF and delete
`complylayer/api/decision.py`. The work is small because the handler was
deliberately kept framework-agnostic from the start — it is the view wrapper that
goes, not the logic:

1. A DRF view calling the existing `DecisionHandler`.
2. An explicit unknown-field check, carried over from `validation.py`.
3. The decision URLconf pointed at it.
4. `tests/test_decision_endpoint.py` runs unchanged; the benchmark keeps both
   until the plain path is gone.

Deliberately **not** done in the same change as the measurement. Deleting a
tested, working path in the same commit that justifies deleting it makes the
diff hard to review and the decision hard to revisit. The measurement is the
finding; the deletion is the next change.

## The lesson worth keeping

The argument was plausible, specific, and wrong — and it stayed in the repository
as a second code path for three phases because nobody could measure it yet. The
thing that eventually settled it was a benchmark written at the same time as the
decision, checked in, and run again when the missing dependency arrived.

Two code paths held together by an untested belief is exactly what survives for
years because nobody re-checks it. What made this one testable was writing the
benchmark before the answer was available.
