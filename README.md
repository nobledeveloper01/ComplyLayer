# ComplyLayer

**Pluggable compliance rules and decision engine for fintechs.**

A decision API a fintech calls before finalising any transaction. It returns `allow`, `flag` or
`block` in under 100 milliseconds, based on rules the compliance officer writes and edits
themselves — no engineer, no pull request, no deploy.

> The person who owns the regulatory risk should be able to change the control without filing a
> ticket.

<!-- phase: 1 -->

## Status — phase 0 complete, phase 1 in progress

The foundations are in and every gate is green. **Nothing here serves a decision yet** — the rules
engine is phase 1 and the endpoint is phase 2.

| Phase | | What it delivers |
|---|---|---|
| 0 | ✅ | Foundations: tooling, CI gates, `doctor`, hello-world script |
| 1 | ⬜ | The AST sandbox, escape corpus written first |
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
