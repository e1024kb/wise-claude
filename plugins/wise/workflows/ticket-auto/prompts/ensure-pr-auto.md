# ensure-pr-auto — create or refresh a PR, autonomously

Autonomous analogue of `pr-interactive/prompts/ensure-pr.md`. Same
create-or-refresh logic, but the base branch is **chosen
autonomously** (the repo's default branch) instead of asked — this
fragment NEVER calls `AskUserQuestion`.

Source of truth for the `/wise-pr-create-auto` skill and the
`ticket-auto` workflow's PR-create step.

## Context the caller supplies

- `current_branch` — the head branch the PR is for.
- `project.path` — absolute path to the repo working tree (a ticket
  worktree, when called from `ticket-auto`).

## Procedure

Run all `gh` / `git` commands with `cd <project.path>` first.

### 1. Detect PR state

```bash
gh pr list --head "<current_branch>" --state open --json number,url,baseRefName --limit 1
```

Empty array → no open PR (`pr_exists=no`). Otherwise read `number`,
`url`, `baseRefName` from the first element (`pr_exists=yes`).

### 2. Draft the PR body

Read `${CLAUDE_PLUGIN_ROOT}/workflows/pr-interactive/prompts/draft-body.md`
and follow it with: `current_branch`, `pr_exists`, `pr_base` (the
detected base or `NONE`), `project.path`. It writes the drafted body
to a temp file and returns its path — capture it as `pr_body_path`.
If `draft-body` fails, stop and report; nothing downstream proceeds.

### 3. Branch on pr_exists

**`pr_exists = yes` → refresh the body** (do not retarget / rename):

```bash
gh pr edit "<pr_number>" --body-file "<pr_body_path>"
```

**`pr_exists = no` → create the PR.** The base is the repo's default
branch — no picker, no prompt:

```bash
BASE="$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name)"
```

Derive a title: the body's first line when it is < 90 chars and not
a markdown heading; otherwise the latest commit subject on
`<current_branch>` (trailing punctuation stripped, ≤ 90 chars). Then:

```bash
gh pr create --base "$BASE" --head "<current_branch>" \
  --title "$TITLE" --body-file "<pr_body_path>"
```

Capture the printed PR url; read the number with
`gh pr view --json number --jq .number`. Record the chosen base as
an assumption (the autonomous run picked it).

### 4. Emit the final line

FINAL line — alone, no markdown, no backticks — MUST match:

```
PR-CREATE: number=<number> url=<url>
```

## Guardrails

- Never force-push, amend, or rebase — the PR uses exactly the
  commits already on `<current_branch>`.
- Never retarget an existing PR's base.
- Never append an AI-attribution trailer to the body.
- If `<current_branch>` is a protected branch (`main` / `master` /
  `release*`) and no PR exists, do not create one — stop with a
  fatal line explaining a feature branch is required.
