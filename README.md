# ComplyLayer

**Pluggable compliance rules and decision engine for fintechs.**

A decision API a fintech calls before finalising any transaction. It returns `allow`, `flag` or
`block` in under 100 milliseconds, based on rules the compliance officer writes and edits
themselves — no engineer, no pull request, no deploy.

> The person who owns the regulatory risk should be able to change the control without filing a
> ticket.

<!-- phase: 8 -->

## Status — phase 7 complete, phase 8 in progress

**A rule change is now safe to make.** Test a candidate rule against real history before activating
it, watch it in shadow on live traffic without affecting anyone, and see which of your rules are
spending a reviewer's week without catching anything. What is left is getting it out of the
door: load and chaos testing, the SDK, packaging and deploy.

| Phase | | What it delivers |
|---|---|---|
| 0 | ✅ | Foundations: tooling, CI gates, `doctor`, hello-world script |
| 1 | ✅ | The AST sandbox, escape corpus written first |
| 2 | ✅ | Interpreter, determinism, the decision endpoint |
| 3 | ✅ | Velocity counters and aggregate facts |
| 4 | ✅ | Rule cache, versioning, the latency contract |
| 5 | ✅ | Management API, approval workflow, tenancy |
| 6 | ✅ | The dashboard and rule builder |
| 7 | ✅ | Backtest, shadow mode, review queue, analytics |
| 8 | ⬜ | Load, chaos, packaging, release |

A phase is ticked when its exit gate in [`docs/ROADMAP.md`](docs/ROADMAP.md) is green in CI, not when
it feels finished. Phases 0–4 are a shippable milestone: they deliver the sandbox, the itemised
latency budget, and reproducible decisions.

## What works today

- **A backtest that says what it does not know.** A rule over facts that were recorded gets an
  exact answer. A rule needing a fact nobody recorded gets no number at all and a pointer to shadow
  mode, because an approximate figure on the screen where somebody decides whether to loosen a
  control is worse than an honest blank. The caveat travels with the number rather than sitting in
  a footnote.
- **Rule analytics that name the rule drowning your queue.** False-positive rate derived from
  recorded review outcomes, worst first: *"Fired 400 times and 95% were cleared on review. This rule
  is spending reviewer time rather than catching anything."* A rule nobody has reviewed reports an
  unknown rate rather than a flattering zero.
- **A rule builder a compliance officer can actually use.** Six shapes, each asking a question
  rather than naming a construct — *"Is this customer splitting transactions to sit under a
  reporting threshold?"* — with the regulation prefilled and the expression writing itself as you
  answer. Every §4.4 rule is reachable through it without touching the editor, which is
  [asserted on every commit](tests/test_dashboard.py).
- **An approval diff that shows a limit moving, not a character changing.** `₦50,000.00 →
  ₦500,000.00`, `10× higher`, the regulation it claims, and what it would have done to last month's
  transactions. Approve sits alone and unlocks only once the impact figure has been on screen.
- **Separation of duties that is real rather than documented.** Nobody approves their own change,
  whatever their role. Editing a rule after approval clears the approval — otherwise the workflow is
  theatre. An engineer cannot create or activate a compliance rule at all, which is the point of the
  product rather than an oversight in a permission table.
- **An audit trail that is evidence, not a log.** Hash-chained per tenant, append-only by database
  trigger — an UPDATE fails for a superuser too — and corrections are appended rather than applied.
- **Tenant isolation, three independent layers.** A key resolves to one tenant, the query layer
  scopes every read, and row level security returns nothing if either is ever bypassed. Cross-tenant
  reads return **404, never 403**: a 403 would confirm the object exists.
- **A versioned rule cache with no database on the decision path.** Each worker compiles the rule
  set into memory and swaps it atomically. Propagation does not depend on pub/sub staying connected:
  there is a poll as well, and [the test that matters](tests/test_ruleset_cache.py) severs the
  subscription entirely and requires the change to land anyway. A worker reports `readyz` only once
  its cache is warm, so a deploy does not make every early request pay for the compile.
- **The latency contract, measured.** 100 rules evaluate at **p99 0.245 ms** against a 5 ms stage
  budget and a 100 ms contract. The gate is split: the evaluation stage blocks CI because it is
  stable on a shared runner, and end-to-end runs nightly on real hardware — a flaky blocking gate is
  one somebody deletes.
- **Velocity rules that hold under concurrency.** A customer's rolling windows and their lifetime
  aggregates come out of Redis in one round trip, and the write is part of it — so a hundred
  simultaneous transactions against a "no more than five an hour" rule let exactly five through,
  every time. Windows count *attempts*, blocked ones included, because eleven transfers just under
  the reporting threshold with six declined is still structuring.
- **The decision endpoint.** `POST /v1/decisions` evaluates a versioned rule set and answers in
  under half a millisecond for 100 rules. Idempotent: a retry returns the original decision
  verbatim, timestamp included. Unknown fields are refused rather than ignored, so a payload
  carrying a PAN does not get as far as being stored and redacted.
- **Decisions that reproduce.** Same input and rule set version, same outcome — asserted across a
  thousand evaluations and two separate processes. The three fields that legitimately differ
  (`decision_id`, `decided_at`, `latency_ms`) are [named in the test](tests/test_determinism.py),
  because a determinism test with a silent exclusion reports a guarantee nobody is checking.
- **A failure mode that is a decision, not an accident.** A rule that cannot evaluate — missing fact,
  Redis gone — is not a rule that did not match. `block` fails closed, `flag` fails open, and every
  such decision is recorded as degraded so a sustained rate is an incident rather than a blip.
- **The rule language.** A rule is parsed, checked against an allowlist of permitted syntax, and
  either accepted or rejected. All six of the specification's §4.4 example rules validate; 94
  published sandbox escapes do not, and [the corpus](tests/test_dsl_escapes.py) proves it on every
  commit. 100,000 fuzzed expressions produce nothing but clean rejections.
- **Errors written for the person who has to fix them.** `customer.kyc_tier > 2` does not return
  `'Attribute' is not permitted in rules`. It returns: *Rules cannot use a dot (.). You wrote
  customer.kyc_tier. Facts are available by name on their own — write kyc_tier, not
  customer.kyc_tier.* A mistyped `velocity_cout` suggests `velocity_count`.
- **The visual builder's grammar**, settled alongside the function set rather than after it. Every
  §4.4 rule is expressible as builder JSON, and what the builder emits goes through exactly the same
  validator as a hand-typed rule — a built rule earns no trust a typed one would not.
- **A four-step install**, counted and enforced. `scripts/hello-world.sh` runs every step a new user
  runs and fails if the count exceeds its budget, so §6.1's ten-minute promise cannot drift a
  convenient step at a time.
- **`complylayer_doctor`** — a preflight for the failure modes that are silent. It checks the Python
  version, that Postgres is 16 or newer, that Redis is close enough to fit the latency budget, and
  that this host's clock agrees with Redis. That last one matters more than it looks: velocity
  windows are trimmed by timestamp, so a drifting clock evaluates the wrong window and never errors.
- **The `eval`/`exec` guard** from [ADR-0001](docs/adr/0001-ast-interpreter-not-eval.md), in CI and
  in a pre-commit hook. It tokenises rather than greps, so it catches a real call and ignores the
  same word in a comment or a docstring — the DSL validator will document why `eval` is forbidden,
  and a gate that fails on its own rationale is a gate somebody disables.
- **CI**: five jobs, sandbox checks first. Lint, format, the 90% coverage gate (currently 100%),
  `bandit`, `semgrep`, `pip-audit`, `gitleaks`, and a check that the README has not fallen behind.

## Try it

```bash
git clone https://github.com/nobledeveloper01/ComplyLayer && cd ComplyLayer
cp .env.example .env          # only needed if 5432 or 6379 are taken on your machine
./scripts/hello-world.sh
```

That installs Python 3.12 and every dependency, starts Postgres and Redis, creates the schema, and
preflights the deployment. A healthy run ends like this:

```
[  ok  ] python version  running 3.12, expected 3.12
[  ok  ] database        Postgres 16 (need 16+)
[  ok  ] redis           round trip 0.25 ms
[  ok  ] clock skew      0.002 s between this host and Redis
```

To serve a decision:

```bash
uv run python -c "
from complylayer.api.handler import DecisionHandler
from complylayer.api.store import InMemoryStore
from complylayer.api.validation import parse_transaction
from complylayer.dsl import validate_source
from complylayer.engine import CompiledRule, RuleSet, Severity
import json

rules = RuleSet(47, (CompiledRule('rul_kyc_t2', 'Tier 2 single transaction limit',
    validate_source('amount_minor > 50_000_000'), Severity.BLOCK, priority=10,
    regulatory_reference='CBN KYC Tier 2',
    customer_message='This transfer is above your tier 2 limit.'),))

handler = DecisionHandler('tnt_demo', rules, InMemoryStore())
txn = parse_transaction({'transaction_ref': 'TXN-1', 'customer_ref': 'usr_9931',
    'amount_minor': 75000000, 'currency': 'NGN', 'customer': {'kyc_tier': 2}})
body = handler.decide(txn, 'TXN-1')
print(json.dumps({k: v for k, v in body.items() if not k.startswith('_')}, indent=2))
"
```

To see the sandbox reject something, and say why:

```bash
uv run python -c "
from complylayer.dsl import validate_source, RuleSyntaxError
for rule in ['amount_minor > tier_daily_limit_minor', 'customer.kyc_tier > 2', \"().__class__.__bases__[0].__subclasses__()\"]:
    try:
        validate_source(rule); print(f'accepted: {rule}')
    except RuleSyntaxError as e:
        print(f'rejected: {rule}\n          {e}')
"
```

To run the gates yourself:

```bash
make ci
```

## Documents

| Document | What it is |
|---|---|
| [`docs/product-specification.md`](docs/product-specification.md) | The full product specification — business analysis, architecture, security, operations |
| [`docs/plan-architecture.md`](docs/plan-architecture.md) | The decisions the specification leaves open, resolved, with their costs stated |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Nine phases, each with a mechanically checkable exit gate |
| [`docs/plan-review-report.md`](docs/plan-review-report.md) | The plan review — 23 decisions, three of which contradicted the specification |
| [`docs/adr/0004-server-rendered-dashboard.md`](docs/adr/0004-server-rendered-dashboard.md) | Why the dashboard is Django templates rather than a React SPA |
| [`DESIGN.md`](DESIGN.md) | The design system — what the dashboard looks like and why |
| [`docs/openapi.yaml`](docs/openapi.yaml) | The API contract, tested against the URLconf so it cannot drift |
| [`docs/security-review-phase1.md`](docs/security-review-phase1.md) | The sandbox security review — 14 findings, none of them holes in the allowlist |
| [`docs/adr/`](docs/adr/) | Architecture decision records |

## The parts worth reading first

- **[ADR-0001](docs/adr/0001-ast-interpreter-not-eval.md)** — why rules are interpreted over a
  validated AST and never `eval`. This is the security core of the product; the obvious
  implementation is remote code execution offered as a feature.
- **[ADR-0002](docs/adr/0002-plain-django-on-the-decision-path.md)** — the decision endpoint skips
  DRF, and the benchmark that was supposed to justify that mostly undermines it. Hand-written
  validation costs 3 microseconds against a 5 millisecond budget, which means DRF would have to be
  1,700× slower to fail. Recorded as unproven rather than settled.
- **[The latency budget](docs/plan-architecture.md#latency-budget-restated-with-the-omissions-filled-in)**
  — 100 ms p99, itemised by stage, including the two costs the specification's own budget omitted.
- **[What went wrong in the plan](docs/plan-review-report.md#phase-3--engineering-review)** — nothing
  wrote the velocity counters, the retention promise and the throughput target could not both be
  true, and decisions were not reproducible as designed. All three are fixed; the reasoning is more
  useful than the fix.

## Licence

To be decided before the first public release.
