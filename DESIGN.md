# Design System — ComplyLayer

## Product Context

- **What this is:** A dashboard where a compliance officer changes a control that
  decides whether money moves, and a risk manager approves the change.
- **Who it's for:** Adaeze, Head of Compliance — non-technical, numerate,
  accountable for every limit in the system. Emeka, Risk Manager — approves rule
  changes and needs separation of duties to be real rather than a policy
  document.
- **Space:** Regulated fintech tooling. Peers are Sardine, Unit21, ComplyAdvantage.
  The category's visual language is enterprise grey: dense, cautious, forgettable.
- **Project type:** Data-dense internal web app. Not a marketing site, and the
  design should not borrow from one.

### The memorable thing

> **"I could see at a glance that the limit had moved by ten times."**

Every decision below serves that sentence. The approval diff is the screen where
a risk manager either catches a weakened control or does not, and
`amount_minor > 5_000_000` becoming `amount_minor > 50_000_000` is one character.
A design that makes that character loud is doing its job; a design that makes the
dashboard look modern and leaves that change quiet has failed at the only moment
that matters.

---

## Aesthetic Direction

- **Direction:** Industrial / utilitarian, with an institutional serif for
  headings.
- **Decoration level:** Minimal. Typography and one accent do all the work. No
  texture, no gradients, no illustration.
- **Mood:** A control room, not a product tour. Sober, dense, legible at 3am.
  It should feel like a system of record — closer to a ledger than to a SaaS
  dashboard.
- **Not this:** the category default of blue-grey chrome, rounded cards floating
  on a light grey field, and a brand colour sprayed across every button.

---

## Typography

Three families, each with a job. Nothing is chosen for looks alone.

- **Display / headings: Instrument Serif.**
  A serif in a dashboard is unusual, which is the point. It signals *record*
  rather than *app*, and it separates page and section headings from data without
  needing a second colour or a heavier weight. Used at 24px and above only —
  below that it loses its argument and its legibility.

- **Body, UI and labels: Geist.**
  Neutral, excellent at 13–15px, and it ships genuine tabular figures. Not Inter:
  every product in this category uses Inter, and this one has a reason to look
  like it was designed rather than defaulted.

- **Data and tables: Geist with `font-variant-numeric: tabular-nums`.**
  Non-negotiable. A column of amounts whose digits do not align is a column
  somebody misreads, and the amounts here are limits on other people's money.

- **Rule expressions and code: JetBrains Mono.**
  Rule text is the product's most important string. It is rendered as code —
  monospace, syntax-coloured, selectable — everywhere it appears: the builder,
  the diff, the audit trail, the decision log. Never paraphrased into prose.

- **Loading:** self-hosted WOFF2, subset to Latin. No CDN on a compliance tool —
  a third-party font request from a regulated customer's browser is a
  conversation nobody wants to have with their security team.

### Scale

| Role | Size / line-height | Family |
|---|---|---|
| Page title | 32 / 38 | Instrument Serif |
| Section heading | 24 / 30 | Instrument Serif |
| Subsection | 18 / 24 | Geist Medium |
| Body | 15 / 22 | Geist |
| UI label, table header | 13 / 18, +0.02em tracking, uppercase | Geist Medium |
| Caption, metadata | 12 / 16 | Geist |
| Rule expression | 14 / 22 | JetBrains Mono |
| Amount, display | 28 / 32, tabular | Geist Medium |

---

## Color

**Approach: restrained, and stricter than that word usually means. Colour is
reserved for severity. Nothing else in the interface is coloured.**

This is the system's central rule and its biggest risk. Buttons are not blue.
Links are not blue. The nav is not branded. The only saturated pixels on any
screen mean `block`, `flag` or `allow`.

The reason: in a tool where red means "this transaction was refused", a red that
also means "delete", "required field" and "primary action" means nothing. Most
compliance dashboards spend their colour on chrome and then have nothing left for
the thing that matters. This one spends it all on severity.

### Neutrals — warm, not blue-grey

Blue-grey is the category's tell. Warm grey reads as paper and ink, which suits a
system of record.

| Token | Hex | Use |
|---|---|---|
| `--ink` | `#1A1815` | Primary text, headings |
| `--ink-muted` | `#5C574F` | Secondary text, metadata |
| `--ink-faint` | `#8A8378` | Placeholders, disabled |
| `--rule` | `#DFDAD1` | Borders, table rules |
| `--surface` | `#F7F5F1` | Page background |
| `--surface-raised` | `#FFFFFF` | Cards, table backgrounds |
| `--surface-sunken` | `#EFEBE4` | Code blocks, inset panels |

### Severity — the only colour

| Token | Hex | Meaning |
|---|---|---|
| `--block` | `#8C2F26` | Oxblood. A considered refusal, not an error. Fire-engine red says "something broke"; this says "this was decided." |
| `--block-wash` | `#F6E9E7` | Row and panel background for a blocked decision |
| `--flag` | `#9A6100` | Amber, dark enough to pass AA on white |
| `--flag-wash` | `#FBF1DF` | Row background for a flagged decision |
| `--allow` | `#2C5F3E` | Deep green. Used sparingly — most things are allowed, so allow should be quiet |
| `--degraded` | `#5B4B8A` | Muted violet. A control that did not run is neither pass nor fail, and it must not be mistaken for either |

`--degraded` earns its own colour because §11.3 treats a degraded decision as an
availability failure rather than a quality one. A reviewer must never read "the
rule did not run" as "the rule did not fire."

### Interactive

Interactive elements are indicated by **weight, underline and position**, not
hue. The primary action on a screen is `--ink` filled; secondary is outlined;
destructive is outlined in `--block`. Focus is a 2px `--ink` ring at 2px offset —
visible on every surface without introducing a colour.

### Dark mode

Not a filter over the light palette. Surfaces are redesigned (`#141310`,
`#1E1C18`, `#282520`), severity colours lift in luminance and drop ~15% in
saturation so they do not glow, and the serif goes one weight down because light
text on dark reads heavier than it measures.

---

## Spacing

- **Base unit:** 4px. This is a dense product and 8px-only scales force
  either wasted space or off-grid values.
- **Density:** Compact. A review queue that shows 8 rows is a queue somebody
  scrolls; one that shows 20 is a queue somebody works.
- **Scale:** `2xs 2 · xs 4 · sm 8 · md 12 · lg 16 · xl 24 · 2xl 32 · 3xl 48 · 4xl 64`
- **Table rows:** 36px. Tight enough to see the shape of a day's decisions,
  tall enough to hit a checkbox.

---

## Layout

- **Approach:** Grid-disciplined. Asymmetry and grid-breaking are for editorial
  work; here they cost scannability and buy nothing.
- **Grid:** 12 columns, 16px gutters. Sidebar 240px fixed, content fluid.
- **Max content width:** 1440px for tables and logs, 720px for anything with
  prose or a form — a 1400px-wide text field is a text field nobody can read
  back.
- **Border radius:** 3px on inputs, buttons and cards. 0 on table cells and code
  blocks. Nothing is pill-shaped. Uniform bubble-radius is the visual signature
  of a product designed by a component library rather than for a purpose.
- **Elevation:** borders, not shadows. One shadow, used once: the approval
  drawer.

---

## Motion

- **Approach:** Minimal-functional, with one deliberate exception.
- **Rule:** Motion exists to explain a state change. A compliance tool that
  animates for delight is one that wastes a reviewer's afternoon 200 rows at a
  time.
- **Durations:** micro 80ms (hover, focus) · short 160ms (drawer, menu) ·
  medium 240ms (page transition). Nothing longer.
- **Easing:** enter `ease-out`, exit `ease-in`, move `ease-in-out`.
- **The exception:** on the approval diff, a changed threshold animates once —
  a 240ms highlight sweep on first render. It draws the eye to the number that
  moved, once, and then never again. That is the whole point of the screen.
- **`prefers-reduced-motion`:** everything above becomes instant, including the
  diff sweep, which is replaced by a static highlight.

---

## The approval diff

The highest-stakes screen in the product gets its own section, because treating
it as "a diff view" is how it ends up as a text diff.

**It is not a text diff.** `- amount_minor > 5_000_000` above
`+ amount_minor > 50_000_000` is technically complete and practically useless: a
reviewer scanning green-and-red lines sees a one-character change and approves it.

**What it shows instead:**

1. **The change, in the unit a human thinks in.** `₦50,000.00 → ₦500,000.00`,
   at display size, with `10× higher` beside it in `--block` when a limit loosens
   and `--allow` when it tightens. The direction of the change is the single most
   important fact on the screen.
2. **The expression**, in monospace, with only the changed token highlighted —
   not the whole line.
3. **The regulation the rule claims to implement**, pulled from
   `regulatory_reference`. A reviewer approving a change to a CBN tier limit
   should see "CBN KYC Tier 2" without leaving the screen.
4. **The backtest impact**, if one has been run: "would have blocked 1,204 of
   48,190 transactions last month, up from 118." A number a reviewer can weigh.
5. **Who asked, and why** — author and stated reason, because the approver is
   being asked to trust a person as much as a diff.

**What it must never do:** collapse the change behind "show diff", render the
amount in minor units without formatting, or put approve and reject next to each
other as equal-weight buttons. Approve is the considered action; it sits alone,
and it is disabled until the reviewer has scrolled past the impact figure.

---

## Safe choices

These follow the category, deliberately. A compliance officer arrives with habits
from three other tools, and spending novelty here would cost comprehension for
nothing.

- **Left sidebar navigation, persistent.** Nine sections, always visible.
- **Tables are tables.** Sortable columns, sticky headers, row selection,
  pagination. No card grids for tabular data.
- **Forms are labelled above the field**, with errors below it, and the label is
  never a placeholder.

## Risks

Where the product gets its own face. Each is a real bet with a real cost.

**1. A serif for headings, in a dashboard.**
*Gains:* it reads as a record rather than an app, and separates structure from
data without a second colour. *Costs:* it is unusual enough that somebody will
call it odd in the first week. *Why it holds:* the product's claim is that these
are controls of record, and the typography should say so before the copy does.

**2. No colour anywhere except severity.**
*Gains:* red means one thing. A blocked row is visible from across a room.
*Costs:* the interface has no brand colour, which will feel austere, and
somebody will ask for a blue primary button. *Why it holds:* a compliance tool
whose "delete" button is the same red as a blocked transaction has spent its most
valuable signal on furniture.

**3. Rule expressions rendered as code, always visible.**
*Gains:* the officer, the approver and the auditor all read the same text, and
the text is the thing that runs. No paraphrase to drift out of date.
*Costs:* monospace code in a non-technical user's interface risks intimidating
them. *Why it holds:* the builder stays primary and the expression sits beneath
it, updating live. Reading it is optional; trusting it is not. And the moment a
rule matters — an approval, an audit — everybody is looking at the same string.

---

## Decisions Log

| Date | Decision | Rationale |
|---|---|---|
| 2026-08-16 | Initial design system | `/design-consultation`, ahead of phase 6 per `docs/ROADMAP.md` — the builder's expressiveness and the DSL's function set are one decision, so the design cannot follow the implementation |
| 2026-08-16 | Colour reserved for severity only | In a tool where red means "refused", a red that also means "delete" means nothing |
| 2026-08-16 | Instrument Serif for headings | Signals a system of record rather than a SaaS dashboard; separates structure from data without a second colour |
| 2026-08-16 | Warm neutrals, not blue-grey | Blue-grey is the category's tell; warm grey reads as paper and ink |
| 2026-08-16 | `--degraded` gets its own colour | §11.3 treats a degraded decision as an availability failure; a reviewer must never read "did not run" as "did not fire" |
| 2026-08-16 | Semantic approval diff, not a text diff | A one-character change that moves a limit tenfold must be the loudest thing on the screen |
| 2026-08-16 | Self-hosted fonts | A third-party font request from a regulated customer's browser is a conversation nobody wants with their security team |
