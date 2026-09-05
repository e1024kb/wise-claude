# impl-plan-auto

<!-- This README is the source of truth for how the workflow LOOKS to
     users. Keep it in sync with workflow.yaml: every edit to the flow,
     steps, inputs or outputs belongs here too (CONTRIBUTING.md 9.6). -->

Autonomous plan-file -> PR pipeline, `version: 2`, run by the TS
engine. Give it one or more `PLAN-*.md` files (for example the plans
`/wise-revise` writes into `docs/plans/`); for each one the engine's
`units` step claims a branch and worktree, re-plans the seed against
current HEAD, implements the refreshed plan, converges the branch
through a review / fix loop, pushes, opens a PR, requests the bot
reviews, watches CI and the bots, fixes what they raise, and merges
once the PR is green and quiet. One worktree + branch + PR per plan
file. A merged PR loses its worktree and local branch; anything else
stays open for a human with the worktree kept for inspection. No
prompts after launch.

The per-plan loop is engine code (`plugins/wise/engine/src/units.ts`,
`pipeline: plan`; design in `docs/wise/research-ts-engine.md` P4). It
is the same loop `ticket-auto` runs; only the plan phase differs (its
template `engine/src/prompts/units/plan/plan.md` re-plans from the seed
file instead of from a ticket). `/wise-implement-plan-auto` is the
implement-only building block (task waves + commits, no push / PR /
watch); this workflow is the full pipeline around a plan file.

## When to use

- You have ready-made plan files (from `/wise-revise`, `ticket-plan`,
  or written by hand in the same schema) and want each turned into a
  reviewed, merged PR unattended.

## When not to use

- You want to run a plan's tasks and stop before pushing: use
  `/wise-implement-plan-auto <plan>`.
- You start from a ticket, not a plan: use `ticket-auto`.

## Prerequisites

- `/wise-init` completed at least once (Node, gh CLI + auth).
- Run from inside the project's git repository (`project-selection:
  current`); the base working tree must be clean and have an `origin`
  remote (`preflight-checks` refuses otherwise).
- Every plan file must exist; `split-plans` stops the run before any
  worktree exists when one is missing.

## Flow

```mermaid
flowchart TD
    A[preflight-checks<br/>bash - clean tree, gh auth, origin] --> B[split-plans<br/>bash - comma list -> JSON array of absolute paths plan_list]
    B --> D[process<br/>units pipeline plan - one unit per seed plan -> units rows]
    D --> E[report<br/>agent sonnet - verify PRs, write run-dir/report.md -> merged, open, failed, report_path]
```

Inside `process`, per plan file and in this order (branch = the file
name without `PLAN-` and `.md`, sanitised):

| Phase | Kind | Group / model | What it does |
|---|---|---|---|
| `claim` | code | - | Idempotent ownership: a ledger under `<run-dir>/units/` marks the unit ours; a foreign worktree or branch is skipped. |
| `worktree` | code | - | `<run-dir>/worktrees/<branch>` on the plan branch off the fetched base. |
| `plan` | model | `plan` | Reads the seed, checks drift against its `SOURCE_SHA`, re-audits the scope at HEAD, writes the refreshed plan to `<run-dir>/plans/PLAN-<ref>.md`. `insufficient-context` (with a `BLUEPRINT-<ref>.md`) fails the unit. |
| `implement` | model | `implement` | Task waves, one atomic commit per task, validation after each commit. `done = 0` or no commits fails the unit. |
| `review` <-> `fix` | model | `review` / `implement` | 3-lens review of `origin/<base>..HEAD` writes a findings file; the fixer applies it (resuming the reviewer's session under `resume: unit` when both run on the same harness, else fresh); repeats up to `max_review_cycles`, then pushes anyway with `converged: false`. |
| `push`, `pr`, `request-review` | code | - | `git push -u`, PR from the repo template or a compact body (links the plan), `gh pr edit --add-reviewer` for each login in `reviewers`. |
| `watch` (+ `fix`, `push`) | model | `watch` / `implement` | One pass per poll: CI state, human comments, bot reviews. Red CI or open bot items go to `fix` then `push` (each counts against `max_fix_attempts`); a stuck bot gets the substitute review once per head; a human comment stands the loop down; `watch_stable_passes` consecutive green passes merge (squash, then merge commit). |
| `cleanup` | code | - | Only on `merged`: remove the worktree and the local branch. |

## Pre-flight questions

| Id | Kind | Default | Notes |
|---|---|---|---|
| `harness.<group>` | choice | `claude` | One per group (`plan`, `implement`, `review`, `watch`; `fix` follows `implement`); asked only when another CLI is logged in. |
| `model.<group>` | choice | `claude-opus-5` (`watch`: `claude-sonnet-5`) | The engine's catalog for the chosen harness. |
| `effort.<group>` | choice | `high` (`watch`: `medium`) | The chosen model's efforts; skipped when it takes one or none. |
| `input.plans` | text | - | Comma-separated `PLAN-*.md` paths; relative paths resolve against the repo root. |
| `input.guidance` | text | `""` (or the context `guidance`) | Standing instruction the engine hands to every model phase. |

Unit caps (`profiles.medium.caps`; only `medium` is applied):

| max_review_cycles | max_fix_attempts | watch_minutes | watch_poll_seconds | watch_stable_passes |
|---|---|---|---|---|
| 3 | 5 | 60 | 60 | 2 |

## Steps

| Step | Type | Purpose |
|---|---|---|
| `preflight-checks` | `bash` | Clean base tree, `gh auth status`, `origin` remote. |
| `split-plans` | `bash` | Splits the `plans` input on commas and semicolons, trims, dedupes, resolves each path against the repo root, fails when a file is missing, emits a JSON array of absolute paths as `plan_list`. |
| `process` | `units` | `pipeline: plan`, `items: {{plan_list}}`. Groups `plan`, `implement`, `review`, `fix -> implement`, `watch`; caps from `profiles.medium`; `reviewers: [copilot-pull-request-reviewer]`; `resume: unit`. Emits `units` (one row per plan). |
| `report` | `agent` (sonnet) | `trigger-rule: all-done`. Renders the `units` rows, verifies every PR with `gh pr view`, writes `<run-dir>/report.md` (table, why each non-merged unit stopped, `git worktree remove` commands, usage per unit). Emits `merged`, `open`, `failed`, `report_path`. |

## Inputs

| Name | Required | Description |
|---|---|---|
| `plans` | yes | Comma-separated `PLAN-*.md` paths, relative to the repo root or absolute. |
| `guidance` | no | Free-form operator guidance for the whole run (libraries to prefer, files to avoid, guardrails). Pre-filled from the context `guidance`. |

## Outputs

| Name | Source | Content |
|---|---|---|
| `plan_list` | `split-plans` | JSON array of absolute seed plan paths, the `units` items. |
| `units` | `process` | `UnitRow[]`: `unit` (ref, branch, worktree, base, plan_path = the seed, pr), `verdict` (`merged`, `all-green`, `blocked`, `partial`, `exhausted`, `human-intervention`, `failed`, `skipped`), `reason`, `review` (converged, cycles), `cleaned`. Full ledgers under `<run-dir>/units/<branch>.json`, the refreshed plans under `<run-dir>/plans/`. |
| `merged`, `open`, `failed`, `report_path` | `report` | Counts and the report file. |

## Examples

```
/wise-workflow-run impl-plan-auto
# Pre-flight asks harness, model and effort per group, and the plan files.

/wise-workflow-run impl-plan-auto docs/plans/PLAN-api-caching.md,docs/plans/PLAN-auth-debt.md
# Two plans, no spaces. Sequential units, one PR each.

/wise-workflow-run impl-plan-auto docs/plans/PLAN-api-caching.md keep the public API unchanged
# Everything after the first token is the guidance input.
```

## Related

- [Definition YAML](./workflow.yaml)
- [`ticket-auto`](../ticket-auto/README.md): the same pipeline started
  from tickets.
- [`/wise-revise`](../../skills/wise-revise/SKILL.md): writes the
  `PLAN-*.md` files this workflow consumes.
- [`/wise-implement-plan-auto`](../../skills/wise-implement-plan-auto/SKILL.md):
  the implement-only building block.
- `docs/wise/research-ts-engine.md` P4: the `units` contract.
