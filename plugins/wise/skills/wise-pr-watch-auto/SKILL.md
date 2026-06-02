---
name: wise-pr-watch-auto
description: >-
  Autonomous variant of `/wise-pr-watch` — watch the current branch's
  PR pipelines, auto-fix failing checks (lint / tests / other),
  commit + push, then wait for CodeRabbit / Copilot and handle every
  bot review comment by severity (minors fixed, majors via a considered
  decision, false positives dismissed with a reasoned reply). Loops
  until CI is green and every comment is resolved, or an attempt cap is
  hit; then merges the PR (squash → merge-commit fallback, branch
  protection respected). Leaves the PR open for a human on any non-minor
  comment it can't confidently resolve, and stands down the moment a
  human comments. NO prompts. Built for unattended ticket→PR runs.
  Invoked as `/wise-pr-watch-auto` (bare alias) or
  `/wise:wise-pr-watch-auto` (canonical). Use when the user says "watch
  the PR and fix it without asking", "auto-drive CI to green", or types
  `/wise-pr-watch-auto`. For the interactive version use `/wise-pr-watch`.
argument-hint: "[<max-fix-attempts>]"
allowed-tools: Read, Edit, Write, Bash(git:*), Bash(gh:*), Bash(npm:*), Bash(make:*), Bash(vendor/bin/codecept:*), Bash(cd:*), Bash(bash:*), Bash(cat:*), Bash(head:*), Bash(grep:*), Bash(date:*), Bash(test:*)
---

# /wise-pr-watch-auto — autonomous CI watch + fix loop

## Why this skill exists

`/wise-pr-watch` is a long interactive loop — it escalates test/other
fixes and walks four review queues with the user. An unattended
ticket→PR pipeline can't stop to ask. `/wise-pr-watch-auto` drives the
same CI-poll → classify → fix → commit → push loop, waits for the
review bots to finish, and addresses every bot review comment
autonomously — classified by severity, minors fixed quickly,
major/critical ones via a considered consolidated decision, false
positives dismissed with a reasoned reply: the Lead Architect persona
makes every call, no `AskUserQuestion`. It is also the reusable
building block the `ticket-auto` workflow's watch step follows.

## Arguments

Read `$ARGUMENTS`. The first whitespace-separated token, if present,
is `max_fix_attempts` — the cap on commit-producing fix rounds before
the loop stops. Default 10 when absent. Ignore anything else.

## Procedure

### 1. Verify a PR exists for the current branch

```bash
git rev-parse --show-toplevel
git rev-parse --abbrev-ref HEAD
gh pr view --json number,url
```

If `gh pr view` fails (no PR for this branch), stop with a clear
message pointing at `/wise-pr-create-auto`.

### 2. Follow the shared fragment

Read `${CLAUDE_PLUGIN_ROOT}/workflows/ticket-auto/prompts/watch-pipelines-auto.md`
and follow it end to end with `pr_number`, `pr_url`, `current_branch`,
`project.path` (the toplevel), and `max_fix_attempts`.

### 3. Relay the verdict

The fragment's final line is `WATCH-AUTO: <merged|all-green|blocked|partial|exhausted|human-intervention> url=<url>`.
Translate it into a short user-facing summary: whether the PR was
merged, what is green, what was accepted or left unfixed, and — for
`all-green` (merge blocked) / `exhausted` / `partial` /
`human-intervention` — that the PR needs a human's attention. For a
`blocked` verdict, say the PR was left open because a non-minor bot
review comment needs the user's judgement, and list the `items=`
`file:line` references the fragment reported.

## Guardrails

- Never call `AskUserQuestion`.
- Never force-push, never `--no-verify`.
- Merge only a fully resolved PR — every CI check green, both review
  bots finished, and every bot comment fixed-or-dismissed; never force
  a merge or override branch protection; never merge past an
  unresolved non-minor bot comment (a `blocked` verdict leaves the PR
  open).
- Never suppress a Sonar issue autonomously.
- Stand down the moment a human comments on the PR.
- Stop cleanly at the attempt cap and the stuck-loop safety catch.
- Never invoke another wise action skill (the fragment reads
  `commit-from-fix.md` / `handle-bot-reviews-auto.md` directly).
