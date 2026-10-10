---
name: wise-pr-watch-auto
description: >-
  Autonomous variant of `/wise-pr-watch` — drive the checked-out
  branch's open PR to merge on the wise engine: the bundled `pr-watch`
  workflow polls CI and any review bot configured on the repo, runs
  wise's own local review (the 3-lens code review team) on each new
  head, fixes what they raise, pushes, and merges once the PR is green,
  locally reviewed and quiet (branch protection respected). Never
  triggers or requests a remote review. Pre-flight asks, once,
  which harness, model and effort run each phase (watch, fix, review,
  report); nothing prompts after launch. A human comment stands the run
  down. Runs in the current checkout, never a worktree. Invoked as
  `/wise-pr-watch-auto` (bare alias) or `/wise:wise-pr-watch-auto`
  (canonical). Use when the user says "watch the PR and fix it without
  asking", "auto-drive CI to green", or types `/wise-pr-watch-auto`.
  For the interactive version use `/wise-pr-watch`.
argument-hint: "[<max-fix-attempts>] [--minutes <n>]"
allowed-tools: Read, Write, AskUserQuestion, TodoWrite, Bash(git:*), Bash(gh:*), Bash(bash:*), Bash(cat:*), Bash(mkdir:*), Bash(test:*)
---

# /wise-pr-watch-auto — conduct the `pr-watch` workflow

This skill is a thin conductor: it starts the bundled `pr-watch`
workflow on the wise engine and follows the run. It has no model
preference of its own — pre-flight asks harness, model and effort per
phase — so the
[model fallback](../../references/workflow-host-control.md#model-fallback)
contract applies only to what the engine's pre-flight offers.

At every skill start, identify your main/child role and the current client
and GUI/TUI question tools, then read and follow the
[question lifecycle](../../references/workflow-host-control.md#keep-asynchronous-questions-open).
Keep asynchronous prompts open until answered; this rule does not authorize
questions beyond the engine's pre-flight in this autonomous procedure.

## Why this skill exists

`/wise-pr-watch` is a long interactive loop that walks review queues
with the user. Routine watch and fix rounds run unattended. The
unattended loop is engine code (the `pr` units pipeline: claim the
checked-out branch's open PR, then the same watch / fix / push /
local-review / merge loop `ticket-auto` runs after its PR is open).
This skill exists so the loop can be started on its own, for a PR that
already exists, with the same per-phase harness / model / effort choice
every workflow gets at pre-flight — on any harness, not only Claude Code.

Wise never triggers a remote review: no Copilot reviewer request, no
`@coderabbitai review` comment, no re-request after a push. A bot
configured on the repo reviews on its own; its review threads are fixed
or dismissed like any other finding. The engine only observes the bot's
state for the head (`references/pr/review-verification.md`) and holds
the merge while that bot's own review is running, or a trigger someone
else posted is unanswered (at most 15 minutes). A silent or stuck bot
blocks nothing.

Wise's own review is the local one: once per new PR head, when CI is
green and no bot item is open, the `review` group runs the read-only
3-lens panel (correctness, security, tests; the same prompt as the
pre-push gate). `changes-requested` sends the findings to a fix pass
and push (counted against `max_fix_attempts`); `approve` covers the
head. The merge needs CI green, the local review approved for the
head, and no open bot item.

## Arguments

Read `$ARGUMENTS` and split into whitespace-separated tokens:

- `--minutes <n>` — wall-clock budget for the whole run (default 10).
  `n` must be an integer in `1..1440`.
- The first remaining token, if present, is `max_fix_attempts` — the
  cap on fix + push rounds (default 10). Must be a positive integer.
- Anything else, a `--minutes` with no value, or a value out of range
  is an error — stop before the run with the matching message:

  ```
  Unknown argument(s): <the extra tokens>
  Usage: /wise-pr-watch-auto [<max-fix-attempts>] [--minutes <n>]
  ```

  ```
  Unknown --minutes value: <value> (must be an integer 1-1440)
  Usage: /wise-pr-watch-auto [<max-fix-attempts>] [--minutes <n>]
  ```

A given argument is passed as the matching workflow input
(`max_fix_attempts`, `watch_minutes`) and that input's pre-flight
question is not asked; an omitted one is asked (blank keeps the default).
Which harness, model and effort run each phase is never an argument:
pre-flight asks it.

## Procedure

### 0. GitHub remote check

Read `${CLAUDE_PLUGIN_ROOT}/references/pr/github-remote.md` and run its
check first, before launching the `pr-watch` workflow (so no pre-flight
runs). If the outcome is `none` or `other`, print the single
watch-variant line from its table and stop successfully; never call
`gh pr`. Only continue when the outcome is `github`.

### 1. Verify a PR exists for the current branch

```bash
git rev-parse --show-toplevel
git symbolic-ref --quiet --short HEAD
gh pr view --json number,url,state,baseRefName
```

Detached HEAD or a protected branch (`main` / `master` / `release*`) →
stop with a clear message. No PR → stop pointing at
`/wise-pr-create-auto`. PR not `OPEN` → report it and stop; there is
nothing to watch. Print the PR url and its base branch.

### 2. Conduct the `pr-watch` workflow

Read `${CLAUDE_PLUGIN_ROOT}/skills/wise-workflow-run/SKILL.md` and
follow its §1 (init check), §2 (pre-flight), §3 (start), §4 (wait
loop) and §5 (final report) with:

- `workflow` = `pr-watch`, `cwd` = the git toplevel.
- `answers` seeded with `input.max_fix_attempts` / `input.watch_minutes`
  when the argument was given; everything else comes from the staged
  pre-flight, put to the user exactly as that skill prescribes: the
  remaining inputs, then
  `harness.<group>`, `permissions.<harness>`, `model.<group>` and
  `effort.<group>` for the `watch`, `fix`, `review` and `support`
  groups (the worktree question is locked to the current checkout).
- `context` = `{}` unless the conversation already knows the ticket the
  PR implements (then `ticket[]` as that skill describes).

Say up front, before `wise_run`, when the base branch requires
approvals: the run will drive the PR to green but cannot merge it.

### 3. Relay the verdict

The `process` step's `units` row carries the outcome:
`verdict` (`merged` | `all-green` | `blocked` | `partial` | `exhausted`
| `human-intervention` | `failed` | `skipped`) and `reason`; the
`report` step writes `<run-dir>/report.md`. Summarise: whether the PR
was merged, how many watch passes and fix rounds it took, what was
fixed, and — for anything but `merged` — that the PR needs a human,
with the reason spelled out (`approval-required`, a branch rule,
`wall-clock`, the fix cap, a bot item or local-review finding in the
findings file, a human comment). Link the PR.

## Guardrails

- Without a GitHub remote, print the one-line notice from
  `references/pr/github-remote.md` and stop; never launch the workflow
  and never call `gh pr`.
- The only questions are the engine's pre-flight (rendered by this main
  harness) and a gate the run opens; never answer one yourself and never
  ask anything else mid-run.
- Never execute a workflow step here: the engine's provider children run
  the watch, fix and review phases. Never force-push, never `--no-verify`;
  the engine's phases never do either.
- The engine merges only a PR whose CI is green, whose head the local
  review approved and with no open bot item, for `watch_stable_passes`
  consecutive passes (squash, then merge commit); a required approval
  is reported as `all-green`, never worked around. A human comment
  stands the run down.
- Never trigger or request a review bot (Copilot code review,
  CodeRabbit); the engine never does either.
- Sonar issues are not part of the engine loop; use `/wise-pr-watch` for
  a PR gated on Sonar.
- Run state lives in the engine's run directory; `/wise-workflow-resume
  <run_id>` continues an interrupted run.
- Never invoke another wise action skill; the `wise-workflow-run`
  procedure is read as the conductor routine, not invoked as a skill.
