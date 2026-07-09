---
name: wise-pr-add-reviewers
description: >-
  Attach Copilot code review to the PR for the current branch, then prompt
  the user for individual reviewers (free-text comma-separated logins,
  with CODEOWNERS-derived candidates and org members shown inline for
  reference). Idempotent — already-requested reviewers are detected and
  not re-requested. This skill runs just the reviewer attach step on an
  existing PR. Fails with a clear message if the current branch has no
  open PR — run `/wise-pr-create` first. Invoked as
  `/wise-pr-add-reviewers`. Use when the user says "add reviewers",
  "request review", "ping Copilot", or types `/wise-pr-add-reviewers`.
---

# /wise-pr-add-reviewers — attach reviewers to the current branch's PR

> **Shared-file resolution:** `${WISE_PLUGIN_ROOT}` defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/pi` — where `./install.sh pi` puts this pack. Export `WISE_PLUGIN_ROOT` only to override. If neither the env var nor the default path exists, use the pack this skill ships in — two levels up from the directory containing this SKILL.md, i.e. `../../` from it (a `pi install`ed pack stays intact).

## Why this skill exists

Most PRs end up requesting Copilot code review plus 0–N individuals
picked from CODEOWNERS or the org. This skill is the narrowed
surface — just the reviewer attach step, on a PR that already exists
(`/wise-pr-create` makes the PR; `/wise-pr-watch` drives CI).

Single source of truth for the reviewer logic:
`harnesses/claude/wise/references/pr/ensure-reviewers.md`. This skill reads it
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

### 3. Read and run ensure-reviewers.md (defaults + ask about extras)

Read the fragment:

```
Read: ${WISE_PLUGIN_ROOT}/references/pr/ensure-reviewers.md
```

Follow its procedure with the context:

- `pr_number = <number>`
- `pr_url = <url>`
- `project.path = <PROJECT_PATH>`

The fragment attaches Copilot (idempotent), asks via `AskUserQuestion`
whether to add extra reviewers, and reverts its own defaults if the
user picks `Skip`. Capture its final line and parse `DEFAULTS_ATTACHED`
/ `EXTRAS_CHOICE`:

```
REVIEWERS: attached=<slugs-or-NONE> extras=<no|yes|skip>
```

### 4. If extras_choice is `yes`, run propose-reviewers.md

Conditional on `EXTRAS_CHOICE == 'yes'`. Read:

```
Read: ${WISE_PLUGIN_ROOT}/references/pr/propose-reviewers.md
```

Follow its procedure with the context:

- `pr_number = <number>`
- `pr_url = <url>`
- `pr_base = <gh pr view --json baseRefName --jq .baseRefName>`
- `project.path = <PROJECT_PATH>`
- `defaults_attached = <DEFAULTS_ATTACHED>`

The fragment ranks reviewer candidates (changed files, CODEOWNERS,
recent authors ∩ org members) and surfaces them as **multi-select**
`AskUserQuestion` picks (plus an Other freetext field), then attaches
the chosen logins via `gh pr edit --add-reviewer`. Capture its final
line and parse `EXTRAS_ATTACHED`:

```
EXTRAS: attached=<comma-separated-logins-or-NONE>
```

When `EXTRAS_CHOICE != 'yes'` (i.e., `no` or `skip`), skip this
step — there are no extras to propose.

### 5. Summarise

Print the final list of reviewers on the PR. Two cases:

- `EXTRAS_CHOICE == 'yes'` → concatenate defaults + extras:
  `Reviewers attached: <DEFAULTS_ATTACHED>, <EXTRAS_ATTACHED> on <pr_url>`
- Else → just defaults:
  `Reviewers attached: <DEFAULTS_ATTACHED> on <pr_url>`
  (or `no reviewers attached` when the user picked `Skip`)

If the user wants to watch pipelines next, point them at:

```
Watch pipelines + comments with:
  /wise-pr-watch
```

## Guardrails

- This is a **standalone slash-command skill**, independent of the
  `/wise` natural-language helper. It reads a shared prompt fragment
  but does NOT invoke other wise action skills.
- Never create a PR here — bail with the pointer at
  `/wise-pr-create` if the branch has no open PR.
- Never remove a reviewer the user didn't ask to remove. The
  fragment's `Skip` path reverts only the defaults *this skill*
  added.
- Never re-request a reviewer who's already on the PR — noisy on
  the PR's activity log.
- Never block on Copilot-attach failures — the fragment tolerates
  them with a clear log line; users whose orgs don't enable
  Copilot code review can still use this skill.
