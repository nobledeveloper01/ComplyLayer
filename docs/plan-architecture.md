# Architecture plan

The product specification (`docs/product-specification.md`) fixes the shape of the system: an
AST-validating rule interpreter rather than `eval`, Redis sorted sets for velocity, an in-process
versioned rule cache, frozen rule set snapshots for reproducibility, three-layer tenancy, and
fail-open/fail-closed chosen per rule severity.

This document decides the things the specification names but leaves open, and records the places
where following the specification literally would not meet its own targets. Each decision below
carries its consequence, because a decision without a stated cost is a decision nobody checked.

---

## D1 — The decision path does not go through DRF

**Problem.** The latency budget allows 3 ms for auth and request validation and 2 ms for response
serialisation. A DRF request cycle — `Request` wrapping, content negotiation, a `Serializer` with
field-by-field validation, a renderer — routinely costs more than that on its own, before any
compliance logic runs. The budget was written as though the framework were free.

**Decision.** Two request paths in one codebase:

| Path | Stack | Budget |
|---|---|---|
| `POST /v1/decisions` | Plain Django ASGI view, hand-written validation against a frozen input schema, `orjson` in and out, minimal middleware list | 25 ms p99 synchronous |
| Everything else (rules, decisions search, analytics, reports, admin) | DRF, full middleware, DRF permissions and throttling | 500 ms p99 |

The decision view does its own validation because the input schema is small, closed and versioned —
about fifteen fields — and because an unknown field must be *rejected*, not ignored, per §8.4.

**Consequence.** Two URL confs and two middleware stacks to keep honest. Mitigated by making the
split load-bearing rather than cosmetic: see D7 — a decision pod does not load the management
URLconf at all, so a rule-management endpoint is not merely forbidden there, it does not exist.

**This decision is provisional and gets measured.** Phase 2's exit gate benchmarks the same endpoint
implemented both ways. If DRF lands inside the budget with headroom, the split is deleted and this
entry is superseded. Recorded in ADR-0002.

---

## D2 — Idempotency and the block-rule lock have to fit inside the one Redis round trip

**Problem.** The specification's budget itemises auth, rule lookup, fact gathering, evaluation and
serialisation. It omits two things it requires elsewhere: the idempotency lookup (A4) and the
per-customer lock that makes `block`-severity velocity rules race-free (§4.5, "roughly 5 ms"). Added
naively these are two extra round trips and the budget is 35 ms before anything goes wrong.

**Decision, part one — idempotency joins the fact pipeline.** The idempotency `GET` is issued in the
same pipeline as the velocity trims and ranges. One round trip still. The decision body is cached in
Redis under `idem:{tenant}:{key}` for 24 hours; Postgres holds it permanently and serves the rare
post-expiry replay through the management path, where the latency budget is not 100 ms.

**Decision, part two — concurrent duplicates are allowed to double-evaluate.** Two simultaneous
requests carrying the same idempotency key can both miss the cache. Because evaluation is
deterministic against a pinned rule set version (§3.4), both produce the identical outcome, so the
caller is never given two different answers. The only real risk is two audit records for one
decision, and that is already prevented by `unique_together = [('tenant', 'idempotency_key')]` on
`Decision`: the second writer loses the insert and reads the winner's row. No distributed lock is
needed for idempotency, and adding one would cost more than the problem does.

**Decision, part three — superseded in phase 3. There is no lock.**

The original decision here, following §4.5, was a short per-customer lock taken only when a
`block`-severity rule's velocity count landed within one of its threshold, on the reasoning that
away from the boundary the answer is the same either way and the ~5 ms is not worth paying.

**The concurrency test disproved it on the first run: 11 transactions passed a threshold of 5.**

The flaw is that "near the boundary" is measured against what *this* decision read, and with
concurrent writers a read is arbitrarily stale. Sixteen threads can each observe a count of zero and
each write. A lock that engages at the boundary cannot close a race that begins long before the
boundary is anywhere in sight.

**The replacement is simpler, faster and exact.** The read and the write became one atomic operation:
`record_and_gather` adds this transaction and returns the resulting window inside a single
MULTI/EXEC. Redis serialises them, so every concurrent decision gets a distinct, consistent count.

| | Lock, as designed | Atomic record-and-read |
|---|---|---|
| Round trips | 2 (gather, then gather again under lock) | 1 |
| Extra latency at a boundary | ~5 ms plus a second evaluation | none |
| Exact for `block` rules | no, as measured | yes |
| Exact for `flag` rules | no, accepted as a trade | yes, for free |

The window now includes the transaction being decided, which is the natural reading of "more than
five transfers in an hour" anyway: the sixth one is the one that trips it.

**The trade §4.5 made no longer has to be made.** It accepted imprecision for `flag` rules as the
price of not locking. With no lock to avoid, that price is not owed, and a reviewer's queue is not
padded with transactions that only appear to have crossed a threshold.

**What this cost to learn:** the lock, the per-comparison boundary detection in the interpreter, and
the `needs_boundary_lock` signal through the engine were all built before the test ran, and all
deleted after. Cheaper than shipping it.

---

## D13 — The rule cache loads from the primary, not the replica

**Problem, found while wiring the decision path.** §11.1 says analytics never touch the database
serving decisions, so the first version of the rule-set loader read from the replica.

**That was wrong.** A replica lags, and this is the one read where lag becomes a compliance problem
rather than a stale dashboard: a worker that loads version 46 because the replica has not caught up
is serving decisions against a control that was retired. That is precisely the version skew §11.6
pages on, arriving by design instead of by accident.

**Decision.** The rule-set load goes to the primary. §11.1's concern is backtests, reports and
dashboard queries — big, frequent, and tolerant of a second's lag. This is one indexed row per
version change per worker, on the path that decides what is in force.

**Consequence.** The primary serves a small extra query whenever a version changes. Backtests,
reports and analytics still read the replica, which is where the volume actually is.

---

## D3 — The audit write is asynchronous through a local durable queue, not through Redis

**Problem.** §4.2 says the audit write is queued and reconciled on startup, without saying what the
queue is. The choice determines what an outage actually loses.

**Options considered.**

| Option | Durability | Failure coupling |
|---|---|---|
| Synchronous Postgres write | Perfect | Puts the database on the critical path — budget gone |
| Redis Stream, AOF `everysec` | ≤ 1 s loss | Velocity and audit die together; the one dependency whose loss already degrades decisions also loses the record of the degradation |
| **Local append-only file per pod, batched fsync, background drainer** | ≤ 200 ms loss on ungraceful node loss | Independent of both Redis and Postgres |

**Decision.** The third. Each pod appends the decision record to a local segment file, fsyncing on a
200 ms or 256-record boundary, whichever comes first, and a drainer ships segments into Postgres and
truncates them. On start, any segment present is drained before the pod reports ready.

**Consequence, stated plainly.** On a graceful shutdown nothing is lost — the drain is inside
`terminationGracePeriodSeconds`. On a node that disappears without warning, up to 200 ms of audit
records for that pod are lost while the decisions they describe were already returned to the
customer. That is the trade-off §4.2 refers to, quantified. The alternative — a synchronous write —
trades a rare 200 ms audit gap for a permanent 8–15 ms on every decision, and this product's promise
is the latency.

The audit *trail* in §8.3 (rule changes, approvals, overrides) is not on this path. Those are
management-path writes, synchronous, hash-chained, and never lost. Only the high-volume decision
record uses the queue. Recorded in ADR-0003.

---

## D4 — Row Level Security requires transaction-mode pooling and a non-owner app role

**Problem.** §8.1 layer three is Postgres RLS keyed on a session variable. Two well-known ways to
get this wrong turn it from a safety net into a false one.

**Decision.**

- The tenant is set with `SET LOCAL complylayer.tenant_id`, which is transaction-scoped. Every
  request body runs inside an explicit atomic block. A session-scoped `SET` combined with a pooled
  connection would leak one tenant's context into another tenant's request, which is the exact
  failure RLS was added to prevent.
- pgbouncer runs in **transaction** mode. Session mode plus `SET LOCAL` is fine; session mode plus a
  stray session-scoped `SET` is not, and transaction mode removes the possibility.
- The application role is **not the owner of any table**, and — the part this decision originally
  missed — **not a superuser and not granted `BYPASSRLS`**. `FORCE ROW LEVEL SECURITY` handles the
  owner case, and handles nothing else: Postgres exempts SUPERUSER and BYPASSRLS roles from every
  policy unconditionally, whatever the table says.

  Found by writing the test in phase 5. The policies were correct, `FORCE` was set, `\d` showed
  everything right, and every policy was being skipped — because the role `docker-compose` creates
  by default is a superuser. Layer three was decoration, and nothing in the schema said so.

  Migration `0006` creates `complylayer_app` with `NOSUPERUSER NOBYPASSRLS`, and
  `complylayer_doctor` inspects the connecting role, because a control that is invisible when it
  fails needs something that looks at it deliberately.
- A CI test opens a fresh pooled connection and asserts `current_setting('complylayer.tenant_id',
  true)` is null before any request sets it, then asserts a cross-tenant read returns zero rows with
  the query layer deliberately bypassed.

---

## D5 — The parser is a denial-of-service surface, and the decision path never touches it

**Problem.** The specification caps node count at 200 and evaluation steps at 1000. Both are
enforced *after* `ast.parse` returns. `ast.parse` itself is the exposed surface: deeply nested
parentheses or unary operators drive CPython's recursive-descent parser into the C stack, and the
node ceiling never gets a chance to apply. A 200-node limit does not protect a parser that has
already been handed 50,000 open brackets.

**Decision — three layers, in order.**

1. **Pre-parse guards.** Source length capped at 2,000 characters; a linear scan rejects bracket
   depth above 20 and runs of unary operators above 5. This is a cheap string scan and runs before
   any parsing.
2. **The parse happens only in the management API.** Rules are parsed, validated and compiled at
   *publish* time, and the compiled, validated AST is what the frozen `RuleSetVersion` snapshot
   carries. The decision endpoint never parses anything. The most dangerous component in the product
   is therefore not reachable from the endpoint that handles untrusted volume — it is reachable only
   by an authenticated compliance user whose every action is audited and rate-limited.
3. **Re-validation on load.** Each pod re-runs the AST allowlist against the snapshot when it loads a
   rule set, and refuses to serve a rule set that fails. A snapshot is data in a database, and data
   in a database is a thing an attacker who has reached the database can edit. Re-validating costs
   milliseconds once per version and closes that path.

**Consequence.** Rule publication is slower than rule evaluation by a wide margin. That is the
correct direction for the cost to run.

---

## D6 — Division leaves the DSL, because 100% reproducibility and floats do not coexist comfortably

**Problem.** §3.4 requires byte-identical output for the same input and rule set version, forever,
across pods and across future hardware. §4.3's `ALLOWED_NODES` includes `ast.Div`, which produces
floats. IEEE-754 is deterministic in principle, but a rule set that compares float results and a
decision record that serialises them invites a class of discrepancy that is miserable to debug and
impossible to explain to an auditor six months later.

**Decision.** `ast.Div` is removed from the allowlist. All arithmetic in the DSL is integer
arithmetic over minor units. `percent_of(amount_minor, pct)` is defined as `amount_minor * pct //
100`, integer in and integer out, with the rounding direction documented in the function reference
because a compliance officer will eventually ask which way ₦1 goes.

**Consequence.** A rule cannot express "more than 3.5% of balance". It can express
`percent_of(balance_minor, 35) // 10`, or better, the threshold is stated in minor units where it
belongs. The expressiveness lost is smaller than the reproducibility gained.

---

## D7 — One image, two workloads, and the separation is real

Decision pods and management pods run the same container image and differ only by
`COMPLYLAYER_ROLE`. The role selects the settings module, and the settings module selects the
URLconf. A decision pod has no route to `POST /v1/rules/{id}/activate` — not a 403, no such URL.
Management pods do not register the decision route, so a heavy backtest cannot be scheduled onto a
pod serving the critical path.

This makes §11.1's "separate the decision workload from the management workload" enforceable rather
than aspirational, and it makes the blast radius of a management-path vulnerability stop at the
management pods.

---

## D8 — Reported latency is quantised; responses are not padded

**Problem.** §10's threat table calls for response timing padded to a floor to hide rule structure.
§3.4 requires p50 under 20 ms. A floor high enough to hide anything would breach the p50 target,
and the two requirements were written without being read against each other.

**Decision.** No per-response padding. Instead:

- The `latency_ms` field in the response is rounded to the nearest 5 ms, which costs nothing and
  removes the most convenient measurement channel.
- The actual defence against threshold probing stays where §10 already put it: per-customer-reference
  rate limiting, `block` responses that never state the numeric threshold, and the built-in
  sequential-probing rule.
- The residual risk is accepted and written down: an attacker with a very large number of requests
  through a tenant's own application could infer roughly how many rules matched. They would need the
  tenant's application to cooperate in the volume, and the tenant is the party the rules protect.

Padding is not merely skipped; it is refused for a stated reason, so it does not get quietly added
back during a security review by someone reading §10 in isolation.

---

## D9 — Velocity counters are written at decision time, and they count attempts

**Problem.** §4.5 shows the read. Nothing in the specification writes. If the decision call does not
add the transaction to the sorted set, every velocity rule evaluates against an empty window and
silently never fires — which looks exactly like a system observing no suspicious activity.

**Decision.** The decision call writes. The window therefore counts *attempts*, including
transactions this very call blocked.

This is not a compromise made for the sake of one round trip. Someone who attempts eleven transfers
just below the reporting threshold and has six declined has exhibited structuring, and a window
counting only settled transactions would miss it. Counting attempts is the more correct AML
semantic, and it keeps the integration promise at one call.

A tenant who needs settled-only counts can opt into `POST /v1/decisions/{id}/confirm`, which moves
the write to confirmation time. The cost — a second call on the critical path — is theirs to choose.

**Consequence.** A customer reconciling ComplyLayer's counts against their own ledger will find them
higher. That has to be stated in the integration guide, not discovered.

The sorted-set member is the transaction id, which makes the write idempotent under retry. That is
load-bearing rather than incidental, and the code says so in a comment.

---

## D10 — `Decision` is partitioned from the first migration, and retention is tiered

**Problem.** Two numbers from the specification, multiplied. 2,000 decisions/sec (§3.4) is 172.8
million rows a day. With the JSONB context at roughly 2 KB a row that is ~345 GB a day, and §11.7's
seven-year retention lands near a petabyte — in an unpartitioned table that also serves the review
queue and analytics.

**Decision.**

- `Decision` is declaratively partitioned by month from the very first migration. Adding partitioning
  to a live table of this size is a project; adding it now costs one afternoon.
- Retention is tiered: 90 days hot in Postgres, then Parquet in object storage for the remainder of
  the seven years. §11.7 already replicates decisions to object storage under a write-once lock —
  this makes that copy the system of record for cold data rather than a duplicate of the hot one.
- Reports and exports read the cold tier. The review queue and analytics read the hot tier only.

**Consequence.** A regulator's question about a decision from four years ago is answered from object
storage and takes seconds rather than milliseconds. That is the right place for the latency to go.

No early customer will sustain 2,000/sec. That is precisely why this is decided now.

---

## D11 — What "reproducible" actually means, stated precisely enough to test

§3.4 asks for byte-identical output. The response contains `decision_id`, `decided_at` and
`latency_ms`, none of which is deterministic, so a byte-equality test would either fail always or
quietly exclude those fields — and a determinism test with a quiet exclusion is worse than none.

**Decision — three parts.**

1. **The requirement, restated.** For the same input and the same rule set version, `outcome`,
   `matched_rules`, `shadow_matches`, `reason` and `ruleset_version` are identical. Identity, timing
   and measurement fields are excluded, explicitly and in the test's name.
2. **`matched_rules` is ordered by (priority, rule_id).** Otherwise the order is a set-iteration
   accident that happens to be stable until the day it is not.
3. **Resolved facts are stored with the decision.** `Decision.context` holds the input *and* every
   fact as resolved at decision time — every velocity count, every aggregate. Redis holds a 30-day
   rolling window; replaying a 90-day-old decision against live Redis re-gathers facts that no longer
   exist and would present a different answer as a reproduction.

**Consequence, and it is a real limitation.** A backtest is exact for rules over facts that were
already gathered, and approximate for a rule needing a window that was never recorded. The UI says
which one it is showing. A compliance officer over-trusting an approximate number is a worse outcome
than showing her the caveat.

**Related: named lists are versioned into the snapshot.** `in_list(destination_country,
high_risk_countries)` reads tenant-configured mutable data. Left outside `rules_snapshot`, editing
the list would change decisions without changing the rule set version, and two decisions recording
the same version would no longer represent the same control. Editing a list publishes a new version,
exactly as editing a rule does.

---

## D12 — The cache lives in a worker process, not in a pod

Gunicorn and uvicorn run multiple worker **processes**. Each has its own memory, so each has its own
compiled rule set and its own pub/sub subscription.

- `complylayer_ruleset_version` is labelled **per worker**. Labelled per pod, it hides skew inside a
  pod, which is the same silent failure §11.2 built the metric to catch.
- The cache build and the pub/sub subscribe happen in a **post-fork hook**. A Redis connection
  created before fork is shared across children, which fails in ways that take a long day to
  diagnose.
- "Atomic swap" means rebinding a module-level name to a new immutable object. That is safe in
  CPython and the code carries a comment saying so, because the next person will reach for mutating
  the dict in place.
- The 30-second polling backstop matters more here than the pub/sub does: a per-worker subscription
  is one more thing that can die quietly.

---

## Latency budget, restated with the omissions filled in

```
Total budget: 100 ms p99, measured at the API edge

  Request read + validation (no DRF)          2 ms
  API key verification (Argon2id, cached)      1 ms   ← verified once, cached 60 s by key prefix
  Rule set lookup                              0 ms   ← in-process, versioned, pre-compiled
  ONE Redis pipeline:
      idempotency probe
      velocity trims + ranges (all windows)
      amount hashes
      aggregate facts                         15 ms
  Rule evaluation (N rules, pure computation)  5 ms
  Response serialisation (orjson)              2 ms
  ────────────────────────────────────────────────
  Synchronous total                           25 ms
  + block-rule boundary lock, when taken       5 ms   ← < 1% of decisions (D2)
  Headroom                                    70 ms

  Audit append to local segment           in-line, ~0.05 ms, fsync batched (D3)
  Audit drain to Postgres                 async
  Review queue insert                     async
  Metrics emission                        async
```

Argon2id deserves a note: verifying it is *designed* to be slow — tens of milliseconds at sane
parameters, which is the whole point for password storage and completely wrong on a per-request hot
path. Keys are verified once and the result cached in-process for 60 seconds against the key prefix,
with revocation propagated over the same pub/sub channel the rule cache uses. Without that cache the
key check alone would consume half the budget.

---

## Open questions, after the review pass

Answered by the review (`docs/plan-review-report.md`):

1. **Multi-region** — no. v1 is single region. Multi-region makes velocity counters genuinely hard,
   because a customer's transactions must all reach one region or the windows are wrong, and nothing
   in v1 needs it.
2. **NGN and CBN first** — yes, and the data model stays currency-neutral. `amount_minor` plus
   `currency` already is. Specific templates beat generic ones; the specificity is a feature.

Decided at the review gate:

3. **Distribution** — the embedded `pip install` path leads, with docker-compose self-host beside it.
   Hosted, Helm and Terraform defer to v1.1. It removes the availability objection rather than
   answering it. (C1)
4. **Phases 0–4 are a shippable milestone**, delivering all three claims §13 rests on. Phases 5–8 are
   commercial surface. Nothing is cut; the ordering is now explicit. (C4)
5. **One SDK for v1: Node**, plus a complete `openapi.yaml`. Python and Go follow demand. (X4)

The full reasoning for each is in `docs/plan-review-report.md`.
