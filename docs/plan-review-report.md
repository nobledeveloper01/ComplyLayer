# GSTACK REVIEW REPORT — ComplyLayer plan

Run: `/autoplan` over `docs/plan-architecture.md` and `docs/ROADMAP.md`, against
`docs/product-specification.md`. Branch `main`, no commits yet.

**Voices: `[single-reviewer]`.** The Codex binary is not installed on this machine, so the dual-voice
pass degraded to one reviewer. Findings below are one model's, not two agreeing. Install `codex` and
re-run for the second voice.

Scope detected: **UI yes** (rule builder, review queue, approvals, analytics), **DX yes** (three
SDKs, `pip install`, management commands, API contract, error messages). All four phases ran.

---

# Phase 1 — CEO review (strategy and scope)

## 0A. Premise challenge

| # | Premise the plan rests on | Verdict |
|---|---|---|
| P1 | A compliance officer at the target customer both wants to and is able to write rules | **Challenged** — see C3 |
| P2 | A fintech will accept a synchronous third-party dependency on its transaction path | **Challenged** — see C1 |
| P3 | Sub-100 ms is the binding requirement | **Partly wrong** — availability binds harder, see C2 |
| P4 | Multi-tenancy from the first commit | Accepted. A day now, a rewrite later. |
| P5 | This is a portfolio artefact (§13) as well as a product | Accepted, and it changes the scope maths — see C4 |

### C1 — The embedded path is the product; the hosted API is the upsell

§6 lists three distribution forms and orders them hosted → `pip install` → self-hosted docker. The
ordering is backwards for the objection that actually kills deals.

Tunde's first question in §3.1 is "what happens when you are down?" The specification answers with
a fail-open/fail-closed policy, which is a good answer to a question the customer would rather not
have to ask. `from complylayer import evaluate` (§6.2) makes the question disappear: no network hop,
no vendor availability, no data leaving the customer's network, and the latency claim becomes
trivially true rather than hard-won.

For a fintech evaluating a compliance control, "runs inside your Django process, your Postgres, your
Redis" is a materially easier yes than "add a third-party call before every transfer".

**Recommendation: v1 ships the embedded library and the docker-compose self-host. Hosted, Helm and
Terraform defer.** This is a change to your stated scope, so it goes to the gate rather than being
auto-decided.

### C2 — 99.95% is the weak number, not the 100 ms

The plan invests heavily in latency and states availability once, in a table. Do the arithmetic that
a customer's risk team will do: 99.95% is 4 hours 23 minutes per year. For a tenant using
`block`-severity rules with the specified fail-closed default, that is 4 hours 23 minutes per year
during which transactions that would have been allowed are refused.

Worse, §11.1 makes Redis a hard dependency for velocity rules, so a Redis failure — not a
ComplyLayer failure — produces the same outcome. The blast radius of a Redis outage is "the customer
cannot move money", and that is a bigger sentence than anything in the latency section.

This is not an argument against the latency work, which is the strongest engineering artefact in the
project. It is an argument that the availability story is currently one row in a table and needs the
same treatment: what the fallback actually does per rule, what the customer sees, how long it lasts,
and what the embedded mode changes about all three. It also reinforces C1 — in embedded mode,
ComplyLayer's availability is the customer's own availability.

### C3 — The buyer may not be the persona

Adaeze, Head of Compliance, is drawn convincingly. At a licensed fintech she exists. At the "early
stage fintech" the Starter tier targets, compliance is often a hat worn by the COO, and the person
who feels the §2.1 pain first is the engineer who has shipped the same limit change four times.

That does not break the product. It changes who you sell to and therefore what v1 must be good at:
if the engineer is the wedge and compliance is the adopter, then developer experience, the
`complylayer_doctor` preflight, and the quality of the rule DSL errors matter more in v1 than
dashboard polish. The dashboard is what makes the second year work; the DX is what makes the first
call happen.

### C4 — §13 says this is a portfolio artefact, and that should be stated in the roadmap

The three claims in §13 that survive follow-up questions are the sandbox, the itemised latency
budget, and reproducible decisions. Roadmap phases 1 through 4 deliver all three. Phases 6 through 8
deliver commercial surface — a React dashboard, three SDKs, a Helm chart, Terraform.

The roadmap currently treats all nine phases as equally load-bearing. It should say plainly that
**phases 0–4 plus a thin CLI are a complete, demonstrable artefact**, and that everything after is
product surface. That is not a reason to cut phases 5–8; it is a reason to know which half you are
in when time runs short, which it will.

## 0B. What already exists

Greenfield repository, but not greenfield experience. `../ReconSync` in the same projects directory
already solves several of these problems in a shipped codebase:

| ComplyLayer needs | ReconSync already has |
|---|---|
| Dashboard auth: users, revocable sessions, TOTP with QR enrolment, recovery codes, roles on every route | Built, commit `a377c6e` |
| Append-only audit serialised per tenant | ADR-0007, built |
| Tenant isolation as a blocking test gate | `make test-isolation` |
| Compliance export as CSV and PDF, with spreadsheet-formula defusing | Built, commits `0a42753`, `3bdff88` |
| Client libraries in three languages verified against server-produced fixtures | Built, commit `ca4a6c9` |
| Per-tenant rate limiting so one tenant cannot starve others | Built, commit `4e45ea0` |

Different language, same shapes. The design decisions transfer even though the code does not, and
the CSV formula-injection defence in particular is a bug you have already found once and should not
find again.

## 0C. Dream state

```
CURRENT          Nothing. A specification and a plan.

THIS PLAN        A compliance officer edits a threshold and it is live in 30 seconds,
                 audited, reversible, and backtested before she committed to it.
                 An engineer added one import.

12-MONTH IDEAL   The rule library is the product: templates that arrive already mapped
                 to the regulation, updated when the regulation changes, so a customer
                 inherits compliance knowledge rather than a rules engine. The engine
                 is the delivery mechanism for that library.
```

**Delta.** The plan builds the engine well and treats templates as a phase-6 checkbox (B5). If the
12-month ideal is the library, then the template library is a first-class artefact with its own
maintenance story, not a seed fixture. Nothing to change in v1; worth writing down so it is not a
surprise later.

## 0C-bis. Implementation alternatives

| Approach | Effort (human / CC) | Risk | Verdict |
|---|---|---|---|
| **A. Embedded library first, HTTP service second** | 6 wk / ~4 days | Lower — no availability story to sell, no hosting to run | Recommended (C1) |
| B. Hosted API first, as specified | 8 wk / ~6 days | Higher — every customer conversation includes "what if you are down" | As written |
| C. Both in parallel | 10 wk / ~8 days | Highest — two integration surfaces, two failure models, before one customer | Rejected (P3, pragmatic) |

## 0E. Temporal interrogation

- **Hour 1:** `pip install`, add to `INSTALLED_APPS`, `migrate`, `complylayer_init`. First decision
  served against a seeded template. This hour must work perfectly; it is the whole first impression.
- **Hour 6:** the engineer has wired `evaluate()` into the transfer path behind a feature flag and is
  asking what happens on a Redis timeout. The answer must be in the README, not in a support thread.
- **Day 2:** compliance logs into the dashboard, opens a template, changes a number, and wants to
  know what it would have done last month. Backtesting is a day-2 feature, not a phase-7 feature, for
  the person the product is sold to.
- **Month 6:** a regulator asks what controls were in force in March. This is the moment the entire
  reproducibility design pays for itself, and it is the only moment. Everything in §3.4 exists for
  this question.

## Error and rescue registry

| What goes wrong | Who notices | Rescue path | Status |
|---|---|---|---|
| Rule expression rejected on save | Compliance officer | Error names the problem and the fix in her vocabulary | **Gap** — see D4/X3 |
| Rule activated, block rate spikes | Risk manager | Block-rate anomaly alert (§11.4), move to shadow — an audited action | Covered |
| Rule activated, was wrong, money was blocked | Customers, then support | No stated path. Rules are versioned but there is no "revert to version N" verb in §7.2 | **Gap — G1** |
| ComplyLayer unreachable | Engineer | Configured fallback per severity, degraded decisions recorded | Covered |
| Redis unreachable | Everyone | Velocity rules cannot evaluate; fail-closed halts transactions | Covered but see C2 |
| Backtest says a rule would have blocked 40% of transactions | Compliance officer | Impact report before activation | Covered — this is the best feature in the product |
| Pod serving stale rules | Nobody, silently | Version-skew page (§11.2) | Covered, and rightly the alert the spec is proudest of |

**G1 — there is no rollback verb.** `POST /v1/rules/{id}/activate` and `/archive` exist. Reverting to
a previous version means recreating it by hand, under time pressure, during an incident, by a person
who is not an engineer. Add `POST /v1/rules/{id}/revert {"to_version": N}` which creates a new
version whose content equals version N, so the audit trail stays append-only and the action is one
click. Auto-decided: add to phase 5.

---

# Phase 2 — Design review (UI scope detected)

Rated per dimension, 0–10, where 10 means the plan specifies it and 3 means the implementer will
have to invent it.

| Dimension | Score | Note |
|---|---|---|
| Information hierarchy | 5 | Nine dashboard sections listed; no statement of what a compliance officer sees first on login |
| Interaction states | **2** | See D2 — this is the largest design gap |
| User journey | 6 | The rule-authoring arc is clear; the review-queue arc is not |
| Specificity | 4 | "Visual builder for common patterns" is a category, not a design |
| Design system | 0 | Nothing decided. Correct for now, but it gates phase 6 |
| Accessibility | 6 | WCAG AA stated; no keyboard story for the repetitive screens |
| Error presentation | **2** | See D4 |

### D1 — The rule representation is a phase-1 decision wearing a phase-6 costume

The roadmap designs the UI in phase 6 and freezes `ALLOWED_FUNCTIONS` in phase 1. That ordering
means the visual builder must be built on whatever grammar the DSL happened to get, and the escape
hatch — the expression editor — becomes the place where anything non-trivial lives. If the expression
editor is where the real rules live, the engineer is back in the loop and the product's central
premise is false.

**Auto-decided (P1 completeness, P5 explicit):** run `/design-consultation` on the rule builder
*before* phase 2 freezes the function set, and treat "every §4.4 example is expressible in the visual
builder" as a phase-1 exit criterion rather than a phase-6 one.

### D2 — Missing states

Phase 6 lists views. It does not list states, and states are where compliance software gets
uncomfortable:

- A rule pending approval, viewed **by its author** — read-only, or editable? If editable, does an
  edit reset the approval? (It must, or the approval means nothing.)
- Backtest **running** — this is a Celery job over 30 days of decisions. Progress, cancel, and what
  happens if the user navigates away.
- Shadow divergence with **zero divergence** — the most common state, and the one that tells the
  officer the rule is safe to activate. It deserves to be the most reassuring screen in the product.
- Review queue **empty** — the goal state, not an error state.
- Review queue at **500+** — this is exactly the alert threshold in §11.4. The queue UI must be at
  its best precisely when it is at its worst.
- **Degraded mode banner** — when decisions are being served with a fallback, every screen showing
  decision data is showing incomplete data and must say so.

### D3 — The approval diff deserves to be its own deliverable

It gets one line: "pending rule changes with before/after diff". It is the screen where a risk
manager either catches a weakened control or does not, and a naive text diff of
`amount_minor > 5_000_000` → `amount_minor > 50_000_000` is a one-character change that moves a limit
by 10×.

Minimum: semantic diff that renders a changed threshold loudly and in currency units, the regulatory
reference the rule claims to implement, the backtest impact of the change if one has been run, who
requested it and their stated reason. Auto-decided into phase 5/6 deliverables.

### D4 — DSL error messages are a design problem that phase 1 owns

`"'Attribute' is not permitted in rules"` is a correct message for the person who wrote the
validator and useless to the person the product is for. Every `RuleSyntaxError` needs a
compliance-facing rendering: what is wrong, why the rule cannot do that, and what to write instead.

```
Not:  'Attribute' is not permitted in rules

But:  Rules cannot use a dot (.). You wrote customer.kyc_tier — write kyc_tier on
      its own. Dots are blocked because they are the main way a rule could break
      out of its sandbox. See the function reference for what is available.
```

Auto-decided: the error catalogue is a phase-1 deliverable, alongside the validator that raises it.

---

# Phase 3 — Engineering review

The findings are ordered by consequence. E1, E2 and E3 are, in my judgement, the three things most
likely to hurt.

## E1 — Nothing writes the velocity counters (critical)

§4.5 shows `gather_velocity`, which reads. No part of the specification says when a transaction is
*added* to the sorted set, and the SDK example in §6.4 shows exactly one call:

```js
const decision = await comply.check({...});
if (decision.outcome === 'block') throw ...;
return ledger.transfer(txn);          // <- nothing tells ComplyLayer this happened
```

If the decision call writes the counter, blocked and failed transactions inflate every window. If it
does not, the counters stay empty and every velocity rule — the entire Epic C — evaluates against
nothing and silently never fires. That second failure is the dangerous one: it looks exactly like a
system with no suspicious activity.

**Options.**

| | Behaviour | Cost |
|---|---|---|
| A | Count at decision time, including blocked attempts | One call. Windows count *attempts*. |
| B | Require `POST /v1/decisions/{id}/confirm` after the transaction commits | Two calls on the critical path — a material change to the integration promise |
| C | Ingest from the customer's ledger asynchronously | Third integration, out of scope for v1 |

**Auto-decided: A, with B available as an opt-in.** Counting attempts is not a compromise — for AML
it is the more correct semantics. Somebody who attempts eleven transfers just under the reporting
threshold and has six of them declined has still exhibited structuring, and a window that counts only
settled transactions would miss it. This needs to be stated as a deliberate semantic in the docs,
because a customer reconciling ComplyLayer's counts against their ledger will otherwise file a bug.

The specification's choice of *transaction id as the sorted-set member* already makes the write
idempotent under retry. That is load-bearing rather than incidental and should be commented as such.

## E2 — The retention promise and the throughput target cannot both be true (critical)

Two numbers from the specification, multiplied:

- §3.4: 2,000 decisions/sec per region
- §11.7: decisions retained 7 years
- §5: `Decision.context` is a JSONB copy of the full input

2,000/sec is 172.8 million rows per day. At roughly 2 KB per row with the context, that is ~345 GB
per day, ~126 TB per year, and the 7-year retention promise lands near a petabyte — in a Postgres
table with no partitioning declared anywhere in the data model, which also serves the analytics and
review-queue queries.

**Auto-decided (P1):**
- `Decision` is declaratively partitioned by month from the first migration. Retrofitting partitioning
  onto a live table of this size is a project, not a change.
- Retention is tiered: 90 days hot in Postgres, then Parquet in object storage for the remainder of
  the 7 years, queried through the reports path. §11.7 already replicates decisions to object storage
  with a write-once lock — this makes that copy the system of record for cold data rather than a
  second copy of the same thing.
- The roadmap's phase 7 export work reads the cold tier, not the hot one.

Realistically no single early customer does 2,000/sec sustained. That is exactly why this must be
decided now: the design is cheap today and impossible later.

## E3 — Decisions are not actually reproducible as specified (critical)

§3.4 requires 100% reproducibility, and the mechanism is the frozen `RuleSetVersion` snapshot. Two
holes:

**E3a — resolved facts are not stored.** `Decision.context` holds "the full input, so it can be
replayed". But a velocity rule's outcome depends on the Redis window at decision time, and Redis
holds a 30-day rolling window that has since moved on. Replaying a 90-day-old decision re-gathers
facts that no longer exist. The replay endpoint (`POST /v1/decisions/{id}/replay`) would silently
produce a different answer and present it as a reproduction.

*Fix (auto-decided):* store the **resolved fact set** — every velocity count, every aggregate, as
evaluated — alongside the input in `Decision.context`. Replay uses the stored facts. This is a data
model change and it belongs in phase 2, not later.

*Honest limitation, to be documented:* a backtest of a *new* rule needing a window that was never
gathered can only be approximated, because that fact was never recorded. Backtesting is exact for
rules over already-gathered facts and approximate otherwise. Say so in the UI rather than letting a
compliance officer over-trust a number.

**E3b — `high_risk_countries` is outside the snapshot.** §4.4's corridor rule reads
`in_list(destination_country, high_risk_countries)`. That list is tenant-configured mutable data. If
it lives outside `RuleSetVersion.rules_snapshot`, then editing the list changes decisions without
changing the rule set version, and two decisions with the same recorded version are no longer the
same control. *Fix (auto-decided):* named lists are versioned into the snapshot. Editing a list
publishes a new rule set version, exactly as editing a rule does.

**E3c — "byte-identical output" is not achievable and the test will lie.** The response contains
`decision_id`, `decided_at` and `latency_ms`. None is deterministic. A determinism test asserting
byte equality either fails always or excludes those fields quietly. *Fix (auto-decided):* restate
§3.4's requirement precisely — for the same input and rule set version, `outcome`, `matched_rules`,
`shadow_matches`, `reason` and `ruleset_version` are identical. And `matched_rules` must be ordered
by (priority, rule_id), not by whatever order evaluation produced, or the ordering is a set-iteration
accident.

## E4 — The in-process cache and the process model

Gunicorn and uvicorn run multiple worker **processes**. Each has its own memory, its own cache, and
must have its own pub/sub subscription.

- `complylayer_ruleset_version` labelled per pod hides skew *inside* a pod. Label it per worker.
- With `--preload`, a Redis connection created before fork is shared across children — a classic and
  confusing bug. The cache build and the pub/sub subscription must happen in a post-fork hook.
- "Atomic swap" means rebinding a module-level name to a new immutable object. Fine in CPython;
  worth a comment, because the next person will reach for mutating the dict in place.
- The 30-second polling backstop already in the plan matters more than pub/sub here, since a
  per-worker subscription is one more thing that can silently die.

Auto-decided into phase 4.

## E5 — The CI latency gate will be disabled within a month unless it is split

Phase 4 asserts p99 < 100 ms in CI. Shared GitHub runners are noisy neighbours; a p99 assertion on
one will flake, and a flaky blocking gate gets commented out — which is a worse outcome than not
having it.

*Auto-decided:* split it.
- **In CI, blocking:** the evaluation stage only. Pure CPU, no I/O, stable across runners. Assert a
  strict bound on eval time per rule and on total eval for 100 rules.
- **Nightly, on a dedicated runner:** end-to-end p99 against real Postgres and Redis, with the result
  tracked over time so a regression shows as a trend rather than a single red build.

## E6 — Smaller findings

| # | Finding | Decision |
|---|---|---|
| E6a | Argon2id key verification cached 60 s means a revoked key stays live up to 60 s. | Revocation publishes on the existing pub/sub channel; the TTL is the backstop, not the mechanism. |
| E6b | `unique_together('tenant','idempotency_key')` does not constrain NULLs, so A4's guarantee silently does not apply when the header is absent. | Make `Idempotency-Key` required on `POST /v1/decisions`. A compliance decision without a retry story is not one you want. |
| E6c | Test-mode keys (`cl_test_`) have no stated semantics for velocity state. | Test keys write to a separate velocity keyspace and a separate decision partition. Otherwise an integration test poisons production windows. |
| E6d | No API deprecation policy for a product whose pitch is stability. | Document one in phase 8: minimum 12 months notice, `Sunset` header. |
| E6e | Backtests replay decisions — make sure they do not bill, since billing is per decision served. | Backtest decisions are marked and excluded from the billing count. |
| E6f | The escape corpus in a public repo. | Non-issue — every escape in it is already published. Noted so it does not get raised as one later. |
| E6g | Phase 8's k6 run at 2,000/sec is not a CI-runner-scale test. | It runs against a deployed staging environment, on demand, not in the PR pipeline. |

## Architecture

```
                    customer transaction service
                              |
                    [ embedded evaluate() ]  or  [ HTTPS POST /v1/decisions ]
                              |
     +------------------------+------------------------+
     |                  decision workload               |
     |  validate -> key cache -> ruleset cache (compiled, versioned)
     |                             |                    |
     |                        evaluator (no I/O)        |
     |                             |                    |
     |            +----------------+----------------+   |
     |            |  ONE Redis pipeline:            |   |
     |            |  idempotency probe              |   |
     |            |  velocity trims + ranges        |   |
     |            |  amount hashes, aggregates      |   |
     |            +----------------+----------------+   |
     |                             |                    |
     |         local append-only segment (fsync batched)|
     +------------------------+------------------------+
                              | drainer (async)
                        Postgres (partitioned monthly)
                              |            \
                     read replica           object storage (cold, write-once)
                              |
     +------------------------+------------------------+
     |               management workload                |
     |  DRF: rules, approvals, backtest, review, reports|
     |  parser + validator live HERE ONLY (D5)          |
     +--------------------------------------------------+
                              | pub/sub: new version, key revocation
                              +--> every decision worker (per-worker cache)
```

---

# Phase 3.5 — Developer experience review

| Dimension | Score | Target |
|---|---|---|
| Time to hello world | 4 | Claimed under 10 min, never measured |
| API/CLI naming | 8 | `check`, `evaluate`, `outcome` — guessable |
| Error messages | 2 | See D4; the DSL errors are the product's error surface |
| Docs findability | 5 | OpenAPI promised, scheduled last |
| Upgrade path | 3 | No deprecation policy |
| Escape hatches | 9 | Self-host, embedded, custom functions — genuinely good |
| Opinionated defaults | 10 | See X1 |
| Dev environment | 6 | `doctor` and `benchmark` exist but land in phase 8 |

### X1 — Keep this exactly as it is

```js
fallback: 'allow',     // REQUIRED. There is no default.
```

A required constructor argument with no default, forcing an integration-time decision about
behaviour during an outage, is the best single API design decision in the specification. Most systems
have an accidental answer to that question that nobody has stated out loud. Do not let anyone add a
default during phase 8 "polish".

### X2 — `doctor` and `benchmark` belong early, not in packaging

`complylayer_doctor` (DB grants, audit trigger, Redis latency, clock skew) and
`complylayer_benchmark` (local p99 against synthetic load) are the two commands that tell a
self-hosting customer *at install time* that their Redis is in another availability zone and they
will not meet the SLA. That is the difference between discovering it during installation and
discovering it during an incident.

They are listed under §6.2 installation but scheduled implicitly in phase 8. *Auto-decided:*
`doctor` lands in phase 0 and grows a check per phase — each new failure mode adds its own
preflight. `benchmark` lands in phase 4 with the latency work.

### X3 — TTHW has no gate

"First decision served in under ten minutes" (§6.1) is a claim with nothing enforcing it.
*Auto-decided:* a `scripts/hello-world.sh` that goes from empty database to a served decision using
only seeded templates, run in CI, with its step count asserted. If a phase adds a step, the test
fails and somebody has to justify it.

### X4 — Three SDKs before one customer

Node, Python and Go clients are scheduled for phase 8. The Python client largely duplicates the
embedded path. *Recommendation:* Node only for v1, plus a complete `openapi.yaml` so anyone can
generate the rest. This changes stated scope, so it goes to the gate rather than being auto-decided.

### X5 — `openapi.yaml` is a phase 8 deliverable in the roadmap and a per-endpoint item in the
Definition of Done. The DoD is right. *Auto-decided:* the roadmap follows the DoD — the spec file
grows with each endpoint, and phase 8 only publishes it.

---

# Cross-phase themes

**Theme 1 — the plan is strongest where it is most technical and thinnest where it meets a person.**
The sandbox, the latency budget and the reproducibility design are excellent. The DSL error messages
(D4), the rule-builder representation (D1), the missing UI states (D2) and the approval diff (D3) are
all under-specified, and all four are the surface the product is actually sold on. Flagged
independently by the design and DX passes.

**Theme 2 — availability, not latency, is the binding constraint.** Raised in CEO (C2), engineering
(E1's fail-closed interaction, the Redis dependency) and DX (X1's fallback argument). Three passes
arrived at it separately, which is the strongest signal in this report.

**Theme 3 — two stated numbers contradict each other and both look authoritative.** 2,000/sec and
7-year retention (E2); 100% byte-identical reproducibility and a response containing a measured
latency (E3c). A plan this detailed earns trust, and that trust means nobody re-checks the
arithmetic. Worth a habit: any two numbers in the same document get multiplied once.

---

# Decision audit trail

| # | Phase | Decision | Class | Principle | Rationale |
|---|---|---|---|---|---|
| 1 | CEO | Add `POST /v1/rules/{id}/revert` to phase 5 | Mechanical | P1 completeness | No rollback verb existed; recreating a rule by hand during an incident is the wrong time to find that out |
| 2 | Design | `/design-consultation` runs before phase 2, not phase 6 | Mechanical | P1, P5 | The function set and the builder's expressiveness are one decision, not two |
| 3 | Design | Error catalogue is a phase 1 deliverable | Mechanical | P1 | The errors are the product's main error surface |
| 4 | Design | Approval diff promoted to its own deliverable with semantic threshold rendering | Mechanical | P1 | It is the screen where separation of duties is real or theatre |
| 5 | Design | Six missing states added to phase 6 | Mechanical | P1 | Listed views, not states |
| 6 | Eng | Velocity counts at decision time, including blocked attempts; confirm call opt-in | Mechanical | P5 explicit | Nothing wrote the counters; counting attempts is the correct AML semantic |
| 7 | Eng | `Decision` partitioned monthly from the first migration | Mechanical | P1 | Retrofitting partitioning at scale is a project |
| 8 | Eng | Retention tiered: 90 days hot, object storage cold | Mechanical | P1 | 2,000/sec × 7 years is not a Postgres table |
| 9 | Eng | Resolved facts stored with each decision | Mechanical | P1 | Replay was not reproducible; the input alone is insufficient |
| 10 | Eng | Named lists versioned into the rule set snapshot | Mechanical | P1 | Mutable data outside the snapshot breaks reproducibility |
| 11 | Eng | §3.4 restated field-by-field; `matched_rules` ordered by (priority, id) | Mechanical | P5 | "Byte-identical" was unachievable and the test would have hidden it |
| 12 | Eng | Per-worker version gauge; cache and pub/sub built post-fork | Mechanical | P5 | Per-pod labelling hides in-pod skew |
| 13 | Eng | Latency gate split: eval-stage blocking in CI, end-to-end nightly on dedicated hardware | Mechanical | P3 pragmatic | A flaky blocking gate gets deleted, which is worse than no gate |
| 14 | Eng | `Idempotency-Key` required | Mechanical | P5 | The unique constraint does not constrain NULLs |
| 15 | Eng | Test keys get a separate velocity keyspace | Mechanical | P1 | Otherwise integration tests poison production windows |
| 16 | Eng | Backtest decisions excluded from billing | Mechanical | P3 | Billing metric is decisions served |
| 17 | DX | `doctor` in phase 0, growing a check per phase; `benchmark` in phase 4 | Mechanical | P1 | Preflight is worthless if it arrives after installation |
| 18 | DX | `scripts/hello-world.sh` asserted in CI | Mechanical | P1 | The 10-minute claim had nothing enforcing it |
| 19 | DX | `openapi.yaml` grows per endpoint; phase 8 only publishes | Mechanical | P5 | The Definition of Done already said so |
| 20 | Eng | Key revocation published on pub/sub; 60 s TTL is the backstop | Mechanical | P1 | A 60-second window on a leaked key is avoidable |

**Surfaced to the user, not auto-decided:** C1 (distribution scope), C4 (portfolio vs commercial
phase ordering), X4 (three SDKs vs one). These change stated scope, so they went to the gate.

## Gate outcome

| # | Question | Decision |
|---|---|---|
| 21 | C1 — distribution scope for v1 | **Embedded + docker first.** `pip install complylayer` leads; hosted, Helm and Terraform defer to v1.1 |
| 22 | C4 — phase ordering | **Phases 0–4 marked a shippable milestone.** Nothing cut; the roadmap now says which half is which |
| 23 | X4 — SDK count | **Node only, plus a complete `openapi.yaml`.** Python and Go follow demand |

E1's option B (the opt-in confirm call) was kept as designed — counting attempts is the default,
`POST /v1/decisions/{id}/confirm` is available for tenants who need settled-only counts.

All 23 decisions are applied to `docs/ROADMAP.md` and `docs/plan-architecture.md`.

**Status: DONE_WITH_CONCERNS.** Two concerns worth carrying forward:

1. **Single-reviewer mode.** No Codex second voice ran. E1, E2 and E3 are the kind of finding a
   second reviewer would either confirm or dismantle, and confirmation is worth having before phase 2
   commits the data model. Install `codex` and re-run, or spawn independent review voices.
2. **The availability story is still one row in a table.** C2 was raised and accepted but not
   designed. Embedded-first (C1) removes most of it, since ComplyLayer's availability becomes the
   customer's own — but the Redis dependency for velocity rules survives that change, and a Redis
   outage with fail-closed block rules still halts money movement. This needs a written answer before
   phase 3 ships velocity.
