# ensure-pr — create or refresh the PR

This prompt fragment is the source of truth for the create-or-update
logic of the standalone `/wise-pr-create` skill (the `ticket-auto`
workflow uses an autonomous analogue, `ensure-pr-auto.md`).

## Context the caller supplies

- `current_branch` — current git branch name.
- `pr_exists` — `yes` / `no` from the earlier detection step.
- `pr_number` — existing PR number (when `pr_exists=yes`), else `NONE`.
- `pr_url` — existing PR url (when `pr_exists=yes`), else `NONE`.
- `pr_base` — existing PR base (when `pr_exists=yes`), else `NONE`.
- `pr_body_path` — absolute path to the drafted PR body written by
  the preceding `draft-body` step.
- `project.path` — absolute path to the repo working tree.

## Procedure

Run all `gh` / `git` commands with `cd <project.path>` first.

### 1. Sanity-check the body file

```bash
test -f "<pr_body_path>" && head -1 "<pr_body_path>"
```

If the file is missing or empty, stop and report — `draft-body`
failed and nothing downstream should proceed.

### 2. Branch on pr_exists

#### 2a. `pr_exists = yes` → refresh the body

```bash
gh pr edit "<pr_number>" --body-file "<pr_body_path>"
```

Do NOT change the base branch, the title, or any other field —
this path exists purely to keep the body in sync with the current
diff. If the user wants to rename or retarget the PR, they do that
by hand.

The response URL comes back from `gh pr view <pr_number> --json url --jq .url`.

Skip to §3.

#### 2b. `pr_exists = no` → create a new PR

##### i. Fetch release branches for the base picker

Pull up to 5 release branches sorted by latest commit date:

```bash
gh api repos/:owner/:repo/branches?per_page=100 --paginate \
  --jq '[.[] | select(.name | test("^release([/-]|$)")) | .name]' \
| python3 -c '
import json, subprocess, sys
names = json.loads(sys.stdin.read())
dated = []
for n in names:
    try:
        d = subprocess.check_output(
            ["gh","api",f"repos/:owner/:repo/commits/{n}","--jq",".commit.committer.date"],
            text=True).strip()
        dated.append((d, n))
    except Exception:
        pass
for _, n in sorted(dated, reverse=True)[:5]:
    print(n)
'
```

(A simpler approximation: `gh api repos/:owner/:repo/branches --jq '.[].name' | grep -E '^release([/-]|$)' | head -5` — good enough when the repo doesn't have hundreds of release branches.)

##### ii. Ask the user to pick the base

Use `AskUserQuestion`:

- question: `Target base branch for the new PR on <current_branch>?`
- header: `Base branch` (≤12 chars)
- options: the repo's default branch first (usually `main`), then
  each release branch from §i as its own option, up to the 4-option
  cap. If you have more than 3 release branches, put `main` as
  option 1 and the 3 most recent release branches as options 2–4;
  let the user pick `Other` and type a branch name for anything
  else.
- multiSelect: false

##### iii. Derive a PR title

First line of the body becomes the title if it's under 90 chars
and doesn't start with `#` (markdown heading). Otherwise:

```
<latest commit subject on this branch>
```

Strip trailing punctuation. Keep it under 90 chars.

##### iv. Create the PR

```bash
TITLE="<derived title>"
BASE="<user's pick>"
gh pr create \
  --base "$BASE" \
  --head "<current_branch>" \
  --title "$TITLE" \
  --body-file "<pr_body_path>"
```

`gh pr create` prints the new PR url on success. Capture it.

##### v. Fetch the PR number

```bash
gh pr view --json number --jq .number
```

### 3. Emit the final line

Your response's FINAL line — alone on its own line, no markdown,
no backticks — MUST match exactly:

```
PR-CREATE: number=<number> url=<url>
```

Example:

```
PR-CREATE: number=142 url=https://github.com/owner/repo/pull/142
```

The engine captures both values via the until regex and updates
the run's `pr_number` and `pr_url` outputs (overriding any NONE
values the initial detect step recorded).

## Guardrails

- Never force-push, amend, or rebase. The PR you refresh or create
  uses exactly the commits already on `<current_branch>`.
- Never pick the base branch without asking — even when there's
  only one sensible option. The user picks.
- Never retarget an existing PR. If the user wants a different
  base on an existing PR, they do that by hand.
- Never invent a PR title. Derive it from the body's first line or
  the latest commit subject.
- Do NOT append a Claude / AI attribution trailer to the body.
