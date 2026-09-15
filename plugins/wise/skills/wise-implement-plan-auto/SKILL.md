---
name: wise-implement-plan-auto
description: >-
  Autonomously implement a written implementation plan (a `PLAN-*.md`
  produced by ticket-auto / ticket-plan / wise-revise) on the checked-out
  branch, on the wise engine: the bundled `impl-plan` workflow turns the
  plan's task waves into atomic commits, each task tidied and validated,
  nothing pushed. Pre-flight asks, once, which harness, model and effort
  implement the plan; nothing prompts after launch. Invoked as
  `/wise-implement-plan-auto` (bare alias) or
  `/wise:wise-implement-plan-auto` (canonical). Use when the user says
  "implement the plan", "execute PLAN-*.md", "build out the plan
  autonomously", or types `/wise-implement-plan-auto`.
argument-hint: "[<plan-file-path>]"
allowed-tools: Read, Write, AskUserQuestion, TodoWrite, Bash(git:*), Bash(bash:*), Bash(cat:*), Bash(ls:*), Bash(mkdir:*), Bash(test:*)
---

# /wise-implement-plan-auto — conduct the `impl-plan` workflow

This skill is a thin conductor: it starts the bundled `impl-plan`
workflow on the wise engine and follows the run. It has no model
preference of its own — pre-flight asks harness, model and effort for
the implementer — so the
[model fallback](../../references/workflow-host-control.md#model-fallback)
contract applies only to what the engine's pre-flight offers.

At every skill start, identify your main/child role and the current client
and GUI/TUI question tools, then read and follow the
[question lifecycle](../../references/workflow-host-control.md#keep-asynchronous-questions-open).
Keep asynchronous prompts open until answered; this rule does not authorize
questions beyond the engine's pre-flight in this autonomous procedure.

## Why this skill exists

`ticket-plan`, `ticket-auto` and `/wise-revise` produce a `PLAN-*.md`;
this skill executes one on the branch you already have checked out and
stops before any push. The implement loop is engine code (the
`implement` units pipeline: claim the checked-out branch, then the same
implement phase `ticket-auto` runs — task waves, one atomic commit per
task, per-task tidy and validation). This skill exists so that phase can
run on its own, with the same harness / model / effort choice every
workflow gets at pre-flight — on any harness. The full plan → PR
pipeline is the `impl-plan-auto` workflow.

## Arguments

Read `$ARGUMENTS`. The first whitespace-separated token, if present,
is the path to the `PLAN-*.md` to implement (relative to the repo root
or absolute). When absent, look for a single `PLAN-*.md` at the git
toplevel and under `docs/plans/`; use it when exactly one exists,
otherwise leave the `plan` pre-flight question to the user. Anything
after the first token is an error:

```
Unknown argument(s): <the extra tokens>
Usage: /wise-implement-plan-auto [<plan-file-path>]
```

## Procedure

### 1. Resolve the checkout + plan

```bash
git rev-parse --show-toplevel
git symbolic-ref --quiet --short HEAD
```

Detached HEAD or a protected branch (`main` / `master` / `release*`) →
stop with a clear message; implementation lands on the checked-out
branch. Resolve the plan path from `$ARGUMENTS` (or the discovery rule
above) and check the file exists.

### 2. Conduct the `impl-plan` workflow

Read `${CLAUDE_PLUGIN_ROOT}/skills/wise-workflow-run/SKILL.md` and
follow its §1 (init check), §2 (pre-flight), §3 (start), §4 (wait
loop) and §5 (final report) with:

- `workflow` = `impl-plan`, `cwd` = the git toplevel.
- `answers` seeded with `input.plan` when resolved; everything else
  comes from the staged pre-flight, put to the user exactly as that
  skill prescribes: the `guidance` input, then `harness.<group>`,
  `permissions.<harness>`, `model.<group>` and `effort.<group>` for the
  `implement` and `support` groups (the worktree question is locked to
  the current checkout).
- `context` = `{guidance}` when the conversation carries operator
  guidance for the implementation (libraries to prefer, files to avoid).

### 3. Relay the result

The `process` step's `units` row carries `verdict` (`all-green` |
`failed` | `skipped`) and `reason` (`implemented: <done> of <tasks>
tasks in <n> commits (failed <f>)` on success); the `report` step writes
`<run-dir>/report.md`. Summarise waves run, tasks done, tasks failed
(with which ones) and the commits, and remind the user nothing was
pushed — `/wise-workflow-run code-review` then `/wise-pr-create` are the
next steps.

## Guardrails

- The only questions are the engine's pre-flight (rendered by this main
  harness) and a gate the run opens; never answer one yourself and never
  ask anything else mid-run.
- Never execute the implement phase here: the engine's provider child
  runs it (parallel executor subagents when its harness can spawn them,
  sequential inline tasks otherwise). One atomic commit per task; the
  heavier code-review branch gate is a separate, later step.
- Never `git push` — the engine's `implement` pipeline has no push phase.
- A failed task does not abort the run; the verdict reports it.
- Never invoke another wise action skill; the `wise-workflow-run`
  procedure is read as the conductor routine, not invoked as a skill.
