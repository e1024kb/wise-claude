# commit-from-fix — autofix-aware delegate over commit-routine.md

Shared procedure for drafting a Conventional-Commits subject line
and committing it after an automated fix during the watch loop
(lint / sonar / tests / other). This prompt is a **thin wrapper**
over the canonical commit routine at
`skills/wise-commit/commit-routine.md`. The routine owns
the staging, drafting, committing, push guards, and final-line
emit format. This file adds two things on top:

1. **Autofix classification bias** — `fix_kind` (lint / sonar /
   tests / other) hints which Conventional-Commits type to prefer
   when the diff is genuinely ambiguous. The routine's general
   classification table (§6) still wins on substance — this is
   guidance for the LLM's tie-breaking, not an override.
2. **`fix_summary` context** — a one-sentence description of the
   fix the autofix produced, so the LLM has more to work with than
   the diff alone when drafting the subject.

If you change the staging policy, drafting heuristics, push guards,
or final-line format, edit `commit-routine.md`, not this file. This
file should stay a thin wrapper.

## Context the caller supplies

- `project.path` — absolute path to the repo working tree.
- `fix_kind` — one of `lint`, `sonar`, `tests`, `other`. Biases the
  classification in §1.
- `fix_summary` — one short sentence describing what was fixed
  (e.g. "removed unused import in SectionQuery.php", "sorted
  `Testing` section bullets", "silenced unused-variable lint rule
  via `// eslint-disable-next-line`"). Pass to the routine as
  drafting context for §7.
- `push` — `yes` (default) or `no`. Maps directly to the routine's
  `PUSH` input. The reviewable-queue handlers
  (`handle-bot-reviews.md`, `handle-human-comments.md`,
  `handle-sonar-issues.md`) all pass `push=no` because they batch
  remote side effects (thread-resolves, replies, Sonar status
  changes) between the local commit and a single push at the end
  of the queue.

The caller does NOT pass `SIMPLIFY`. This file hard-codes
`SIMPLIFY=yes` when invoking the routine — every commit gets the
per-commit simplify cleanup, autofix commits included. The pass
edits recently-modified code in place before staging, so the autofix
and its cleanup land together in one commit. (The heavier
code-review branch gate is a separate, once-per-branch step — it is
**not** part of an autofix commit.)

## Procedure

### 1. Apply the autofix classification bias

When the routine reaches §6 ("Classify the change"), prefer the
type below for the supplied `fix_kind` *unless the diff content
clearly says otherwise*:

| fix_kind | Preferred type (unless the diff clearly says otherwise) |
|---|---|
| `lint` | `style` if the diff is whitespace/import-order only; `refactor` if it restructures; `fix` if it added a guard clause to silence a real warning |
| `sonar` | `fix` if the issue was a bug; `refactor` for maintainability issues; `chore` for suppression annotations |
| `tests` | `test` if only test files moved; `fix` if a test uncovered a real bug the commit also patches |
| `other` | classify by diff content using the routine's general table — `feat` > `fix` > `refactor` > `perf` > `test` > `docs` > `style` > `build` > `ci` > `chore` |

This is a hint, not an override. The routine's §6 table still
applies; the bias only matters when two types are equally
defensible.

### 2. Use `fix_summary` as drafting context

When the routine reaches §7 ("Write the subject"), incorporate
`fix_summary` into the subject prose. The summary is a phrase the
caller already knows is true about the diff, so the subject can
lean on it instead of re-reading the diff to summarize.

### 3. Read and follow `commit-routine.md`

Read `${WISE_PLUGIN_ROOT}/skills/wise-commit/commit-routine.md` and
follow it end-to-end with `PUSH=$push` and `SIMPLIFY=yes`.

The routine handles:

- §1: verify git working tree.
- §2: runs the simplify pass (`SIMPLIFY=yes`) over the autofix's
  edits before staging, so the cleanup lands in the same commit.
- §3: `git add -A` (every working-tree change - modifications,
  deletions, and new untracked files).
- §4: skip if nothing landed in the index after staging.
- §5: Jira-key detection (branch / diff / log / session).
- §6: classification — apply the §1 bias above when ambiguous.
- §7: write the subject — incorporate `fix_summary`.
- §8: push (only when `PUSH=yes`); refuses detached HEAD,
  `main` / `master`, missing upstream.
- §9: emit the final `COMMIT:` line.

### 4. Final-line contract (unchanged from the routine)

The routine emits one of these on its final line — and this is the
exact grammar the watch-loop queue handlers parse:

```
COMMIT: ok subject="<subject>"                  # PUSH=yes — commit + push both landed
COMMIT: ok subject="<subject>" pushed=no        # PUSH=no — commit landed, caller still owes the push
COMMIT: skip reason="<reason>"
COMMIT: failed reason="<reason>"
```

Examples:

```
COMMIT: ok subject="style(PROJ-812): sort imports in AuditPanel.tsx"
COMMIT: ok subject="fix(PROJ-812): apply 3 Copilot review comments" pushed=no
COMMIT: skip reason="nothing to commit"
COMMIT: failed reason="git push rejected (non-fast-forward)"
```

The skip-reason text comes from the routine — it'll read `nothing
to commit` rather than `autofix produced no changes`. The queue
handlers parse the `COMMIT: skip` shape, not the reason, so this is
parser-safe.

## Guardrails

All of `commit-routine.md`'s guardrails apply: never `--amend`
already-pushed commits, never `--force`, never `--no-verify`, never
push to `main` / `master`, never invent a Jira key, never append an
AI attribution trailer, never retry on commit or push failure.

The routine stages with `git add -A` (modifications, deletions, and
new untracked files all get swept in). That's the routine's
contract; this wrapper inherits it.

The routine adds one guardrail this file inherits: detached HEAD
refuses the push entirely. Pushing without a branch ref is
operator-only.

`SIMPLIFY=yes` is hard-coded in §3 above so workflow autofix commits
run the routine's §2 simplify pass — every commit, autofix
included, gets the per-commit cleanup. A simplify edit lands inside
the same fix commit (it does not add a fix round), so the watch
loop's own `max_fix_attempts` / stuck-loop caps still bound the loop;
it cannot churn forever. The heavier code-review branch gate stays
a separate, once-per-branch step.
