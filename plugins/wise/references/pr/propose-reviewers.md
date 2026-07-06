# propose-reviewers — analyse the PR + surface Claude-picked candidates

This fragment runs only when the user picked **Yes — Claude
proposes candidates** in `ensure-reviewers.md` (the preceding
step records `extras_choice=yes`). Its job is to turn the raw
"who in the org should review this?" question into a short list
of specific people the user can click to add.

Used by:
- `/wise-pr-add-reviewers` standalone skill, after it runs
  `ensure-reviewers.md` and the user picked `yes`.

## Context the caller supplies

- `pr_number` — PR number.
- `pr_url` — PR url (informational only).
- `pr_base` — the PR's base branch (used to scope `git log`).
- `project.path` — absolute path to the repo working tree.
- `defaults_attached` — comma-separated list of slugs already on
  the PR from the defaults step (informational; used only to
  exclude them from the candidate pool so we don't re-propose a
  reviewer who is already attached).

## Procedure

Run all `gh` / `git` commands with `cd <project.path>` first.

### 1. Gather PR context

```bash
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/wise-pr-XXXXXX")"

# Files changed in the PR
gh pr view <pr_number> --json files --jq '.files[].path' > "$SCRATCH/pr-files.txt"

# PR author — exclude from candidates (self-review isn't useful)
PR_AUTHOR="$(gh pr view <pr_number> --json author --jq .author.login)"
```

If `"$SCRATCH/pr-files.txt"` is empty (empty PR), skip to §4 and propose
nothing — there's nothing to rank against.

### 2. Build the candidate pool

Three signals, in priority order:

#### 2a. CODEOWNERS (if present)

```bash
BASE="<pr_base>"
# Check the PR's base branch for a CODEOWNERS file
for p in .github/CODEOWNERS CODEOWNERS docs/CODEOWNERS; do
  if git show "origin/$BASE:$p" >/dev/null 2>&1; then
    git show "origin/$BASE:$p" > "$SCRATCH/CODEOWNERS"
    break
  fi
done
```

If `"$SCRATCH/CODEOWNERS"` was produced, parse it and match each line's
glob pattern against the PR's changed files. The logins / team
slugs after the pattern become high-weight candidates (they're
the project's declared owners — strong prior).

CODEOWNERS syntax reminder: lines like `js/components/** @user1 @org/team`.
`*` is glob, `**` is recursive, `#` is comment, empty lines are
ignored.

#### 2b. Recent per-file authors

For each changed file (up to 20 — cap to keep the API calls
bounded), fetch recent commit authors:

```bash
# Last 10 commits touching the file, with GitHub logins
gh api "repos/:owner/:repo/commits?path=<file>&per_page=10" \
  --jq '.[].author.login' 2>/dev/null | grep -v '^$' | sort -u
```

Tally login frequency across all changed files. Excludes commits
with no `author.login` (email-only commits or anonymous).

#### 2c. Org-member intersection

Resolve the PR's owning org from `gh repo view --json owner --jq .owner.login`,
then list its members:

```bash
ORG="$(gh repo view --json owner --jq .owner.login)"
gh api "orgs/$ORG/members" --paginate --jq '.[].login' | sort -u > "$SCRATCH/org-members.txt"
```

Intersect the combined candidate pool (§2a + §2b) with
`"$SCRATCH/org-members.txt"`. Drop anyone NOT in the org — a PR should
have reviewers from the repo's own org. If the org-members call
fails (e.g. the token lacks org-read scope), skip this filter
rather than failing the step.

Also drop:
- `PR_AUTHOR` (no self-review).
- Any slug already in `defaults_attached` (no re-request).
- Any bot (login ending in `[bot]` or matching known bot names
  like `dependabot`, `github-actions`).

### 3. Rank and select top 3

Scoring:
- CODEOWNERS match → +3 per matched file (strong signal: explicit
  declaration).
- Per-file author → +1 per file they've committed to recently.

Pick the **top 3** by score. Tiebreak by login alphabetically.

If fewer than 3 candidates emerged, that's fine — show whatever you
have. If zero candidates emerged (empty CODEOWNERS, no file
history, tiny org), skip the AskUserQuestion entirely, `rm -rf
"$SCRATCH"`, and emit `EXTRAS: attached=NONE-no-candidates` (the step
still completes cleanly — the user just didn't get suggestions).

### 4. Present picks via AskUserQuestion

For each candidate, compose a rationale ≤80 chars explaining
*why* they're a good pick. Examples:
- `CODEOWNERS for js/components/**`
- `authored 4 commits in AuditPanel.tsx`
- `recent reviewer on PR #1890 (same area)`

Use `AskUserQuestion`:

- question: `Claude suggests these reviewers based on the PR's changed files + CODEOWNERS + recent authors. Pick any you'd like to request.`
- header: `Reviewers`
- multiSelect: **true** — user can pick multiple.
- options (up to 4, at least 2):
  - Option per candidate: label `<login>`, description
    `<rationale>`.
  - If you have fewer than 3 candidates, pad with a final option
    `None of these` (description: `Don't add any extras — keep the
    defaults only.`) so AskUserQuestion's 2-option minimum holds.

AskUserQuestion also supplies an `Other` free-text affordance the
user can use to add a login Claude didn't propose — that's
always available even when `multiSelect=true`. The user can type
one login or a comma-separated list; we'll parse both shapes.

### 5. Apply the picks

Collect the user's selections into a final list `PICKED`:
- Selected options (one or more logins from §4).
- Any freetext from `Other`: split on commas, trim whitespace per
  entry, keep only entries matching `^[A-Za-z0-9][A-Za-z0-9-]*$`.
  Warn on invalid entries in the step prose (don't fail).
- De-duplicate.

Drop any entries that ended up already on the PR between §1 and
now (idempotency — a reviewer added via another tool in parallel
shouldn't be re-requested).

Attach each:

```bash
for LOGIN in $PICKED; do
  gh pr edit <pr_number> --add-reviewer "$LOGIN"
done
```

Log each outcome (added / already-present / failed — e.g. "user
doesn't have repo access") in the step prose.

### 6. Emit the final line

Any path that reaches this section without already cleaning up
(the normal candidates-picked completion) must `rm -rf "$SCRATCH"`
here before emitting.

Your response's FINAL line — alone on its own line, no markdown,
no backticks — MUST match:

```
EXTRAS: attached=<comma-separated-logins-or-NONE>
```

Examples:

```
EXTRAS: attached=ikhilko,jlevdev
EXTRAS: attached=NONE-user-picked-none
EXTRAS: attached=NONE-no-candidates
```

Use the suffixed `NONE-*` forms so the `report` step can tell why
nothing was attached.

## Guardrails

- Never propose the PR author as a reviewer.
- Never propose known bots.
- Prefer logins in the repo's owning org; never propose a login
  outside it unless the org-members lookup was unavailable.
- Never re-request a reviewer already on the PR.
- Never invent logins — only propose entries that came from a real
  signal (CODEOWNERS, git history, or the user's Other-field
  input).
- If candidate ranking is tied and ambiguous, include a short
  paragraph in the step prose explaining *why* these three (so the
  user can override if Claude's reasoning is off). Transparency
  over magic.
- `rm -rf "$SCRATCH"` before EVERY exit — the final line (§6, which
  also covers the normal candidates-picked completion) and the
  no-candidates early exit (§3). Neither may leave the scratch dir
  behind.
