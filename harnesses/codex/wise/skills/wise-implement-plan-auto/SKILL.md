---
name: wise-implement-plan-auto
description: >-
  Autonomously implement a written implementation plan (a `PLAN-*.md`
  produced by ticket-auto / ticket-plan) in the current git working tree —
  the plan's task waves are phase gates, each task in a wave is handed to
  a fresh-context executor agent running in parallel, and every task lands
  as one atomic commit with per-task verification (type-check / lint /
  tests). NO prompts. Invoked as `/wise-implement-plan-auto`. Use when the
  user says "implement the plan", "execute PLAN-*.md", "build out the plan
  autonomously", or types `/wise-implement-plan-auto`.
---

## Harness adaptation note

This skill was authored for Claude Code and adapted for OpenAI Codex CLI. Where the steps below reference Claude-specific tools, substitute:

- **Task / subagent dispatch (`subagent_type: wise:<role>`)** — spawn a subagent with the role card at `${WISE_PLUGIN_ROOT}/agents/<role>.md` if this harness supports subagents; otherwise adopt that role yourself and perform the steps sequentially.
- **TodoWrite** — keep a visible checklist in your replies instead.


# /wise-implement-plan-auto — execute a plan, autonomously

## Why this skill exists

`ticket-plan` and `ticket-auto` produce a `PLAN-*.md` but no skill
*executes* one. `/wise-implement-plan-auto` is that executor — a
phase-gated model: parse the plan's task waves, dispatch one fresh-context
executor agent per task in parallel, commit each task atomically,
verify as it goes. It is the reusable building block the `ticket-auto`
workflow's implement phase follows.

## Arguments

Read `$ARGUMENTS`. The first whitespace-separated token, if present,
is the path to the `PLAN-*.md` to implement. When absent, look for a
single `PLAN-*.md` at the git toplevel and use it; if there are zero
or several, stop and ask the user to name one.

## Procedure

### 1. Resolve the worktree + plan

```bash
git rev-parse --show-toplevel
```

Use the toplevel as `worktree`. Resolve `plan_path` from `$ARGUMENTS`
(or the discovery rule above).

### 2. Follow the shared fragment

Read `${WISE_PLUGIN_ROOT}/workflows/ticket-auto/prompts/implement-plan.md`
and follow it end to end with `plan_path`, `worktree`, `project.kind`
(infer from the worktree's manifest if unknown), and `SUPERVISE=yes`.
The fragment processes waves in order, dispatches the wave's executors
per task (persona: this skill's `agents/executor.md`) — **supervised**
background teammates a leader loop nudges if one hangs or goes idle
mid-task — then simplifies (per-task, scoped to the task's files via
`references/simplify-pass.md`) and commits each task sequentially, and
verifies per task. (To fall back to plain blocking `Task` executors,
pass `SUPERVISE=no` / set `WISE_WORKER_*` env to tune the watchdog.)

### 3. Relay the result

The fragment's final line is
`IMPLEMENT: waves=<w> tasks=<t> done=<d> failed=<f>`. Summarise for
the user — waves run, tasks done, tasks failed (with which ones) —
and remind them nothing was pushed (commit/push is a separate step).

## Guardrails

- Never call `AskUserQuestion` mid-run — the only prompt is the
  argument-resolution stop in §Arguments when the plan is ambiguous.
- One atomic commit per task; never bundle tasks.
- Executors edit files; only this skill simplifies (per-task, scoped)
  and commits, serially — never let parallel subagents race the git
  index. The heavier high-depth code-review branch gate is a separate,
  later pipeline step, not this skill's job.
- Never `git push` — that is the caller's step.
- A failed task does not abort the run.
- Never invoke another wise action skill.
