# Roadmap

Nine phases. Each has an **exit gate** that a machine checks — not a judgement call, not "looks
done". A phase is finished when its gate is green in CI and its review has been run. The gates
accumulate: every later phase must keep every earlier gate passing, which is what stops phase 7 from
quietly breaking the latency promise phase 4 established.

The specification's §12 delivery plan is eight weeks. This is that plan with the exit criteria made
explicit, plus a phase 0 the specification assumes rather than schedules.

## Two things settled before phase 0, because they change what gets built

**Distribution: the embedded library leads.** v1 ships `pip install complylayer` — evaluated
in-process, no network hop — plus the docker-compose self-host. The hosted product, the Helm chart
and Terraform defer to v1.1.

The reasoning is worth keeping visible. Tunde's first question in §3.1 is what happens when
ComplyLayer is down. The fail-open/fail-closed policy is a good answer to a question the customer
would rather not have to ask; `from complylayer import evaluate` deletes the question. No vendor
availability, no data leaving the customer's network, and the sub-100 ms claim becomes trivially true
rather than hard-won. Every distribution form in §6 still gets built — just not all at once, and not
in the order §6 lists them.

**Phases 0–4 are a shippable milestone.** They deliver all three things §13 says survive
follow-up questions: the AST sandbox, the itemised latency budget, and reproducible decisions.
Phases 5–8 deliver commercial surface — the dashboard, the SDK, packaging, deploy.

Nothing is cut. But when time runs short, and it will, stopping at the end of phase 4 leaves a
complete and demonstrable engine, while stopping in the middle of phase 6 leaves a half-built
dashboard. Knowing which half you are in is the point.

---

## Phase 0 — Foundations

Nothing about compliance. Only the machinery that every later gate depends on.

**Deliverables**
- `pyproject.toml`, Python 3.12, ruff, pytest, coverage, `pip-audit`, `bandit`, `semgrep`, `gitleaks`
- `Makefile` with the same targets CI runs, so local and CI cannot disagree
- `.github/workflows/ci.yml` with the five jobs of §11.8 wired but mostly empty
- Docker compose: Postgres 16, Redis, and nothing else yet
- Pre-commit hooks: format, lint, `gitleaks`
- `docs/adr/0001-ast-interpreter-not-eval.md` — written now, before the code it justifies
- `complylayer_doctor`, with the checks that exist today. It grows one check per phase — every new
  failure mode adds its own preflight, so a self-hosting customer finds out at install time rather
  than during an incident
- `scripts/hello-world.sh` — empty database to a served decision, asserted in CI with a step count.
  §6.1 claims under ten minutes; this is the thing that keeps the claim true when a later phase is
  tempted to add a step

**Exit gate**
- `make ci` is green from a clean checkout
- The `grep -rn "\beval(\|\bexec("` guard is in CI and demonstrably fails a branch that adds `eval(`
- Coverage gate is configured at 90% and enforced (trivially passing on an empty package is fine —
  the point is that the gate exists before there is pressure to lower it)

**Review:** `/devex-review` — this is the one moment when the setup experience can be judged
without anyone being invested in it.

---

## Phase 1 — The sandbox, escape corpus first

The escape suite is written **before** the validator exists. Every test fails at the start of the
phase. That ordering is the point: the security requirement becomes the specification rather than
something checked afterwards.

**Deliverables**
- `tests/test_dsl_escapes.py` — a corpus of published Python sandbox escapes: `__class__` /
  `__bases__` / `__subclasses__` chains, `__globals__` via function attributes, `__builtins__`
  recovery, format-string `{0.__class__}` tricks, comprehension scope leaks, generator frame access,
  decorator and lambda routes, `__import__`, `getattr` chains
- `tests/test_dsl_limits.py` — node ceiling, step budget, source length, bracket depth, unary runs
- `complylayer/dsl/parser.py`, `validator.py` — pre-parse guards (D5) then the AST allowlist
- `complylayer/dsl/errors.py` — **the error catalogue**, not a set of exception classes. Every
  rejection renders for a compliance officer: what is wrong, why the rule cannot do it, and what to
  write instead. `"'Attribute' is not permitted in rules"` is a correct message for the person who
  wrote the validator and useless to the person the product is for. These errors are the product's
  main error surface, so they are a phase 1 deliverable rather than phase 6 polish.
- Hypothesis-driven fuzzing job, seeded from the corpus

**Exit gate**
- Every corpus entry raises `RuleSyntaxError`. Not "returns false" — raises.
- The fuzzer runs 100,000 generated expressions without a single one reaching outside the allowlist
- `ast.Div` is absent from `ALLOWED_NODES` (D6), asserted by a test that reads the set
- Every §4.4 example rule is expressible in the grammar **and** in the visual builder's structured
  form. The function set and the builder's expressiveness are one decision, not two — if the builder
  cannot express a real rule, the expression editor becomes where the real rules live and the
  engineer is back in the loop
- Coverage of `complylayer/dsl/` is 100%. Not 90 — this module gets no exceptions.

**Review:** `/design-consultation` on the rule representation **before** the function set is frozen,
then `/cso` on the DSL module. The security core gets a dedicated pass rather than being folded into
a general review.

---

## Phase 2 — Interpreter, determinism, and the decision endpoint

**Deliverables**
- `complylayer/dsl/interpreter.py` — tree walk, step budget, names resolved only from the supplied
  fact context, no I/O reachable
- `complylayer/dsl/functions.py` — the eight allowed functions, integer arithmetic throughout
- `complylayer/engine/` — evaluation orchestration, priority ordering, first-match and
  all-match semantics
- Models: `Tenant`, `Rule`, `RuleSetVersion`, `Decision`, `AuditRecord`. `Decision` is **partitioned
  by month in this first migration** (D10) — retrofitting partitioning onto a table at the throughput
  target is a project, not a change
- `Decision.context` stores the input **and the resolved fact set** (D11), because the input alone
  cannot be replayed once Redis's rolling window has moved on
- `POST /v1/decisions`, the plain-Django path of D1, with idempotency per D2. `Idempotency-Key` is
  required — the unique constraint does not constrain NULLs, so an optional header means A4's
  guarantee silently does not apply
- `tests/test_determinism.py`

**Exit gate**
- Same context + same rule set version produces identical `outcome`, `matched_rules`,
  `shadow_matches`, `reason` and `ruleset_version` across 1,000 runs and two separate processes.
  Identity, timestamp and measured-latency fields are excluded **by name in the test**, per D11 —
  a determinism test with a quiet exclusion is worse than no test
- `matched_rules` is ordered by (priority, rule_id), asserted — otherwise the order is a
  set-iteration accident that stays stable until it doesn't
- Idempotent replay returns the original decision verbatim, including its original timestamp
- **The D1 benchmark:** the endpoint implemented with and without DRF, measured. The result decides
  whether the two-path split survives, and ADR-0002 is updated with the numbers either way.

**Review:** `/review` on the diff.

---

## Phase 3 — Velocity and facts

**Deliverables**
- `complylayer/velocity/` — Redis sorted sets, the single pipeline of §4.5, TTLs beyond the largest
  window, amount hashes for `velocity_sum` and amount-filtered counts
- `complylayer/facts/` — aggregate fact provider: lifetime volume, account age, prior flag count,
  average transaction size, days since last activity
- **The write path (D9)** — the decision call adds the transaction to the sorted set, so windows
  count attempts including blocked ones. The specification only ever showed the read; without this
  every velocity rule evaluates against an empty window and silently never fires, which looks exactly
  like a system observing no suspicious activity
- Optional `POST /v1/decisions/{id}/confirm` for tenants who need settled-only counts
- The boundary lock of D2, taken only inside the race window
- `tests/test_concurrency.py`

**Exit gate**
- 100 concurrent decisions for one customer against a `block` rule with threshold 5: exactly 5 are
  allowed. Run 50 times, zero variance.
- The same scenario against a `flag` rule documents its imprecision rather than asserting exactness
- Fact gathering is **one** Redis round trip. Asserted by counting calls on a wrapped client — this
  is the failure that arrives gradually and is invisible until it is a 3am page.

**Review:** `/review`.

---

## Phase 4 — Rule cache, versioning, and the latency contract

The phase that turns the latency promise into something CI defends.

**Deliverables**
- In-process compiled rule cache keyed by version, atomic swap
- Redis pub/sub version announcements, plus a 30-second polling backstop (§11.6 step 6 — pub/sub
  alone is not a guarantee, and the backstop is cheap)
- Warm start: a pod compiles its rule set before reporting ready
- Snapshot re-validation on load (D5 layer 3), including named lists now versioned into the snapshot
  (D11)
- `complylayer_ruleset_version` as a **per-worker** gauge and the skew alert. Per pod it would hide
  skew inside a pod, which is the failure the metric exists to catch (D12)
- Cache build and pub/sub subscribe in a post-fork hook (D12)
- `tests/test_latency_benchmark.py`, **split** — see the gate below
- `complylayer_benchmark` management command

**Exit gate**
- **Blocking in CI:** the evaluation stage alone — pure CPU, no I/O, stable on a shared runner —
  with a strict bound per rule and for 100 rules. A deliberately slow rule fails the build
- **Nightly on dedicated hardware:** end-to-end p99 under 100 ms with 100 active rules, tracked as a
  trend. A p99 assertion on a noisy shared runner flakes, and a flaky blocking gate gets commented
  out within a month — which is a worse outcome than not having one
- Per-stage histogram present for every stage in the budget — a p99 alert without a stage breakdown
  is undiagnosable, which is the whole reason the metric exists
- Rule activation propagates to every node in under 30 seconds, tested with a node whose pub/sub
  subscription is severed — the backstop must carry it
- A pod does not report ready before its cache is warm, asserted against the readiness endpoint

**Review:** `/benchmark` establishes the baseline that later phases are measured against, then
`/review`.

---

## Phase 5 — Management API, approval workflow, tenancy

**Deliverables**
- DRF management API: the full §7.2 surface
- Rule lifecycle: draft → shadow → pending approval → active → archived
- `POST /v1/rules/{id}/revert {"to_version": N}` — creates a new version whose content equals version
  N, so the audit trail stays append-only. Without it, undoing a bad rule means recreating it by
  hand, during an incident, by someone who is not an engineer
- Test-mode keys (`cl_test_`) write to a separate velocity keyspace and decision partition, so an
  integration test cannot poison a production window
- RBAC per §10.2, including the row that matters most — the engineer role cannot create or activate
  a rule
- Emergency override: written reason required, pages the risk lead
- Hash-chained audit trail, append-only grants, the `BEFORE UPDATE OR DELETE` trigger
- Postgres RLS per D4, non-owner app role, transaction-mode pooling
- `tests/test_tenant_isolation.py` — two tenants, every read endpoint returns **404** as the other
  tenant, never 403

**Exit gate**
- The isolation suite is a blocking CI gate and covers every endpoint that exists, enforced by a
  test that enumerates the URLconf and fails on any route the suite does not exercise
- An author cannot approve their own rule — asserted at the API, not only in the UI
- `UPDATE` and `DELETE` against the audit table fail as the superuser
- Chain verification detects a tampered record and names the first broken link

**Review:** `/cso` on the auth, tenancy and audit surfaces, then `/review`.

---

## Phase 6 — The dashboard

The rule builder is where the product's claim is either true or false. If a compliance officer
cannot write a velocity rule without help, the entire premise fails regardless of how good the
engine is.

**Deliverables**
- ~~Design system and component decisions settled *before* implementation~~ —
  done, in [`DESIGN.md`](../DESIGN.md). Colour is reserved for severity, headings
  are a serif, and the approval diff has its own section because treating it as
  "a diff view" is how it ends up as a text diff
- Rule builder: visual builder for the common patterns, expression editor for the rest, live
  validation with errors phrased for a non-engineer
- Template library annotated with regulatory references
- **The approval diff, as its own deliverable.** It is the screen where a risk manager either catches
  a weakened control or does not, and `amount_minor > 5_000_000` → `amount_minor > 50_000_000` is one
  character that moves a limit tenfold. Semantic diff rendering changed thresholds loudly and in
  currency units, the regulatory reference the rule claims, the backtest impact if one was run, and
  who requested it with their stated reason
- The six states the plan previously left to the implementer: a pending rule seen by its own author
  (editing resets the approval, or the approval means nothing); a backtest running, with progress and
  cancel; shadow divergence at zero, which is the most common state and should be the most reassuring
  screen in the product; the review queue empty, which is the goal state and not an error; the review
  queue past 500, which is exactly where §11.4 alerts and so is where the UI must be at its best; and
  the degraded-mode banner on every screen showing decision data during a fallback
- Auth: email + TOTP, OIDC

**Exit gate**
- A rule from each of the six §4.4 shapes can be built through the UI alone
- Validation errors name the problem and the fix, never a Python exception
- Every state above renders, tested
- Keyboard navigable, WCAG AA on contrast and focus order. The review queue is a repetitive-task
  screen and gets a keyboard path through clear/confirm without touching the mouse

**Review:** `/design-consultation` before any code, `/design-review` and `/qa` after.

---

## Phase 7 — Backtest, shadow, review queue, analytics

**Deliverables**
- Backtest against stored decision contexts, on a read replica, with an impact report. It is **exact**
  for rules over facts that were already gathered and **approximate** for a rule needing a window
  that was never recorded (D11), and the UI says which one it is showing — a compliance officer
  over-trusting an approximate number is worse than showing her the caveat
- Reports and exports read the cold object-storage tier (D10); the review queue and analytics read
  the hot 90 days
- Backtest decisions are marked and excluded from the billing count, since the billing metric is
  decisions served
- Shadow mode and divergence reporting
- Review queue with recorded outcomes feeding false-positive analytics
- Compliance export: rule set, complete change history, decision volumes, chain attestation
- Degraded-decision reconciliation — an outage must not leave a permanent hole in the review record

**Exit gate**
- A backtest over 30 days of decisions runs on the replica and touches the primary zero times,
  asserted by connection instrumentation
- A shadow rule never appears in a returned `outcome`, asserted across the whole decision suite
- Replaying a stored decision against its own rule set version reproduces it exactly — the same
  property phase 2 established, now proven through the replay endpoint

**Review:** `/qa`, then `/review`.

---

## Phase 8 — Load, chaos, packaging, release ✅

**Deliverables**
- k6 at 2,000 decisions/sec with 100 active rules, run against a deployed staging environment on
  demand — not in the pull-request pipeline, where no runner is that size
- An API deprecation policy: minimum twelve months notice, `Sunset` header. A product whose pitch is
  stability needs to have written one down
- Chaos: kill Redis and assert the configured fallback applies per severity with every degraded
  decision recorded; kill a pod mid-decision and assert idempotency returns the original
- **One SDK: Node**, verified against fixtures the server produced, plus a complete `openapi.yaml` so
  anyone can generate the rest. Python is largely redundant with the embedded path, and three SDKs is
  three release pipelines, three fixture sets and three deprecation stories before a customer has
  asked for any of them. Python and Go follow demand.
- PyPI packaging for `complylayer` and npm for `@complylayer/node`, released from one tag
- Docker compose self-host. The Helm chart follows in v1.1 with the hosted product, since it deploys
  workloads that v1 does not separate
- `openapi.yaml` **published**, not written — it grew one endpoint at a time from phase 5 onward,
  which is what the Definition of Done already required, and `tests/test_openapi.py` fails if it
  falls behind the URLconf. Runbooks, ADRs, README

**Exit gate**
- 2,000 decisions/sec sustained, p99 under 100 ms
- Redis killed mid-load: every resulting decision is marked `degraded`, the fallback matches the
  configured policy per severity, and the count reconciles exactly with the alert
- Restore from backup, replay a known set of decisions, assert identical outcomes
- SBOM published, image signed, `trivy` clean at HIGH and CRITICAL
- The Node SDK requires `fallback` with no default, per §6.4. A generated client cannot enforce that,
  so `openapi.yaml` marks the field required and the integration guide leads with it — an explicit
  choice at integration time beats a silent default a fintech learns the meaning of during an outage

**Review:** `/cso` full pass, `/qa-only` for an independent report, `/ship`, then
`/land-and-deploy` with `/canary` watching the rollout, and `/document-release` afterwards.

---

## Where each review runs

| Skill | When |
|---|---|
| `/plan-ceo-review`, `/plan-eng-review`, `/plan-design-review`, `/plan-devex-review` | Now, over this roadmap and the architecture plan |
| `/review` | Every phase, before merge |
| `/cso` | Phases 1, 5 and 8 — the DSL, the auth and tenancy surfaces, and the full pass |
| `/benchmark` | Phase 4 onward; a regression is a product defect here, not a nice-to-have |
| `/design-consultation`, `/design-review` | Phase 6, before and after |
| `/qa`, `/qa-only` | Phases 6, 7 and 8 |
| `/investigate` | Any latency regression — the stage histogram exists to make this fast |
| `/document-release` | **Every phase boundary**, to update the README — see below |
| `/ship`, `/land-and-deploy`, `/canary` | Phase 8 |

---

## Definition of Done

Per §12.1, and every item is checkable:

- [ ] Acceptance criteria met and demonstrated
- [ ] Unit tests written, coverage gate passed
- [ ] **If the DSL changed, the escape suite was extended**
- [ ] Latency benchmark still within budget
- [ ] Determinism test passes
- [ ] Tenant isolation covered for any new data path
- [ ] Structured logging with no sensitive fields
- [ ] Metrics emitted for the new path, including per-stage timing
- [ ] SAST and dependency scan clean
- [ ] Endpoint documented in `openapi.yaml`
- [ ] Migration reviewed separately and reversible
- [ ] Runbook updated if a new failure mode was introduced
- [ ] ADR written for any non-obvious decision
- [ ] **README updated** — see below

---

## The README is a phase deliverable

Every phase ends by updating `README.md`. Not as a courtesy to a future reader — as the one document
that has to stay honest about what the project actually does today, as opposed to what the roadmap
says it will do eventually.

A repository whose README describes the finished product while the code is four phases in is lying,
and the person it lies to most effectively is the author six weeks later.

**What gets updated at every phase boundary:**

| Section | Update |
|---|---|
| Status line | The phase just completed, and what that means a reader can do right now |
| Phase table | Tick the completed phase; nothing is ticked before its exit gate is green |
| What works today | Real capabilities, in plain terms. If a decision cannot yet be served, it does not appear here |
| Try it | The shortest real command sequence that demonstrates the phase's work. It must actually run — this section is copy-pasted more than it is read |
| The parts worth reading first | New ADRs, new design documents |

**Rules:**

- **Never describe unbuilt work in the present tense.** "ComplyLayer serves decisions in under 100 ms"
  is false until phase 4 measures it. Until then it is a target, and the README says so.
- **Every command in "Try it" is executed before the phase is called done.** A README command that
  fails is worse than no README, because it costs the reader their trust as well as their time.
- **The phase table is ticked from the exit gate, not from the intent.** A phase whose gate is amber
  is not ticked, however finished it feels.

**Mechanically enforced.** `scripts/check-readme-phase.sh` asserts that the phase declared in the
README matches the `PHASE` file at the repository root, and that every phase at or below it is
ticked. It runs in the `quality` CI job. Bumping `PHASE` without touching the README fails the build,
which is precisely the moment the update would otherwise get skipped.

**Review:** `/document-release` at each phase boundary, which is what it is for.
