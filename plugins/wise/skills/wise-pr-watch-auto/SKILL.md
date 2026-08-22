---
name: wise-pr-watch-auto
description: >-
  Autonomous variant of `/wise-pr-watch` — watch the current branch's
  PR pipelines, auto-fix failing checks (lint / tests / other),
  commit + push, then trigger + wait for the bot reviews (Copilot and
  CodeRabbit) and handle every bot review comment by severity (minors
  fixed, majors via a considered decision, false positives dismissed
  with a reasoned reply). A stuck bot never blocks the merge: when
  Copilot times out / errors / is rate-limited, or CodeRabbit is out of
  credits / rate-limited / silent, the loop runs wise's own high-depth
  reviewer panel (the `/wise-code-review-auto` pass) over the branch
  diff instead, commits + pushes what it finds, and keeps going. Loops
  until CI is green and every comment is resolved and the PR has stayed
  quiet for two consecutive post-green stability windows (so late
  comments aren't missed), or an attempt cap is hit; then merges the PR
  (squash → merge-commit fallback, branch protection respected). Leaves the PR open for a human on any non-minor
  comment it can't confidently resolve, and stands down the moment a
  human comments. NO prompts. Built for unattended ticket→PR runs.
  Invoked as `/wise-pr-watch-auto` (bare alias) or
  `/wise:wise-pr-watch-auto` (canonical). Use when the user says "watch
  the PR and fix it without asking", "auto-drive CI to green", or types
  `/wise-pr-watch-auto`. For the interactive version use `/wise-pr-watch`.
argument-hint: "[<max-fix-attempts>]"
allowed-tools: Read, Edit, Write, Bash(git:*), Bash(gh:*), Bash(npm:*), Bash(make:*), Bash(vendor/bin/codecept:*), Bash(cd:*), Bash(bash:*), Bash(cat:*), Bash(head:*), Bash(grep:*), Bash(date:*), Bash(test:*), Bash(sleep:*)
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

Copilot and CodeRabbit are review *inputs*, not merge gates. When one of
them is down — timeout, error, rate limit, out of credits — the loop
substitutes wise's own review (`review-fallback-auto.md`, the same
5-lens panel `/wise-code-review-auto` runs), records that it did, and
keeps driving the PR to green and merge. The branch still gets reviewed
before it merges; it just isn't held hostage to a vendor's uptime.

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
`file:line` references the fragment reported. If the line carries a
`copilot=stuck` or `coderabbit=<bypassed|gave-up>` annotation, name
which bot could not review and why (timeout / error / rate limit / out
of credits / never answered); when `review-fallback=ran` is present, say wise reviewed
the branch itself in that bot's place and how many findings it applied.
`all-green reason=review-fallback-failed` means a bot was stuck AND the
substitute review failed, so nothing reviewed the branch — the PR is
left open for a human.

## Guardrails

- Never call `AskUserQuestion`.
- Never force-push, never `--no-verify`.
- Merge only a fully resolved PR — every CI check green, every expected
  bot terminal (Copilot reviewed / absent / stuck; CodeRabbit reviewed /
  bypassed / gave-up / absent), every stuck bot covered by a successful
  local review pass, and every comment from a bot that reviewed
  fixed-or-dismissed; never force a merge or override branch protection;
  never merge past an unresolved non-minor bot comment (a `blocked`
  verdict leaves the PR open).
- A stuck bot degrades to the local review panel; it never stops the
  run. But never merge a branch that nothing reviewed — a stuck bot plus
  a failed fallback leaves the PR open.
- Drive SonarCloud open issues to **zero** before merging — fix each, or
  accept it with a minimum-scope suppression + rationale (or a Sonar MCP
  `change_issue_status`). Never merge with open issues. If the issues
  can't be fetched (no token / no MCP), postpone: keep working
  everything else, remind the operator, and leave the PR open
  (`all-green reason=sonar-unchecked`) rather than guessing it's clean.
- Stand down the moment a human comments on the PR.
- Stop cleanly at the attempt cap and the stuck-loop safety catch.
- Never invoke another wise action skill (the fragment reads
  `commit-from-fix.md` / `handle-bot-reviews-auto.md` /
  `handle-sonar-issues-auto.md` / `review-fallback-auto.md` directly —
  the review fallback runs `/wise-code-review-auto`'s *fragment*, not
  the skill).
