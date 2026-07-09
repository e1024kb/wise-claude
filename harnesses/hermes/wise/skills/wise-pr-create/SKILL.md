---
name: wise-pr-create
description: >-
  Detect the PR state for the current branch and either create a new
  GitHub PR or refresh an existing one — body drafted from the project's
  `.github/pull_request_template.md` (or a bundled fallback) filled from
  the branch's diff + commits, Jira key auto-detected from
  branch/diff/log/session, base branch picked interactively from `main` +
  recent `release*` branches when creating. This skill runs just the PR
  create/refresh piece — the `draft-body` + `ensure-pr` steps — without
  the reviewer + watch loop. Does NOT attach reviewers — use
  `/wise-pr-add-reviewers` for that — and does NOT watch CI — use
  `/wise-pr-watch`. Invoked as `/wise-pr-create`. Use when the user says
  "open a PR", "draft the PR body", "update the PR description", "push
  this to review", or types `/wise-pr-create`.
---

# /wise-pr-create — create or refresh a PR for the current branch

> **Shared-file resolution:** `${WISE_PLUGIN_ROOT}` defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/hermes` — where `./install.sh hermes` puts this pack. Export `WISE_PLUGIN_ROOT` only to override.

## Why this skill exists

Opening a PR is mostly mechanical: pick a base, draft a
body, run `gh pr create`. This skill is the narrowed surface for
exactly that: it runs the **draft-body** and **ensure-pr** steps
and stops, leaving reviewers (`/wise-pr-add-reviewers`) and the
CI/review watch loop (`/wise-pr-watch`) to their own commands.

Single source of truth for the drafting + creation logic:
`harnesses/claude/wise/references/pr/draft-body.md` and
`harnesses/claude/wise/references/pr/ensure-pr.md`. This skill reads both at
run time via the `Read` tool and follows them — and so do the `-auto`
PR skills and the `ticket-auto` workflow, so behaviour stays in sync
by construction. If that prose ever changes, this skill's behaviour
changes too — no duplication.

## Invocation

```
/wise-pr-create
/wise:wise-pr-create          # canonical namespaced form
```

No positionals, no flags. Anything the user types beyond the skill
name is ignored.

## Procedure

This skill does NOT probe dependencies up-front. If `gh` or `git`
isn't installed, the first command below fails with a clean
`command not found` and Claude surfaces that to the user
conversationally with a pointer at `/wise-init` — same UX as
running any other command against an incomplete environment.
Skipping the pre-flight makes the skill faster on the hot path
and removes a coupling between wise-pr-* and the workflow engine's
registry.

### 1. Detect PR state

```bash
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
PR_JSON="$(gh pr list --head "$BRANCH" --state open --json number,baseRefName,url --limit 1 2>/dev/null || echo '[]')"
CLOSED_JSON="$(gh pr list --head "$BRANCH" --state closed --json number,state,url --limit 3 2>/dev/null || echo '[]')"
```

We filter explicitly to `--state open` because `gh pr view` happily
returns closed/merged PRs when the branch has nothing open, and we
don't want to "refresh" a PR that's already been merged or abandoned.
Closed/merged PRs for this branch are captured separately in
`CLOSED_JSON` purely for a transparency note — a closed PR is not
treated as "existing" here.

Parse `PR_JSON` as a JSON array:

- Empty array → `pr_exists = no` (no OPEN PR on this branch).
- Non-empty → `pr_exists = yes`; read the first element's
  `number`, `baseRefName`, `url` into `pr_number`, `pr_base`, `pr_url`.

Then capture the five values the prompt fragments expect:

- `current_branch = $BRANCH`
- `pr_exists` — `yes` / `no` per above.
- `pr_number`, `pr_base`, `pr_url` — from the open PR when present,
  else the literal string `NONE`.

If `CLOSED_JSON` is a non-empty array AND `pr_exists = no`, print a
one-line informational note to chat naming the closed/merged PR
number(s) — e.g. `Found closed PR #1924 on this branch; drafting a
new PR instead of refreshing it.` This is purely informational; the
new-PR creation path continues unchanged.

### 1.5. Protected-branch guard

This skill does not run a dedicated `guard` step, but the same
reasoning applies: creating a PR *from* `main` / `master` / a
release branch is almost always a mistake.

- If `current_branch` matches any of `^main$`, `^master$`,
  `^release$`, or `^release[/-]`, AND `pr_exists == no`, STOP.
  Tell the user their current branch is protected and suggest
  checking out a ticket-scoped branch named exactly the ticket key
  (e.g. `git checkout -b PROJ-777`, per
  `${WISE_PLUGIN_ROOT}/references/branch-naming.md`) before re-running.
- If `current_branch` matches a protected pattern AND `pr_exists
  == yes`, continue — the user wants to refresh an existing PR
  that was created deliberately.
- Otherwise continue unconditionally.

### 2. Detect the project path

The `draft-body.md` and `ensure-pr.md` fragments expect a
`project.path` — the absolute root of the working tree. Derive it:

```bash
PROJECT_PATH="$(git rev-parse --show-toplevel)"
```

If that fails (we're not in a git repo), stop with a clear message.

### 3. Read and run draft-body.md

Read the fragment:

```
Read: ${WISE_PLUGIN_ROOT}/references/pr/draft-body.md
```

Follow its procedure with the context you collected:

- `current_branch = <BRANCH>`
- `pr_exists = yes | no`
- `pr_base = <base or NONE>`
- `project.path = <PROJECT_PATH>`

At the end of the draft-body procedure you'll have a path to a
temp file holding the drafted PR body. Remember it as `BODY_PATH`.

Print a visible note in chat stating which template resolved (the
`draft-body.md` procedure logs the winner).

### 4. Read and run ensure-pr.md

Read the fragment:

```
Read: ${WISE_PLUGIN_ROOT}/references/pr/ensure-pr.md
```

Follow its procedure with the context you now have:

- `current_branch = <BRANCH>`
- `pr_exists = yes | no`
- `pr_number = <number or NONE>`
- `pr_url = <url or NONE>`
- `pr_base = <base or NONE>`
- `pr_body_path = <BODY_PATH>`
- `project.path = <PROJECT_PATH>`

On the no-PR path the fragment uses `AskUserQuestion` to pick the
base and `gh pr create`; on the existing-PR path it uses `gh pr
edit --body-file`. Both paths end with the new/refreshed PR
number + url.

### 5. Summarise

Print three lines:

```
PR <created|refreshed>: <url>
Template used: <path that won the resolution ladder>
Body drafted at: <BODY_PATH>
```

If the user wants to attach reviewers next, point them at:

```
Attach reviewers next with:
  /wise-pr-add-reviewers
```

If the user wants to watch the PR next, point them at:

```
Watch pipelines + comments with:
  /wise-pr-watch
```

## Guardrails

- This is a **standalone slash-command skill** — independent of the
  `/wise` natural-language helper. It reads shared prompt fragments
  from `${WISE_PLUGIN_ROOT}/references/pr/` but does NOT invoke
  workflow steps or other wise action skills (that's reserved for
  `workflow-run` / `workflow-resume`).
- Never attach reviewers here — `/wise-pr-add-reviewers` owns that.
- Never start a watch loop here — `/wise-pr-watch` owns that.
- Never force-push, amend, rebase, or otherwise modify the
  commits on the branch — the draft describes what's already on
  `HEAD`.
- Never invent a Jira key or retarget an existing PR without the
  user asking. Both fragments enforce this; don't work around it.
