# ComplyLayer

**Pluggable compliance rules and decision engine for fintechs.**

A decision API a fintech calls before finalising any transaction. It returns `allow`, `flag` or
`block` in under 100 milliseconds, based on rules the compliance officer writes and edits
themselves — no engineer, no pull request, no deploy.

> The person who owns the regulatory risk should be able to change the control without filing a
> ticket.

<!-- phase: 8 -->

## Status — all nine phases complete

**ComplyLayer decides — verified by running it, not only by testing it.** `POST /v1/decisions`
against a live server returns `block` in 20 ms with the reason, the regulation and the message
compliance wrote. Kill Redis and every `block` rule fails closed while every `flag` rule fails open,
each one recorded. It packages as a wheel, a container and a Node client, released from one tag.

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
| 8 | ✅ | Load, chaos, packaging, release |

A phase is ticked when its exit gate in [`docs/ROADMAP.md`](docs/ROADMAP.md) is green in CI, not when
it feels finished. Phases 0–4 are a shippable milestone: they deliver the sandbox, the itemised
latency budget, and reproducible decisions.

## What works today

- **Metrics that can actually see what they were built to see.** `complylayer_ruleset_version` is
  published to Redis per worker rather than held in process, because a Prometheus scrape reaches
  exactly one worker — so the gauge built to detect *disagreement between workers* could not see
  across them, and would have flickered between values and read as flapping. Verified under three
  gunicorn workers: every scrape sees every worker.
- **Wiring proved by use.** The decision endpoint is exercised end to end through the real
  middleware stack with nothing attached by hand ([the test](tests/test_decision_wiring.py)), because
  832 green tests once sat on top of an endpoint that raised `AttributeError` on its first real
  request — every one of them supplied the dependency nobody had built.
- **A documented failure mode that was executed rather than quoted.** The chaos suite kills Redis
  and asserts §10.3's table line by line — and found a real bug doing it: a Redis outage during
  *fact gathering* propagated out and would have returned a 500 instead of a degraded decision. That
  is the exact failure the fail-open/fail-closed design exists to replace.
- **Packaged three ways from one tag.** A wheel that carries its own dashboard (asserted in the
  release job), a non-root container on a read-only rootfs, and a Node SDK whose `fallback` argument
  is required with no default — because a silent default is one a fintech discovers during an
  outage.
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
- **Tenant isolation, three independent layers — and the third one now runs.** A key resolves to one
  tenant, the query layer scopes every read, and row level security returns nothing if either is
  ever bypassed. Cross-tenant reads return **404, never 403**: a 403 would confirm the object
  exists. Layer three took two goes to make real. The policies were inert first because the
  connecting role was a superuser, and Postgres exempts those unconditionally. Then, under the
  non-superuser role that fixes it, authentication stopped working entirely: the API key table was
  scoped by the tenant that reading it exists to *determine*, so the lookup returned nothing and
  every request answered 401. A deployment could have row level security or it could have
  authentication. [Migration 0008](complylayer/migrations/0008_api_key_resolution.py) gives key
  resolution its own narrow policy — one row, by unique prefix, inside a function that sets a flag
  for the length of one call — so ordinary queries stay scoped and the bootstrap works.
  [The test](tests/test_api_key_auth.py) runs as `complylayer_app` and checks both halves: the key
  resolves, and a plain `SELECT` on that table still sees nothing.
- **An API key that is the whole key.** The prefix is stored in the clear so a dashboard can show
  which key is which; it identifies a key and does not authenticate one. The Argon2id verification
  is cached for a minute against a digest of the *entire* presented key, and the row itself is read
  every request, so revoking a key stops it on its next call rather than whenever a cache happens to
  expire. Both properties are held down by the exploits that used to work against them.
- **A server that refuses to start on the secrets published in this repository.** `SECRET_KEY`
  signs the session cookie and the dashboard's second-factor flag lives inside it, so on the default
  key a forged cookie is a complete sign-in — with every health probe green throughout.
  `COMPLYLAYER_CUSTOMER_SALT` is the HMAC key pseudonymising customer references; it used to fall
  back to the tenant id, a column on every row it protects, which made §8.4's promise that "a stolen
  decisions table without the salt yields nothing" true of nobody. Both are now refused at boot by
  [server/boot.py](server/boot.py), reported by `complylayer_doctor`, and gated in CI by
  `manage.py check --deploy --fail-level WARNING` — which had never been run, and which is how the
  dashboard reached phase 8 with no clickjacking protection on the page whose main control is an
  Approve button.
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

**Server and dashboard: [BUSL-1.1](LICENSE)**, converting to Apache-2.0 on
2030-01-01. You may run it in production against your own organisation's
transactions, self-hosted, including as a regulated entity. You may not offer it
to third parties as a hosted compliance decisioning service.

**Client libraries: [Apache-2.0](sdk/LICENSE).** Separate on purpose — integrating
means importing a library into a regulated company's payment path, and a
permissive licence there means that decision never has to reach their legal team.
