---
name: wise-pr-watch-auto
description: >-
  Autonomous variant of `/wise-pr-watch` — drive the current branch's PR
  to merge in bulk rounds with NO prompts. Each round: one linear
  2-minute poll until CI is terminal and every review bot that is going
  to review the head (Copilot, CodeRabbit) has done so; gather every
  failing check, every unresolved bot thread (outdated ones included),
  every open Sonar issue; fix the whole set in one pass, resolve every
  handled thread immediately, one commit, one push; wait one 2-minute
  re-review window to see what the push triggered (the push IS the
  trigger — no `@coderabbitai review` while a bot is auto-reviewing);
  settle again. Converges on the first head with nothing actionable, or
  after two nit-only rounds (remaining nits accepted and resolved, no
  push), or at the round cap. Reads the base branch's rules up front
  (thread-resolution rule, required approvals), re-reads the PR state at
  every tick so a PR merged or closed from outside ends the run, and
  keeps its state under the PR so a re-invocation resumes. A stuck bot
  gets wise's own substitute review instead of blocking; a human comment
  stands the run down. Merges (squash → merge-commit fallback, branch
  protection respected). Invoked as `/wise-pr-watch-auto` (bare alias)
  or `/wise:wise-pr-watch-auto` (canonical). Use when the user says
  "watch the PR and fix it without asking", "auto-drive CI to green", or
  types `/wise-pr-watch-auto`. For the interactive version use
  `/wise-pr-watch`.
argument-hint: "[<max-fix-attempts>] [--minutes <n>] [--profile low|medium|max] [--on <harness>[:<model>[:<effort>]] | --on ask]"
allowed-tools: Read, Edit, Write, Task, Bash(git:*), Bash(gh:*), Bash(python3:*), Bash(npm:*), Bash(make:*), Bash(vendor/bin/codecept:*), Bash(cd:*), Bash(bash:*), Bash(cat:*), Bash(head:*), Bash(grep:*), Bash(date:*), Bash(test:*), Bash(sleep:*), Bash(mkdir:*), Bash(touch:*), Bash(tail:*), Bash(rm:*), Bash(stat:*), Bash(chmod:*), Bash(id:*), Bash(mv:*), AskUserQuestion
---

# /wise-pr-watch-auto — autonomous CI watch + bulk-fix loop

## Why this skill exists

`/wise-pr-watch` is a long interactive loop that walks review queues
with the user. An unattended pipeline cannot stop to ask.
`/wise-pr-watch-auto` runs the same job as a **round loop** the Lead
Architect persona drives alone:

```
settle → gather → bulk-fix → push → re-review window → (settle …) → merge
```

One push per round, every handled thread resolved before that push,
every wait a linear 2-minute poll that re-reads the PR state and the
human-comment gate. It ends when a settled head has nothing actionable,
never at "the bot posted another nit".

Copilot and CodeRabbit are review *inputs*, not merge gates. When one is
down the loop substitutes wise's own review
(`review-fallback-auto.md`), records it, and keeps going.

## Arguments

Read `$ARGUMENTS` and split into whitespace-separated tokens:

- `--profile <low|medium|max>` — per-run override of the session
  token-budget profile.
- `--minutes <n>` — wall-clock budget for the whole run (default 120).
  `n` must be an integer in `1..1440` (one minute to one day). The loop
  stops with `exhausted reason=wall-clock` when it runs out.
- The first remaining token, if present, is `max_fix_attempts` — the
  cap on commit-producing rounds. Ignore anything else.
- A `--profile` / `--minutes` with no value, or a value out of range, is
  an error — stop before the loop with the matching message:

  ```
  Unknown --profile value: <value>
  Usage: /wise-pr-watch-auto [<max-fix-attempts>] [--minutes <n>] [--profile low|medium|max]
  ```

  ```
  Unknown --minutes value: <value> (must be an integer 1-1440)
  Usage: /wise-pr-watch-auto [<max-fix-attempts>] [--minutes <n>] [--profile low|medium|max]
  ```

Resolve `profile`: the argument if given, else the session profile via
`${CLAUDE_PLUGIN_ROOT}/references/profile-read.md` (silent degrade to
`medium`). The profile scales budget only — never gates, verdicts or
merge rules:

| profile | max_fix_attempts default | fixer tier | Opus model (`opus_model`) |
|---|---|---|---|
| `low` | 3 | prefer sonnet-grade focus | `claude-opus-4-8` (MUST — never Opus 5) |
| `medium` | 10 | today's defaults | `opus` |
| `max` | 10 | today's defaults | `opus` |

An explicit `max_fix_attempts` always beats the profile's default.

## Run on another harness (`--on`)

If `$ARGUMENTS` contains `--on <harness>[:<model>[:<effort>]]` (or
`--on ask` / a bare `--on`), do NOT run the procedure below here. Strip
the `--on` tokens (everything left is `SKILL_ARGS`), then read
`${CLAUDE_PLUGIN_ROOT}/references/dispatch.md` and follow it with:

- `SKILL_MD` = `${CLAUDE_PLUGIN_ROOT}/skills/wise-pr-watch-auto/SKILL.md`
- `SKILL_ARGS` = the remaining tokens
- `TIMEOUT_S` = `(watch_minutes + 15) * 60` — the child's timeout must
  outlast the loop's own wall-clock budget, or the dispatcher kills a
  run that was about to finish.

`--on ask` (or a bare `--on`) picks harness, model and effort through
one composite `AskUserQuestion` before any child spawns — the ONE
sanctioned prompt in this skill. While the child runs, tail its
heartbeat instead of waiting blind:

```bash
tail -n 5 "${TMPDIR:-/tmp}/wise-pr-watch/<owner>/<repo>/<pr_number>/progress.log"
```

The reference relays the child's verdict line; §3 below applies to it.

## Procedure

### 1. Verify a PR exists for the current branch

```bash
git rev-parse --show-toplevel
git rev-parse --abbrev-ref HEAD
gh pr view --json number,url,state
```

No PR → stop with a clear message pointing at `/wise-pr-create-auto`.
PR not `OPEN` → report it and stop; there is nothing to watch.

### 2. Follow the shared fragment

Read `${CLAUDE_PLUGIN_ROOT}/workflows/ticket-auto/prompts/watch-pipelines-auto.md`
and follow it end to end with `pr_number`, `pr_url`, `current_branch`,
`project.path` (the toplevel), `max_fix_attempts`, `watch_minutes`,
`profile`, `opus_model` (the table's last column), and
`dispatch_mode=task` — the bot-thread and Sonar handlers run as fresh
`Task` subagents that return only their verdict lines.

Print the fragment's progress-log path on the first line of output, and
— when the base branch requires approvals — say up front that this run
will drive the PR to green but cannot merge it.

### 3. Relay the verdict

The fragment's final line is
`WATCH-AUTO: <merged|merged-externally|closed|all-green|blocked|partial|exhausted|human-intervention> url=<url> rounds=<n> …`.
Translate it into a short summary: whether the PR was merged (by this
run or by someone else), how many rounds it took, what was fixed,
accepted or left, and — for `all-green` / `blocked` / `partial` /
`exhausted` / `human-intervention` — that the PR needs a human, with the
`reason=` spelled out (`approval-required`, `behind`, `dirty`, a branch
rule, `sonar-unchecked`, `review-fallback-failed`, `wall-clock`,
`rounds`, `stuck-loop`). For `blocked` list the `items=` `file:line`
references. Name any bot that could not review (`copilot=stuck`,
`coderabbit=<bypassed|gave-up>`) or skipped a docs-only head
(`coderabbit=skipped`), and when `review-fallback=ran` say wise reviewed
the branch in its place and how many findings it applied.
`converged=nits-accepted` means the last remaining minor comments were
accepted as-is and resolved rather than fixed — say so.

## Guardrails

- Never call `AskUserQuestion` mid-run — the one exception is the
  `--on ask` pick before the loop starts.
- Never force-push, never `--no-verify`.
- Every wait is a linear 2-minute poll through the fragment's `tick`:
  PR state, human gate and wall-clock deadline at every tick. No
  `--watch`, no multi-minute sleeps, no backoff.
- The push is the review trigger. Never post `@coderabbitai review` on a
  head younger than the grace period, on a docs-only head, on a
  rate-limited CodeRabbit, on a bot that auto-reviews and already has a
  footprint on the head, on a PR that is not open, or twice for one
  head. Every trigger posted is deleted before the run ends.
- One push per round; every handled thread resolved before it.
- Merge only a fully resolved PR — CI green, every expected bot terminal
  for the head, every stuck bot covered by a successful substitute
  review, zero unresolved bot threads verified live, Sonar at zero or
  proven absent, `mergeStateStatus` read live. Never force a merge or
  override branch protection; a required approval is reported as
  `all-green reason=approval-required`, never worked around.
- Never merge a branch nothing reviewed.
- Stand down the moment a human comments.
- Stop cleanly at the round cap, the wall-clock budget and the
  unchanged-head catch; converge on nits instead of chasing them.
- State lives under `${TMPDIR:-/tmp}/wise-pr-watch/<owner>/<repo>/<pr>/`
  (owner and repo as separate path segments, never joined — a joined
  `owner-repo` string can collide across repos) and is removed only
  when the PR is merged or closed, so a killed or re-invoked run
  resumes its counters and trigger bookkeeping.
- `Task` is granted for the `dispatch_mode=task` handlers (one bot-thread
  subagent, one Sonar subagent per round) and the review fallback's
  reviewer panel. Nothing else spawns subagents.
- Never invoke another wise action skill — the fragment reads
  `commit-from-fix.md` / `handle-bot-reviews-auto.md` /
  `handle-sonar-issues-auto.md` / `review-fallback-auto.md` directly.
