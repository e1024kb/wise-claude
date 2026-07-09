---
name: wise-pr-watch
description: >-
  Watch the current branch's PR and drive it to green: block on
  `gh pr checks --watch`, classify failing checks (lint / tests / other)
  and auto-fix them — committing via the shared Conventional-Commits
  routine — then walk four sequential review queues
  (humans → Copilot → CodeRabbit → Sonar), each gated by an interactive
  Paged-bulk / Fix-all / Walk-step-by-step / Skip choice with a phased
  collect → commit → remote-side-effects → push apply. Surfaces new PR
  comments each iteration (a reviewer saying "stop autofixing"
  short-circuits the loop) and exits on all-green — confirmed across two
  consecutive post-green stability windows so late comments aren't
  missed — push failure, user abort, or a no-progress safety catch. Runs the shared
  `watch-pipelines` procedure. Requires an open PR on the
  current branch. Invoked as `/wise-pr-watch` (bare alias) or
  `/wise:wise-pr-watch` (canonical). Use when the user says "watch the
  PR", "drive the pipelines", "fix the failing checks", "babysit CI", or
  types `/wise-pr-watch`.
argument-hint: ""
allowed-tools: Read, Edit, Write, Bash(git:*), Bash(gh:*), Bash(npm:*), Bash(make:*), Bash(vendor/bin/codecept:*), Bash(cd:*), Bash(bash:*), Bash(cat:*), Bash(head:*), Bash(grep:*), Bash(date:*), Bash(test:*), AskUserQuestion
---

# /wise-pr-watch — watch a PR's pipelines and drive fixes

## Why this skill exists

After a PR lands, the ritual is: watch checks, fix the failures,
push, repeat until green. This skill is that loop, detached from PR
creation / reviewer attach so you can run it on any PR whenever you
want to hand off CI babysitting. (The `ticket-auto` workflow runs an
autonomous analogue of the same procedure.)

Single source of truth for the watch logic:
`harnesses/claude/wise/references/pr/watch-pipelines.md`. The drafting routine
the fix path commits with lives at
`harnesses/claude/wise/references/pr/commit-from-fix.md` (distilled from
`wise-commit-message` §3–§6). This skill reads `watch-pipelines.md`
at run time and follows it.

## Invocation

```
/wise-pr-watch
/wise:wise-pr-watch         # canonical namespaced form
```

No positionals, no flags.

## Procedure

This skill does NOT probe dependencies up-front. If `gh` / `git`
is missing or `gh` is unauthenticated, the first command below
fails with a clean error and Claude surfaces that to the user
with a pointer at `/wise-init`.

### 1. Verify a PR exists for this branch

```bash
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
PR_JSON="$(gh pr view --json number,url 2>/dev/null)"
```

If `PR_JSON` is empty, STOP with:

```
No open PR found for branch <BRANCH>.

Create one first:
  /wise-pr-create
```

Otherwise parse:

- `pr_number = .number`
- `pr_url = .url`
- `current_branch = <BRANCH>`

### 2. Detect the project path

```bash
PROJECT_PATH="$(git rev-parse --show-toplevel)"
```

### 3. Read and run watch-pipelines.md

Read the fragment:

```
Read: ${CLAUDE_PLUGIN_ROOT}/references/pr/watch-pipelines.md
```

Follow its procedure with the context:

- `pr_number = <number>`
- `pr_url = <url>`
- `current_branch = <BRANCH>`
- `project.path = <PROJECT_PATH>`

The fragment owns the whole loop — the `gh pr checks --watch` block,
failure classification, per-class fix dispatch (committing autofixes
via `commit-from-fix.md`), the four review queues with their phased
apply, and the exit conditions. Your job after it returns is §4.

### 4. Summarise

When the fragment returns its final line, translate it into a
user-facing summary:

- `WATCH: all-green url=<url>` → "All checks green ✓ PR is ready for
  review: `<url>`".
- `WATCH: aborted reason=<reason>` → "Watch aborted — `<reason>`.
  Re-run `/wise-pr-watch` when you're ready to resume."
- `WATCH: partial url=<url> accepted=<names>` → "PR is ready for
  review but not every check is green — the following were accepted
  with rationale: `<names>`. PR: `<url>`."

## Guardrails

- This is a **standalone slash-command skill**, independent of the
  `/wise` natural-language helper. It reads shared prompt fragments
  but does NOT invoke other wise action skills.
- Never create a PR here — bail with the pointer at
  `/wise-pr-create` if the branch has no open PR.
- Never attach reviewers here — `/wise-pr-add-reviewers` owns that
  surface.
- Never force-push or skip hooks when committing autofixes —
  `commit-from-fix.md` enforces this; don't work around it.
- Never dismiss a reviewer comment asking you to stop. The
  fragment treats "please hold off" / "I'm reviewing manually" as
  an implicit abort and exits; let it.
- Long-running: expect the skill to occupy the conversation for
  minutes to tens of minutes while CI runs. The user can interrupt
  at any time by typing a normal message — the fragment's loop
  responds to user input between iterations.
