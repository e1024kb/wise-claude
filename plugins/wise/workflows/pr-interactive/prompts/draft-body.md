# draft-body — gather changes, resolve template, draft PR body

This prompt fragment is the source of truth for the `draft-body`
step of the `pr-interactive` workflow AND for the standalone
`/wise-pr-create` skill. Both read this file at run time and follow
the procedure below.

## Context the caller supplies

The caller passes these values as inline context (templated from
workflow state or derived on the fly by the standalone skill):

- `current_branch` — current git branch name.
- `pr_exists` — `yes` / `no`. When `yes`, a PR already exists; its
  body will be refreshed.
- `pr_base` — the existing PR's base branch (only when `pr_exists=yes`;
  otherwise `NONE`).
- `project.path` — absolute path to the repo working tree.
- `workflow.dir` — absolute path to the workflow folder (empty when
  invoked as a standalone skill; the skill substitutes its own
  `${CLAUDE_PLUGIN_ROOT}/workflows/pr-interactive`).

## Procedure

### 1. Determine the diff base

```bash
BASE="<pr_base from caller>"
if [ "$BASE" = "NONE" ] || [ -z "$BASE" ]; then
  # No existing PR — best-effort default to the repo's default branch.
  BASE="$(cd <project.path> && gh repo view --json defaultBranchRef --jq .defaultBranchRef.name 2>/dev/null)"
  [ -z "$BASE" ] && BASE=main
fi
```

### 2. Collect change data

All commands run with `cd <project.path>` first.

```bash
git fetch origin "$BASE" >/dev/null 2>&1 || true

# Prefer origin/<base>; fall back to local <base> if origin isn't there.
REF="origin/$BASE"
git rev-parse --verify "$REF" >/dev/null 2>&1 || REF="$BASE"

git log "$REF..HEAD" --oneline
git diff "$REF...HEAD" --stat
git diff "$REF...HEAD" --unified=0 --no-color | head -400
```

If either the log or diff is empty, note that in the Summary section
of the body (the PR is a stub) and continue — don't abort.

### 3. Detect a Jira key

Follow `${CLAUDE_PLUGIN_ROOT}/references/subject-drafting.md` §1 — the
shared first-match-wins chain (branch → the §2 diff content → `git log
-5 --pretty=%s` → session), no invention. If none yield a key, omit the
scope in the Context section.

### 4. Resolve the PR-body template

Walk this ladder under `<project.path>` in order; first hit wins:

```bash
for P in \
  "<project.path>/.github/pull_request_template.md" \
  "<project.path>/.github/PULL_REQUEST_TEMPLATE.md" \
  "<project.path>/docs/pull_request_template.md"; do
  [ -f "$P" ] && echo "$P" && exit 0
done

# Fallback: directory of per-kind templates. Prefer default.md if it
# exists, else first alphabetically.
DIR="<project.path>/.github/PULL_REQUEST_TEMPLATE"
if [ -d "$DIR" ]; then
  [ -f "$DIR/default.md" ] && echo "$DIR/default.md" && exit 0
  FIRST="$(ls "$DIR"/*.md 2>/dev/null | head -1)"
  [ -n "$FIRST" ] && echo "$FIRST" && exit 0
fi

# Final fallback: workflow-shipped template.
echo "<workflow.dir>/templates/pr-template.md"
```

Record which template won — include its path in a visible note at
the top of your final summary line (see §7) so the user can tell
whether their project override was picked up.

### 5. Fill the template

Read the selected template via the Read tool. For each section
heading present in the template, generate a populated version:

- **Summary** — 1–3 short bullets capturing the dominant change.
  Imperative voice ("add X", "fix Y"), lowercase-first after the
  bullet.
- **Context** — the Jira key as a linked ticket if detected
  (`https://your-org.atlassian.net/browse/<KEY>`), any
  related PR or design link surfaced from the diff or branch
  prose. Write "none surfaced" if nothing is applicable.
- **Changes** — one bullet per coherent change, grouped by area if
  helpful. Reference concrete file names only when they aid the
  reader.
- **Testing** — tick boxes the change warrants; leave boxes
  unticked that the author still needs to perform. Add lines when
  the template's default list is incomplete for this change.
- **Screenshots** — omit the section if no UI files changed; keep a
  placeholder `<before / after>` otherwise.
- **Risk & rollout** — flag feature flags, data migrations, dep
  bumps, breaking changes. Write "no rollout considerations" if
  truly none.

Preserve section headings verbatim (the user may have custom
headings their team reviews for). Do NOT invent new sections if
the template doesn't have them. Do NOT drop sections whose content
you can't fill — leave a short TODO bullet so the author sees the
gap.

Do not add a Claude / AI attribution line. The PR is the user's.

### 6. Write to a temp file

Pick a deterministic-per-run path under `/tmp` so `ensure-pr` can
pipe the file into `gh`:

```bash
BODY_PATH="/tmp/pr-interactive-body-$(date +%s)-$$.md"
cat > "$BODY_PATH" <<'PR_BODY_EOF'
<the filled template, verbatim>
PR_BODY_EOF
```

Verify with `head -3 "$BODY_PATH"` that the header of the body
landed. If the write failed for any reason, stop and report.

### 7. Emit the final line

Your response's FINAL line — alone on its own line, no markdown,
no backticks — MUST match:

```
DRAFT: body_path=<BODY_PATH>
```

Example:

```
DRAFT: body_path=/tmp/pr-interactive-body-1714123456-78910.md
```

The engine captures the path via the until regex and records it
as the `pr_body_path` output. `ensure-pr` reads it.

## Guardrails

- Never invent a Jira key.
- Never modify the repo or the template file. This step is
  strictly read-only against the working tree.
- Never write anywhere outside `/tmp`. The body file is transient
  and may be cleaned up between runs.
- When the template is the workflow-shipped fallback
  (`<workflow.dir>/templates/pr-template.md`), say so explicitly
  in your prose output so the user can drop a project-level
  template and supersede the fallback next time.
