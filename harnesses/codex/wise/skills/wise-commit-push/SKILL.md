---
name: wise-commit-push
description: >-
  Sweep every working-tree change into the index (`git add -A` —
  modifications, deletions, and new untracked files), draft a
  Conventional-Commits subject from the staged diff, run `git commit`,
  then `git push` to the tracked upstream. If the working tree is already
  clean but the local branch is ahead of upstream (e.g. a previous skill
  already committed), skips the commit half and just pushes the existing
  local commits — the operator types one slash command either way. Refuses
  to push to `main` / `master` (commit lands locally; push is on the
  operator) and refuses `--force`, `--force-with-lease`, `--no-verify`
  entirely. Invoked as `/wise-commit-push`. Use whenever the user says
  "commit and push", "ship this", "push my changes", "commit and ship",
  "push the lint commit", or types `/wise-commit-push`. For local-only
  commits use `/wise-commit` instead; for read-only subject drafting use
  `/wise-commit-message`.
---

# /wise-commit-push — draft, commit, and push

## Why this skill exists

`/wise-commit` closes the local loop — stage, draft, commit. The
common follow-up on a feature branch is `git push`. Bundling those
two steps into one call is the whole point of `/wise-commit-push`:
the operator types one command and the change lands on the remote.

The skill is a thin wrapper: read the shared
[`commit-routine.md`](../wise-commit/commit-routine.md) (co-located
with `/wise-commit`, since that's the simpler skill that owns the
file), set `PUSH=yes` and `SIMPLIFY=yes`, and follow it. The
routine is the source of truth for the §2 pre-staging simplify
pass, staging, drafting, committing, the main/master push
refuse guard, the upstream check, and the final-line emit format.
The per-caller `SIMPLIFY` default policy lives in the routine's
§"Inputs the caller sets".

## Invocation

```
/wise-commit-push
/wise:wise-commit-push                # canonical namespaced form
```

No positionals, no flags. If the argument string contains anything
other than optional whitespace, stop with:

```
Unknown argument(s): <the extra tokens>
Usage: /wise-commit-push
```

If the user wants `--force`, `--no-verify`, or any other escape
hatch, they run `git push` directly. This skill's argument surface
is deliberately empty so the routine's behaviour is predictable
and safe.

## Procedure

### 1. Read the shared routine

Read `${WISE_PLUGIN_ROOT}/skills/wise-commit/commit-routine.md`
(the file ships alongside `/wise-commit`'s `SKILL.md`).

### 2. Run the routine with `PUSH=yes` and `SIMPLIFY=yes`

Set `PUSH=yes` and `SIMPLIFY=yes`, then follow the routine
end-to-end. `SIMPLIFY=yes` runs the §2 simplify pass before
staging; `PUSH=yes` additionally enables §8 (push), which refuses
`main` / `master` / detached HEAD, refuses to auto-set a missing
upstream, and surfaces any `git push` error verbatim with no retry,
force, or `--no-verify`.

One subtle path stays worth knowing: if `git add -A` finds nothing to
stage but the local branch is ahead of upstream (a previous skill
already committed), the routine skips the draft/commit half and just
pushes the existing commits — the final `COMMIT: ok subject="…"`
reports the existing HEAD subject. Empty index AND nothing ahead →
`COMMIT: skip reason="nothing to commit and nothing to push"`.

### 3. Final line

The routine emits one of:

```
COMMIT: ok subject="<subject>"
COMMIT: skip reason="nothing to commit"
COMMIT: failed reason="<verbatim error>"
```

The success line on `/wise-commit-push` has no `pushed=no` suffix
because the push landed.

## Examples

**Example 1** — branch is `feat/PROJ-812-a11y-panel`, upstream is
configured, working tree has tracked modifications.

```
$ /wise-commit-push
[feat/PROJ-812-a11y-panel abc1234] feat(PROJ-812): add a11y audit panel
 1 file changed, 47 insertions(+)
To github.com:owner/repo.git
   def5678..abc1234  feat/PROJ-812-a11y-panel -> feat/PROJ-812-a11y-panel

COMMIT: ok subject="feat(PROJ-812): add a11y audit panel"
```

**Example 2** — branch is `main`. Commit lands locally; push is
refused.

```
$ /wise-commit-push
[main abc1234] chore: bump dep versions
 1 file changed, 4 insertions(+), 4 deletions(-)

COMMIT: failed reason="refuse to push to main; push manually if intentional"
```

The local commit is already in `main`. If the operator meant to
push it, they run `git push` themselves — typically after pausing
to ask whether `main` is really the right branch for this change.

**Example 3** — branch is freshly created with no upstream.

```
$ /wise-commit-push
[my-feature abc1234] feat: rough out the new dashboard
 2 files changed, 31 insertions(+)

COMMIT: failed reason="no upstream configured for my-feature; run 'git push -u origin my-feature' once to set it"
```

The local commit landed; the operator runs the printed command
once and subsequent `/wise-commit-push` calls succeed.

**Example 4** — working tree clean but a previous skill already
landed a commit; just push it.

```
$ /wise-commit-push
To github.com:owner/repo.git
   def5678..abc1234  feat/PROJ-19106 -> feat/PROJ-19106

COMMIT: ok subject="style(PROJ-19106): escape apostrophe in form help text"
```

The routine's §4 found nothing to stage but saw the local branch
was 1 commit ahead of upstream. It skipped the draft / commit
steps and went straight to §8 (push). The `subject="…"` field is
the existing commit's subject — `git log -1 --pretty=%s` — because
that's what landed on the remote.

**Example 5** — non-fast-forward push.

```
$ /wise-commit-push
[feat/PROJ-812 abc1234] fix: address review feedback
 1 file changed, 8 insertions(+), 2 deletions(-)
To github.com:owner/repo.git
 ! [rejected]        feat/PROJ-812 -> feat/PROJ-812 (non-fast-forward)
error: failed to push some refs

COMMIT: failed reason="git push rejected (non-fast-forward)"
```

The local commit landed. Push failed because the remote has work
the local branch doesn't. The skill does NOT auto-`git pull --rebase`
— the operator decides whether a merge or rebase is the right
resolution.

## Guardrails

All of `commit-routine.md`'s guardrails apply (no `--amend`, no
attribution trailer, never invent a Jira key). For the push half
specifically — every rule the routine's §8 enforces, kept explicit
here because the stakes are high: **never** `--force` /
`--force-with-lease`, **never** `--no-verify`, **never** push to
`main` / `master`, **never** auto-set a missing upstream (surface the
`git push -u origin <branch>` hint instead), and **never** retry a
failed push (no auto-pull, no auto-rebase). A refused or failed push
leaves the local commit in place; the operator decides the resolution.
