# ADR-0002 — The decision endpoint is a plain Django view

**Status:** accepted, with the deciding comparison still outstanding
**Date:** 2026-08-16

## Context

The latency budget allows 2 ms to read and validate a decision request and 2 ms
to serialise the response. D1 in `docs/plan-architecture.md` argued that a DRF
request cycle — `Request` wrapping, content negotiation, a `Serializer`
validating field by field, a renderer — routinely costs more than that on its
own, and that the budget had been written as though the framework were free.

That was an argument, not a measurement. The plan committed to settling it here
rather than leaving a second HTTP path in the repository on the strength of an
assumption.

## Decision

`POST /v1/decisions` is a plain Django view with hand-written validation and
`orjson` in both directions. Everything else — rules, approvals, analytics,
reports — goes on DRF from phase 5, in a separate settings module, so a decision
worker does not load the management URLconf at all (D7).

## The measurements

`make bench`, 2,000 iterations after a 100-call warm-up, 100 active rules,
Python 3.12.13 on an M-series laptop:

| | p50 | p99 |
|---|---|---|
| Rule evaluation alone (100 rules) | 0.285 ms | 0.301 ms |
| Full plain-Django request cycle | 0.323 ms | 0.406 ms |
| Validation + serialisation in isolation | **3.0 µs** | **3.2 µs** |

## What the numbers actually say, including against this decision

The hand-written validation and serialisation cost **3 microseconds**. The budget
line D1 was defending is 5 milliseconds. The path this ADR chose uses 0.06% of
the allowance it was designed to protect.

That is a good result for the endpoint and a weak one for the argument. For DRF
to breach a 5 ms budget it would have to be roughly 1,700 times slower than the
hand-written path. DRF is slow by the standards of this budget, but it is not
that slow — a full DRF cycle is typically 1–3 ms, which would fit, with less
headroom but fit nonetheless.

**So the honest position is that D1 has not yet been proven, only shown to be
survivable.** The claim that DRF cannot meet the budget remains untested, because
DRF is not a dependency of this project until phase 5.

## What is still owed

When DRF arrives in phase 5, `tests/test_decision_benchmark.py` gains the second
half: the identical handler behind a DRF view, measured the same way. Then one of
two things happens.

- **DRF lands outside the budget**, or inside it with little headroom: this ADR
  stands, and it stands on a measurement rather than an expectation.
- **DRF lands comfortably inside it**: the plain path is deleted, this ADR is
  superseded, and the repository loses a second HTTP surface it did not need.
  That is the outcome to hope for, because one path is cheaper to maintain than
  two and the split only earns its keep if the numbers demand it.

Two code paths held together by an untested belief is exactly the kind of thing
that survives for years because nobody re-checks it. The benchmark is checked in
so that re-checking is one command.

## Consequences

- Two request paths and two middleware stacks until phase 5 resolves it. Mitigated
  by D7, which makes the split load-bearing rather than cosmetic: a decision
  worker has no route to rule management at all.
- Request validation is hand-written. Reasonable here because the schema is small,
  closed and versioned — and because it lets unknown fields be *rejected* rather
  than ignored, which §8.4 wants and which a permissive serialiser would not give.
- The endpoint has 99.6 ms of headroom against the 100 ms contract before any
  Redis round trip or audit write. Phase 3 and phase 4 will spend most of that;
  the point of recording it now is to know how much was there to begin with.
