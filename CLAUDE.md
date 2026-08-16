# ComplyLayer

A compliance decision engine for fintechs. Read `docs/plan-architecture.md` for
the decisions the specification left open, and `docs/ROADMAP.md` for what phase
the project is in and what its exit gate is.

## Design System

Always read `DESIGN.md` before making any visual or UI decision. Fonts, colours,
spacing and aesthetic direction are defined there. Do not deviate without
explicit approval.

Two rules from it are load-bearing and easy to break by accident:

- **Colour is reserved for severity.** No brand colour in the chrome, no blue
  primary buttons, no coloured links. If a pixel is saturated it means `block`,
  `flag`, `allow` or `degraded`.
- **The approval diff is not a text diff.** It shows the change in currency
  units, at display size, with the direction and magnitude called out.

In QA and review, flag any code that does not match `DESIGN.md`.

## Working on this repo

- `make ci` is the gate. It needs Postgres and Redis — `docker compose up -d`,
  and copy `.env.example` to `.env` if 5432 or 6379 are taken on your machine.
- The escape corpus in `tests/test_dsl_escapes.py` is a blocking gate. Any change
  to `ALLOWED_NODES` or `ALLOWED_FUNCTIONS` needs a corresponding entry.
- Never `eval` or `exec`. `scripts/no_eval_guard.py` enforces it; ADR-0001
  explains why.
- The README is a phase deliverable. `scripts/check-readme-phase.sh` fails the
  build if `PHASE` moves without it.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool.
When in doubt, invoke the skill.

- Product ideas / brainstorming → `/office-hours`
- Strategy / scope → `/plan-ceo-review`
- Architecture → `/plan-eng-review`
- Design system / plan review → `/design-consultation` or `/plan-design-review`
- Full review pipeline → `/autoplan`
- Bugs / errors → `/investigate`
- QA / testing site behaviour → `/qa` or `/qa-only`
- Code review / diff check → `/review`
- Visual polish → `/design-review`
- Ship / deploy / PR → `/ship` or `/land-and-deploy`
- Security audit → `/cso`
