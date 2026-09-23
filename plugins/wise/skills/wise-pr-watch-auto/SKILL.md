---
name: wise-pr-watch-auto
description: >-
  Autonomous variant of `/wise-pr-watch` — drive the checked-out
  branch's open PR to merge on the wise engine: the bundled `pr-watch`
  workflow polls CI and the review bots (Copilot, CodeRabbit), fixes
  what they raise, pushes, runs wise's own substitute review when a bot
  is stuck (only if allowed at pre-flight), and merges once the PR is
  green and quiet (branch protection respected). Pre-flight asks, once,
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
substitute-review / merge loop `ticket-auto` runs after its PR is open).
This skill exists so the loop can be started on its own, for a PR that
already exists, with the same per-phase harness / model / effort choice
every workflow gets at pre-flight — on any harness, not only Claude Code.

Copilot and CodeRabbit are review *inputs*, not GitHub branch-protection
gates, but their verification state does feed Wise's own merge decision. After a
fix batch is pushed the engine requests one CodeRabbit verification
review of the new head when CodeRabbit is on the PR but has not reviewed
that head (repositories with automatic incremental reviews off), never
one per push and never twice for a head; the rules are
`references/pr/review-verification.md`. When a bot is
stuck the run may review the branch itself (one read-only 3-lens pass on
the `review` group's model) only when the `substitute_review` pre-flight
input says `yes`; on `no` a stuck bot ends the run as
`all-green reason=review-consent-declined` without reviewing or merging.
That question is the consent gate, asked once before launch.

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
  `substitute_review` consent, the remaining inputs, then
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
`review-consent-declined`, `wall-clock`, the fix cap, a bot item in the
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
- The engine merges only a PR whose CI is green and whose bot reviews are
  resolved or covered for `watch_stable_passes` consecutive passes
  (squash, then merge commit); a required approval is reported as
  `all-green`, never worked around. A human comment stands the run down.
- Sonar issues are not part of the engine loop; use `/wise-pr-watch` for
  a PR gated on Sonar.
- Run state lives in the engine's run directory; `/wise-workflow-resume
  <run_id>` continues an interrupted run.
- Never invoke another wise action skill; the `wise-workflow-run`
  procedure is read as the conductor routine, not invoked as a skill.
