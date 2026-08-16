# ADR-0004 — The dashboard is server-rendered Django, with no build step

**Status:** accepted
**Date:** 2026-08-16

## Context

§6.5 calls the dashboard a React SPA. That was written before two decisions this
project has since made, and both of them push the other way.

**Distribution leads with `pip install complylayer`** (roadmap, settled before
phase 0). The dashboard ships inside the wheel. A React SPA means node in the
release pipeline, a committed build output or a build step at install time, and a
second set of dependencies for a customer's security team to audit — on a product
whose pitch to that team is that it collects almost nothing and depends on little.

**The management API already enforces everything.** Roles, tenant scoping,
approval rules and the audit trail all live behind the API and are tested there.
An SPA would re-express a subset of that in TypeScript, and the copy would drift.

## Decision

Django templates, server-rendered, with vanilla JavaScript for the two places
that genuinely need interactivity: the rule builder's live expression preview,
and inline validation. No node, no bundler, no framework.

CSS is hand-written against the tokens in `DESIGN.md`, in one stylesheet. The
design system is small on purpose — one accent that is only severity, three
fonts, a 4px scale — and small design systems do not need a component library to
stay consistent, they need a stylesheet somebody reads.

## Consequences

**What this costs.**

- No component ecosystem. Every table, drawer and form control is written here.
  That is perhaps two days of work that a library would have given away, and it
  is the honest price.
- Rich client-side interaction is harder. If the review queue later needs
  virtualised scrolling over 50,000 rows, this decision gets revisited rather
  than worked around.
- No hot module reload. Editing a template means refreshing a page.

**What it buys.**

- `pip install complylayer` and the dashboard is there. No node, ever.
- One implementation of who-may-do-what. The template asks the same permission
  functions the API does, so a button that should not exist is absent for the
  same reason the endpoint returns 403.
- Nothing to keep in sync. There is no client-side model of a rule that can
  disagree with the server's.
- A security surface a customer can read in an afternoon.

## What would reverse this

If the dashboard grows a genuinely application-like surface — a canvas, a
real-time collaborative editor, an analytics workspace with client-side
aggregation — the balance changes and an SPA becomes the right answer for that
part. The seam is already drawn: the management API is complete and documented in
`docs/openapi.yaml`, so a future SPA would consume the same contract rather than
requiring a rewrite behind it.

## Alternatives considered

| Alternative | Why not |
|---|---|
| React SPA, per §6.5 | Node in the release pipeline for a product that ships as a wheel; a second implementation of the permission rules |
| htmx | Genuinely tempting, and close to what is written here by hand. Rejected only because the interactive surface is two components, and one 12 KB dependency to avoid perhaps 80 lines of JavaScript is not a trade this project needs to make |
| Django admin | Free, and wrong. The rule builder is the product's centre of gravity; a generic CRUD interface over the models would make the central thing generic |
