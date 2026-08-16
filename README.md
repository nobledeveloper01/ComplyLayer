# ComplyLayer

**Pluggable compliance rules and decision engine for fintechs.**

A decision API a fintech calls before finalising any transaction. It returns `allow`, `flag` or
`block` in under 100 milliseconds, based on rules the compliance officer writes and edits
themselves — no engineer, no pull request, no deploy.

> The person who owns the regulatory risk should be able to change the control without filing a
> ticket.

<!-- phase: 2 -->

## Status — phase 1 complete, phase 2 in progress

The rule language exists and is sandboxed. A rule can be written, validated and rejected with a
message a compliance officer can act on. **Nothing here serves a decision yet** — the interpreter
that evaluates a validated rule, and the endpoint that returns an outcome, are phase 2.

| Phase | | What it delivers |
|---|---|---|
| 0 | ✅ | Foundations: tooling, CI gates, `doctor`, hello-world script |
| 1 | ✅ | The AST sandbox, escape corpus written first |
| 2 | ⬜ | Interpreter, determinism, the decision endpoint |
| 3 | ⬜ | Velocity counters and aggregate facts |
| 4 | ⬜ | Rule cache, versioning, the latency contract |
| 5 | ⬜ | Management API, approval workflow, tenancy |
| 6 | ⬜ | The dashboard and rule builder |
| 7 | ⬜ | Backtest, shadow mode, review queue, analytics |
| 8 | ⬜ | Load, chaos, packaging, release |

A phase is ticked when its exit gate in [`docs/ROADMAP.md`](docs/ROADMAP.md) is green in CI, not when
it feels finished. Phases 0–4 are a shippable milestone: they deliver the sandbox, the itemised
latency budget, and reproducible decisions.

## What works today

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
| [`docs/adr/`](docs/adr/) | Architecture decision records |

## The parts worth reading first

- **[ADR-0001](docs/adr/0001-ast-interpreter-not-eval.md)** — why rules are interpreted over a
  validated AST and never `eval`. This is the security core of the product; the obvious
  implementation is remote code execution offered as a feature.
- **[The latency budget](docs/plan-architecture.md#latency-budget-restated-with-the-omissions-filled-in)**
  — 100 ms p99, itemised by stage, including the two costs the specification's own budget omitted.
- **[What went wrong in the plan](docs/plan-review-report.md#phase-3--engineering-review)** — nothing
  wrote the velocity counters, the retention promise and the throughput target could not both be
  true, and decisions were not reproducible as designed. All three are fixed; the reasoning is more
  useful than the fix.

## Licence

To be decided before the first public release.
