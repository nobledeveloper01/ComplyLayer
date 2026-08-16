# ComplyLayer

**Pluggable compliance rules and decision engine for fintechs.**

A decision API a fintech calls before finalising any transaction. It returns `allow`, `flag` or
`block` in under 100 milliseconds, based on rules the compliance officer writes and edits
themselves — no engineer, no pull request, no deploy.

> The person who owns the regulatory risk should be able to change the control without filing a
> ticket.

<!-- phase: 0 -->

## Status — phase 0, in progress

Planning is complete and reviewed. No code yet. **Nothing here serves a decision.**

| Phase | | What it delivers |
|---|---|---|
| 0 | ⬜ | Foundations: tooling, CI gates, `doctor`, hello-world script |
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

Nothing yet. This section lists real, runnable capability only — if a decision cannot be served, it
does not appear here.

## Try it

Nothing to run yet. From phase 0 this section carries the shortest command sequence that
demonstrates the most recent phase, and every command in it is executed before that phase is called
done.

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
