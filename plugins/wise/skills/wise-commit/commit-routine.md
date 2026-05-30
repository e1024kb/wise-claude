# commit-routine — shared draft + commit (+ optional push) procedure

Single source of truth for the plugin's automated-commit routine.
Read by:

- `/wise-commit` (sets `PUSH=no`).
- `/wise-commit-push` (sets `PUSH=yes`).
- `workflows/pr-interactive/prompts/commit-from-fix.md` — the
  workflow's autofix commit fragment, which adds a `fix_kind`
  classification bias and a `fix_summary` drafting hint on top
  before delegating here.

The three queue handlers (`handle-bot-reviews.md`,
`handle-human-comments.md`, `handle-sonar-issues.md`) each call
`commit-from-fix.md` for their Phase-B commit, then run a bare
`git push` in Phase D after their Phase-C remote side effects
(thread resolves, replies, Sonar status changes). That decoupling
is intentional — those Phase-D pushes do **not** delegate here.

The Jira-scope + type-classification + subject rules in §5–§7 live in
`${CLAUDE_PLUGIN_ROOT}/references/subject-drafting.md` — the shared
single source of truth that this routine, `/wise-commit-message`, and
`draft-body.md` all read. Change a drafting rule there, not here.

## Inputs the caller sets

- `PUSH` - `yes` or `no`. Controls whether step 8 runs `git push`.
  - `/wise-commit` sets `PUSH=no`.
  - `/wise-commit-push` sets `PUSH=yes`.
- `SIMPLIFY` - `yes` or `no`. Controls whether step 2 runs the
  code-simplifier pass before staging, so its edits land in the
  same commit. Default `yes`, so every caller that omits the input gets
  the per-commit cleanup for free. The invocation itself lives in
  `${CLAUDE_PLUGIN_ROOT}/references/simplify-pass.md` — the single
  source of truth this routine shares with the implement phase and
  `/wise-simplify-auto`.
  - `/wise-commit` and `/wise-commit-push` default to `SIMPLIFY=yes`.
  - `commit-from-fix.md` (workflow autofix commits) **also** passes
    `SIMPLIFY=yes` — every commit, autofix included, gets the cleanup.
  - `SIMPLIFY=no` is for callers that already ran simplify
    (`/wise-simplify-auto`) or that explicitly want a raw commit (the
    code-review gate's fix commit via `/wise-code-review-auto`).

No other inputs. The routine reads everything else from `git` and
the live conversation context.

## Procedure

### 1. Verify we're inside a git working tree

```bash
git rev-parse --show-toplevel
```

If exit code ≠ 0, stop with the final line:

```
COMMIT: failed reason="not inside a git working tree"
```

### 2. Simplify recently-modified code (when `SIMPLIFY=yes`)

**Skip this step entirely if `SIMPLIFY=no`.** That's the carve-out for
callers that already ran simplify themselves (`/wise-simplify-auto`) or
that explicitly want a raw commit (the code-review gate's fix commit
via `/wise-code-review-auto`).

Otherwise, before staging, run the per-commit cleanup per
`${CLAUDE_PLUGIN_ROOT}/references/simplify-pass.md` — it dispatches the
`code-simplifier` agent over the recently-modified code so its edits get
swept into this commit by §3's `git add -A`. Surface its summary
verbatim and continue; the summary is mid-flight diagnostics, not a stop point.

On a simplify failure, follow that reference's failure policy and stop
with:

```
COMMIT: failed reason="simplify errored: <one-line summary>"
```

Do **not** retry, stage what was simplified, or invent a recovery — same
policy as the `git commit` / `git push` failure paths. The pass runs
before staging so §3's `git add -A` sweeps its edits into the commit;
the routine does not re-run typecheck/lint afterwards — the project's
pre-commit hook (§7) is the final guard. One simplify pass, one commit.

### 3. Stage all working-tree changes (always `git add -A`)

```bash
git add -A
```

This is the routine's contract: every working-tree change — tracked
modifications, deletions, AND new untracked files — gets swept into
the commit unconditionally, regardless of what the user pre-staged.
The whole point of `/wise-commit` (and any orchestrator that wraps
it) is one-call drafting + commit; making the operator hand-stage
new files before invoking it would defeat that — and an orchestrator
chain can produce new files (the §2 simplify pass extracts a
helper, an agent team adds regression tests) that the operator
hasn't seen yet, so a "tracked-only" stage would silently drop
those.

Users who want a partial-staging workflow run `git commit` directly
instead.

**Files the operator never wants committed must be in `.gitignore`.**
That's where the "what stays out of commits" decision lives, not
inline in the routine. If `.env`, build artefacts, IDE droppings,
or other local-only files show up as untracked instead of ignored,
that's a `.gitignore` gap to fix — the routine commits what it
sees. Pushing the wrong file is recoverable (`git revert`,
`git reset --soft HEAD~1`, `git rm --cached`); silently dropping
a chain-produced file is not, because the operator didn't know it
existed.

### 4. Verify something is actually staged after the add

```bash
git diff --cached --quiet && INDEX_EMPTY=yes
```

If the index is empty after `git add -A`, the skill's outcome
depends on `PUSH`:

- **`PUSH=no`** (the `/wise-commit` case) — there's nothing to do.
  Stop with:
  ```
  COMMIT: skip reason="nothing to commit"
  ```

- **`PUSH=yes`** (the `/wise-commit-push` case) — the operator may
  be running this to push commits already landed locally (e.g. a
  previous skill committed and they want to push the result). Check
  whether the local branch is ahead of
  its tracked upstream:

  ```bash
  AHEAD=$(git rev-list --count @{u}..HEAD 2>/dev/null || echo "no-upstream")
  ```

  - `AHEAD == 0` (upstream resolves and we're not ahead) — truly
    nothing to do. Stop with:
    ```
    COMMIT: skip reason="nothing to commit and nothing to push"
    ```
  - `AHEAD > 0` — there are local commits to push. **Skip §5–§7
    (no draft, no commit) and jump to §8 (push).** §9 will use the
    existing `git log -1 --pretty=%s` for the `subject="…"` field
    in the `COMMIT: ok` line — that's the commit landing on the
    remote, the truthful subject to report. Set a flag the rest
    of the routine can check (`PUSH_ONLY=yes`) so §7's commit step
    is skipped.
  - `AHEAD == "no-upstream"` — upstream isn't configured. Skip
    §5–§7 and jump to §8. §8b will refuse cleanly with the
    `git push -u origin <branch>` hint; that's the right outcome
    here (operator decides whether to set tracking).

Do not invent a no-op commit; do not auto-stage untracked files; do
not prompt.

### 5. Detect a Jira ticket key

Follow `${CLAUDE_PLUGIN_ROOT}/references/subject-drafting.md` §1. Your
change set is the staged index — grep `git diff --cached` for source 2.
Run the chain in full before drafting; never skip straight to §6.

### 6. Classify the change

Read the staged diff for context (after §3 the index contains
everything the commit will include):

```bash
git diff --cached --unified=0 --no-color
```

The unified diff carries the file list in its `+++ b/<path>` headers,
so a separate `--stat` call is redundant. Pick the type per
`${CLAUDE_PLUGIN_ROOT}/references/subject-drafting.md` §2.

### 7. Write the subject and commit

**Skip this entire section if `PUSH_ONLY=yes`** — there's nothing
to commit; we're only pushing existing local commits. §9 will use
the existing HEAD commit's subject for the final line. Continue
straight to §8.

Draft the subject per `${CLAUDE_PLUGIN_ROOT}/references/subject-drafting.md`
§3 (format with/without Jira key, imperative, lowercase, ≤72 chars, no
attribution trailer). Then commit:

```bash
SUBJECT="<the drafted subject>"
git commit -m "$SUBJECT"
```

If `git commit` fails (pre-commit hook reject, signing error, …),
stop with:

```
COMMIT: failed reason="<verbatim git error, one line>"
```

Do **not** retry, do **not** `--no-verify`, do **not** amend. A
failed pre-commit hook means the commit didn't happen — investigate
the hook output, don't bypass it.

### 8. Push (only if `PUSH=yes`)

If `PUSH=no` (the `/wise-commit` case), skip this section entirely.

If `PUSH=yes` (the `/wise-commit-push` case):

**8a. Refuse main / master / detached HEAD.**

```bash
BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null) || BRANCH=""
```

- `BRANCH` is empty → detached HEAD. There's no branch to push;
  emit and stop:
  ```
  COMMIT: failed reason="detached HEAD; check out a branch before pushing"
  ```
- `BRANCH` is `main` or `master` → commit has landed locally but the
  push is refused. Emit and stop:
  ```
  COMMIT: failed reason="refuse to push to $BRANCH; push manually if intentional"
  ```

The main/master refuse matches the global `~/.claude/CLAUDE.md` rule
against unauthorized force / direct main pushes. The detached-HEAD
refuse catches a state where `git push` would fail anyway, but with
a less actionable error than this one.

In both cases the local commit stays; the operator decides whether
the situation is intentional.

**8b. Verify upstream is configured.**

```bash
git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null
```

If exit ≠ 0, no upstream is set. Don't auto-set it — that's a
decision the user owns. Emit:

```
COMMIT: failed reason="no upstream configured for $BRANCH; run 'git push -u origin $BRANCH' once to set it"
```

**8c. Push.**

```bash
git push
```

If `git push` fails (non-fast-forward, auth, hook reject), stop with
the verbatim git error:

```
COMMIT: failed reason="<verbatim git error, one line>"
```

Do **not** retry, do **not** `--force`, do **not** `--force-with-lease`,
do **not** `--no-verify`, do **not** auto-`git pull --rebase`. Push
failures get surfaced to the operator.

### 9. Emit the final line

The routine's final line — alone on its own line — MUST match one
of:

```
COMMIT: ok subject="<subject>"                  # PUSH=yes — commit + push both landed
COMMIT: ok subject="<subject>" pushed=no        # PUSH=no — commit landed, no push attempted
COMMIT: skip reason="<reason>"
COMMIT: failed reason="<reason>"
```

For the `PUSH_ONLY=yes` case (§4 found nothing to commit but the
local branch was ahead of upstream and §8 successfully pushed
those existing commits), `<subject>` is the existing HEAD commit's
subject (`git log -1 --pretty=%s`) — that's what landed on the
remote. The shape stays `COMMIT: ok subject="<subject>"`, no
extra fields, so the watch-loop parser doesn't need to change.

This shape matches `commit-from-fix.md`'s contract so the
`pr-interactive` watch loop's parser keeps working through every
path.

Examples:

```
COMMIT: ok subject="feat(PROJ-812): add a11y audit panel"
COMMIT: ok subject="fix: prevent null deref in IndexController" pushed=no
COMMIT: skip reason="nothing to commit"
COMMIT: failed reason="refuse to push to main; push manually if intentional"
COMMIT: failed reason="git push rejected (non-fast-forward)"
```

## Guardrails

- Never invent a Jira key. Unscoped > wrong-scoped.
- Never bypass `.gitignore` to commit ignored files. The routine
  uses `git add -A`, sweeping in everything not ignored — keep
  `.gitignore` accurate to keep commits clean.
- Never `git commit --amend` an already-pushed commit — always
  create new commits on top.
- Never `git push --force` / `--force-with-lease`.
- Never skip hooks (`--no-verify`).
- Never push to `main` or `master` from this routine.
- Never append an AI attribution trailer to the subject.
- Never retry on commit or push failure — surface the error and
  stop.
- Simplify failure aborts the commit (§2). Same no-retry / no-bypass
  policy as the `git commit` and `git push` steps; callers that want
  to skip the simplify pass entirely pass `SIMPLIFY=no` at the routine
  boundary.
- Simplify never re-validates inside the routine. The pre-commit hook
  in §7 is the final guard for whatever the simplify pass edits; a
  longer validation chain (typecheck / lint / format) is an orchestrator
  skill's job, not this routine's. One simplify pass, one commit — §2
  does not re-run the simplify pass to iterate-to-clean.
