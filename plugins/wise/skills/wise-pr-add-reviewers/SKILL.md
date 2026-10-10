---
name: wise-pr-add-reviewers
description: >-
  Request human reviewers on the PR for the current branch: ask
  whether to add reviewers, then propose candidates (CODEOWNERS,
  recent authors, org members) as picks plus free-text logins. Never
  requests a review bot (Copilot code review, CodeRabbit). Idempotent
  - already-requested reviewers are detected and not re-requested.
  This skill runs just the reviewer attach step on an existing PR.
  Fails with a
  clear message if the current branch has no open PR — run
  `/wise-pr-create` first. Invoked as `/wise-pr-add-reviewers` (bare
  alias) or `/wise:wise-pr-add-reviewers` (canonical). Use when the
  user says "add reviewers", "request review", or types
  `/wise-pr-add-reviewers`.
argument-hint: ""
allowed-tools: Read, Bash(git:*), Bash(gh:*), Bash(cd:*), Bash(bash:*), AskUserQuestion
---

# /wise-pr-add-reviewers — attach reviewers to the current branch's PR

Before executing, follow [model fallback](../../references/workflow-host-control.md#model-fallback)
for unavailable models or delegation routes, including in autonomous procedures.

At every skill start, identify your main/child role and the current client
and GUI/TUI question tools, then read and follow the
[question lifecycle](../../references/workflow-host-control.md#keep-asynchronous-questions-open).
Keep asynchronous prompts open until answered; this rule does not authorize
questions in autonomous or otherwise prompt-free procedures.

## Why this skill exists

Most PRs end up requesting 0-N individuals picked from CODEOWNERS or
the org. This skill is the narrowed surface - just the human reviewer
attach step, on a PR that already exists (`/wise-pr-create` makes the
PR; `/wise-pr-watch` drives CI and runs the local review). Review bots
are never requested: a bot configured on the repo reviews on its own.

Single source of truth for the reviewer logic:
`plugins/wise/references/pr/ensure-reviewers.md`. This skill reads it
at run time and follows it.

## Invocation

```
/wise-pr-add-reviewers
/wise:wise-pr-add-reviewers         # canonical namespaced form
```

No positionals, no flags. Anything the user types beyond the skill
name is ignored.

## Procedure

This skill does NOT probe dependencies up-front. If `gh` / `git`
is missing or `gh` is unauthenticated, the first command below
fails with a clean error (`command not found`, or `gh: auth status:
not logged in`) and Claude surfaces that to the user with a
pointer at `/wise-init`.

### 0. GitHub remote check

Read `${CLAUDE_PLUGIN_ROOT}/references/pr/github-remote.md` and run its
check first. If the outcome is `none` or `other`, print the single
reviewers-variant line from its table and stop successfully; never call
`gh pr`. Only continue when the outcome is `github`.

### 1. Verify a PR exists for this branch

```bash
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
PR_JSON="$(gh pr view --json number,url 2>/dev/null)"
```

If `PR_JSON` is empty (gh returned non-zero — no PR for this
branch), STOP with:

```
No open PR found for branch <BRANCH>.

Create one first:
  /wise-pr-create
```

Do NOT attempt to create a PR here — that's `/wise-pr-create`'s
responsibility. This skill operates only on existing PRs.

Otherwise parse the JSON:

- `pr_number = .number`
- `pr_url = .url`

### 2. Detect the project path

```bash
PROJECT_PATH="$(git rev-parse --show-toplevel)"
```

### 3. Read and run ensure-reviewers.md (current reviewers + ask about extras)

Read the fragment:

```
Read: ${CLAUDE_PLUGIN_ROOT}/references/pr/ensure-reviewers.md
```

Follow its procedure with the context:

- `pr_number = <number>`
- `pr_url = <url>`
- `project.path = <PROJECT_PATH>`

The fragment reads the reviewers already requested and asks via
`AskUserQuestion` whether to add human reviewers. It attaches
nothing. Capture its final line and parse `ALREADY_REQUESTED` /
`EXTRAS_CHOICE`:

```
REVIEWERS: attached=<slugs-or-NONE> extras=<no|yes>
```

### 4. If extras_choice is `yes`, run propose-reviewers.md

Conditional on `EXTRAS_CHOICE == 'yes'`. Read:

```
Read: ${CLAUDE_PLUGIN_ROOT}/references/pr/propose-reviewers.md
```

Follow its procedure with the context:

- `pr_number = <number>`
- `pr_url = <url>`
- `pr_base = <gh pr view --json baseRefName --jq .baseRefName>`
- `project.path = <PROJECT_PATH>`
- `already_requested = <ALREADY_REQUESTED>`

The fragment ranks reviewer candidates (changed files, CODEOWNERS,
recent authors ∩ org members) and surfaces them as **multi-select**
`AskUserQuestion` picks (plus an Other freetext field), then attaches
the chosen logins via `gh pr edit --add-reviewer`. Capture its final
line and parse `EXTRAS_ATTACHED`:

```
EXTRAS: attached=<comma-separated-logins-or-NONE>
```

When `EXTRAS_CHOICE` is `no`, skip this step - there are no
extras to propose.

### 5. Summarise

Print the final list of reviewers on the PR. Two cases:

- `EXTRAS_CHOICE == 'yes'` → concatenate already-requested + extras:
  `Reviewers on <pr_url>: <ALREADY_REQUESTED>, <EXTRAS_ATTACHED>`
- Else → just the already-requested ones:
  `Reviewers on <pr_url>: <ALREADY_REQUESTED>`
  (or `no reviewers requested` when both are `NONE`)

If the user wants to watch pipelines next, point them at:

```
Watch pipelines + comments with:
  /wise-pr-watch
```

## Guardrails

- This is a **standalone slash-command skill**, independent of the
  `/wise` natural-language helper. It reads a shared prompt fragment
  but does NOT invoke other wise action skills.
- Without a GitHub remote, print the one-line notice from
  `references/pr/github-remote.md` and stop; never call `gh pr`.
- Never create a PR here — bail with the pointer at
  `/wise-pr-create` if the branch has no open PR.
- Never remove a reviewer.
- Never re-request a reviewer who's already on the PR — noisy on
  the PR's activity log.
- Never request a review bot (Copilot code review, CodeRabbit): no
  `--add-reviewer copilot-pull-request-reviewer`, no GraphQL
  `requestReviews` for a bot, no `@coderabbitai review` comment.
  Humans only.
