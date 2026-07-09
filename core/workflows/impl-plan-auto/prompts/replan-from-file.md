# replan-from-file — refresh a ready-made plan against current HEAD, autonomously

The autonomous planning procedure for a **single** pre-built
`PLAN-*.md`. It is the plan phase of `impl-plan-auto`'s per-plan
pipeline — the same role `plan-ticket.md` plays for `ticket-auto`, but
seeded from a **provided plan file** (e.g. one `/wise-revise` wrote into
`docs/plans/`) instead of a tracker ticket.

The plan file is treated as the **seed**, not gospel: `/wise-revise`
stamps each plan with a `SOURCE_SHA`, and the codebase may have moved
since. This phase re-plans the seed against the worktree's **current
HEAD** — re-verifying the cited evidence, refreshing anything that
drifted, and re-deciding the approach — then writes a fresh plan the
implement phase runs. Every choice a human might make is made by the
**Lead Architect**.

Source of truth for `impl-plan-auto`'s re-plan phase. The
orchestrator runs this against each plan's worktree.

## Context the caller supplies

- `seed_plan` — absolute path to the provided `PLAN-*.md` to re-plan from.
- `worktree` — absolute path to this plan's git worktree (the codebase
  audit + drift re-verification read here, at current HEAD).
- `plan_path` — absolute path to write the **refreshed** plan to. The
  orchestrator points this at the **run directory** (off the project
  tree), so the plan persists with the run state and never lands in the
  branch. (Do NOT overwrite `seed_plan`.)
- `project.kind` — `frontend | backend | fullstack | other`, if known.
- `config_prompt` — the operator's free-form standing guidance (may be
  empty): skills / libraries to prefer, guidelines, guardrails, files to
  stay out of. The Lead Architect treats it as binding configuration for
  every decision below and folds it into `## Assumptions`.

## Procedure

### 1. Read the seed plan

Read `seed_plan` end to end. It is in wise's `PLAN-*.md` schema; extract
its intent:

- `## Source` — scope, `SOURCE_SHA`, evidence `file:line` references,
  the lens/leverage it came from.
- `## Summary` — the problem, why it matters, what "done" looks like.
- `## Assumptions`, `## Decisions Made` — the deliberate choices baked in.
- `## Current state` — the code excerpts the plan was written against.
- `## Tasks` (the wave breakdown), `## Testing`, `## Validation`,
  `## Stop conditions`.

This is the starting point — your job is to confirm it still holds at
HEAD and refresh it, not to discard it. If `seed_plan` is missing the
`## Tasks` / `## Validation` sections (not a well-formed plan), note it
and re-derive those sections from the rest in step 5.

### 2. Drift check against current HEAD

```bash
git -C "<worktree>" rev-parse --short HEAD
```

Compare it with the seed's `SOURCE_SHA` **by prefix** — either may be
short or full; they refer to the same commit when one is a prefix of the
other:

- **Same SHA** → the excerpts are still valid; a light re-verify suffices.
- **Different (or seed has none)** → the seed's `## Current state` /
  evidence may be stale. Re-open each cited `file:line` in `<worktree>`
  and confirm it still matches. Note every divergence — a moved line, a
  refactor, an already-fixed finding — for the architect's re-decision.

### 3. Classify the plan

Determine `plan_type` (`frontend | backend | fullstack | other`):
combine `project.kind` (or infer from the worktree's manifest —
`go.mod`/`pom.xml` → backend, `package.json` with React/Vue/Svelte →
frontend, both → fullstack) with the seed plan's vocabulary and the
files its evidence touches.

### 4. Parallel research wave (fresh audit at HEAD)

Dispatch `Task` (`subagent_type: "Explore"`) subagents concurrently
against `<worktree>`, scoped to the seed plan's `## Source` scope:

- **Re-verify** — re-open every evidence `file:line` from the seed and
  report whether each still matches at HEAD, with the current excerpt.
  Flag any finding the codebase has already addressed (so it can be
  dropped) or that has shifted (so the excerpt can be refreshed).
- **Codebase audit** — reuse-first audit routed by `plan_type` over the
  seed's scope: discover the worktree's layout, then locate the existing
  assets the plan should reuse vs. extend (UI components/hooks/styling for
  frontend; API handlers/models/services for backend; both for
  fullstack), each with path + line + reuse-as-is vs. needs-extension.

### 5. Re-decide

The Lead Architect folds the seed plan, the drift findings, and the
fresh audit into one refreshed investigation, then makes — autonomously,
with rationale — every scope / technical-approach / component / testing
decision the work needs, **at current HEAD**:

- Carry forward the seed's decisions that still hold.
- Drop any task whose finding the codebase already fixed (record it as a
  decision: "dropped — already addressed at HEAD").
- Refresh tasks whose target code moved; re-ground each task's Reuse/New
  classification in the fresh audit.
- Apply `config_prompt` as binding guidance: prefer the skills / libraries
  it names, respect its guidelines, guardrails, and "stay out of"
  constraints, honor any explicit knob override, and predict any answer it
  implies. For anything left open, take the maximum-quality option.

Output a `## Decisions Made` section: each decision + a one-line
rationale (note which carried over from the seed, which changed due to
drift, and which were steered by `config_prompt`).

### 6. Build the refreshed plan

Compose the implementation plan and **write it to the supplied
`plan_path`** with the `Write` tool (`mkdir -p` its parent dir first if
needed). Use wise's standard `PLAN-*.md` schema (what the implement phase
consumes — it reads `## Tasks`, `## Decisions Made`, `## Assumptions`,
`## Validation`):

```text
# <Title>   (carry the seed plan's title)
## Summary
## Assumptions   (every autonomous decision; the seed's provenance —
                  source scope + original SOURCE_SHA — and the drift state
                  you found; any access/uncertainty gap)
## Decisions Made
## Design Notes  (frontend / fullstack plans with design context only)
## Tasks         (parallelizable WAVES; each task "Reuse: …" / "New: …"
                  with file paths; per-task SP and total SP)
## Testing
## Validation    (exact, copy-pasteable type-check / lint / test commands
                  with expected output — carry the seed's where still valid)
```

The `## Tasks` section MUST be authored as ordered **waves** of
independent tasks — the implement phase dispatches one executor per task
and runs waves strictly in sequence. For SP estimation consult the
`wise-estimation` reference skill (Fibonacci, ≤ 3 SP per sub-task, round
the total to the nearest Fibonacci).

If the drift check found the plan **fully obsolete** (every finding
already fixed at HEAD, nothing left to do), still write a valid plan with
an empty `## Tasks` (no waves) and an `## Assumptions` note saying so —
the implement phase will report `done=0` and the orchestrator records the
plan as `verdict=failed reason=implement`, which the report surfaces.

### 7. Final line

FINAL line — alone, no markdown, no backticks — MUST match:

```text
PLAN: written=<plan_path> type=<plan_type>
```

## Guardrails

- Read-only against the codebase — this phase only inspects `<worktree>`
  and **writes the one refreshed plan** to `plan_path`. It never edits
  source and never overwrites `seed_plan`.
- Fully autonomous — never call `AskUserQuestion`. Every decision the
  seed leaves open or that drift forces is the Lead Architect's to make.
- Never invent code that is not there — re-verify cited evidence before
  carrying a task forward.
- All work runs in this Claude Code session with native `Task` subagents.
  Never shell out to `claude -p` or any external agent / LLM CLI.
