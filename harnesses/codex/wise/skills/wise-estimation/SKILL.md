---
name: wise-estimation
description: >-
  Story-point estimation reference — a Fibonacci 0.5 → 13 SP scale for
  sizing work across frontend, backend, mobile, and all teams, including
  how to size uncertain work and the "anything > 8 SP is a research
  ticket, not an implementation ticket" rule. Use whenever the user says
  "estimate", "story points", "SP", "size this ticket", "how many points",
  "t-shirt size", "how long will this take", "break down the estimate",
  "is this a 3 or a 5", or is planning/refining a Jira ticket and needs a
  point value.
---

# Story-Point Estimation

Canonical story-point (SP) scale for sizing Jira tickets. Applies to
every team (frontend, backend, mobile, platform) — using the same units
so cross-team planning works.

This is a **reference doc**, not a procedure. When the user is
planning a ticket or breaking work into tasks, consult this scale to
pick a value; when the user asks "is this a 3 or a 5?", walk the
complexity/scope/time axes below to decide.

## The scale

Fibonacci, with a 0.5 minimum for truly trivial work.

| SP | Complexity | Typical scope | Calendar time |
|----|------------|---------------|---------------|
| **0.5** | Trivial, mechanical | Single file, one obvious change (copy tweak, constant rename, one-line config) | ~half day |
| **1** | Simple, well-understood | 1–2 files, clear requirements, pattern already exists | ~1 day |
| **2** | Moderate | 3–5 files, some decisions to make, one or two unknowns | ~2 days |
| **3** | Complex | Multiple components, non-trivial logic, meaningful test surface | ~3 days |
| **5** | Large feature | Architecture decisions, several components, full test coverage | ~1 week |
| **8** | Epic-level | Multiple sub-tasks, crosses file/module boundaries, touches API + UI | ~2 weeks |
| **13** | Major feature with unknowns | Research needed, spec not fully baked, architecture open | ~3 weeks+ |

## How to pick a number

Size against **three axes**. The estimate is the HIGHEST of the
three, not the average — an unfamiliar-tech ticket with one file is
still not a 1.

1. **Scope** — how many files, components, or systems change?
2. **Complexity** — how much decision-making, algorithmic work, or
   cross-cutting coordination is involved?
3. **Uncertainty** — how much of the work is genuinely understood
   up-front vs. will-need-to-figure-it-out-as-we-go?

Examples:

- Change a button colour across one component + its story →
  **0.5** (low on all three).
- Add a new field to an existing form, with validation, backend
  wiring, and tests → **2** (scope drives it).
- Rewrite a contested caching layer with existing patterns →
  **3** (complexity drives it).
- Integrate a third-party SDK we've never used → **5 minimum**
  (uncertainty drives it, even if the file count is small).

## When to research instead of estimate

Anything > **8 SP** is almost always a signal, not an estimate.
The correct response is usually one of:

- **Split the ticket.** 13 SP rarely means "let's block out three
  weeks" — it means the ticket bundles multiple real tickets that
  should be separated (parent epic + 3–5 implementation tickets,
  each in the 2–5 SP range).
- **Spike first.** If the uncertainty is what's driving the size,
  create a small time-boxed research ticket (1–2 SP, fixed
  duration) whose deliverable is a design doc or a plan. Then
  re-estimate the real work after the spike.
- **Ask the PM.** If the scope is this large because the ticket is
  too ambitious, the answer is usually product-side scope cut, not
  engineering heroics.

## Decomposition rule of thumb

When breaking a ticket into sub-tasks during planning, aim for
**every sub-task to be ≤ 3 SP**. If a sub-task is a 5, that's a
hint there's another decomposition step you haven't made yet.

The final ticket estimate is the **sum of the sub-tasks** (not a
separate judgment), rounded to the nearest Fibonacci value if the
sum falls between two. This keeps estimates honest — you can't
estimate lower than the pieces add up to.

Example breakdown for a dropdown feature from a Figma design:

```
Task 1 — add `useFeatureXQuery` hook                  (1 SP)
Task 2 — build DropdownSelector component             (2 SP)
Task 3 — wire component into SettingsPanel            (0.5 SP)
Task 4 — unit tests for hook + component              (1 SP)
                                                   ─────────
                                                Total  4.5 SP → round to 5 SP
```

## Bias and calibration

Two common estimation mistakes to watch for:

- **Anchoring to wall time.** "It'll take me a day" → 1 SP is the
  wrong reasoning. Use the complexity/scope/uncertainty axes; the
  calendar-time column is a *consequence* of the size, not an
  input. Time estimates are personal; SP estimates are shared.
- **Optimism about unfamiliar code.** If the work touches a module
  nobody on the team has edited in the last 6 months, bump the
  estimate by one step. The surprises are always in the unknown
  code.

## What NOT to use this skill for

- Velocity / capacity planning — that's PM territory, not engineering.
- Setting deadlines — SP is a sizing tool, not a commitment.
- Cross-team comparison of individuals — SP is a team unit of
  shared understanding, not a productivity metric.
