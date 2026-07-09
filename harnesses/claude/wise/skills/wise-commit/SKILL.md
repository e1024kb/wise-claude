---
name: wise-commit
description: >-
  Sweep every working-tree change into the index (`git add -A` —
  modifications, deletions, and new untracked files), draft a
  Conventional-Commits subject from the staged diff (Jira-scoped when
  a key is detectable from the branch / diff / log / session), and
  run `git commit`. Closes the loop that `/wise-commit-message` opens
  — that one drafts and hands the subject back to the user;
  `/wise-commit` drafts AND commits. Invoked as `/wise-commit` (bare
  alias) or `/wise:wise-commit` (canonical). Strictly local — never
  pushes (use `/wise-commit-push` for that), never amends, never
  bypasses hooks. Use whenever the user says "commit", "commit
  changes", "make a commit", "commit my work", or types
  `/wise-commit`.
argument-hint: ""
model: opus
effort: low
allowed-tools: Bash(git:*), Read, Edit, Write, Task
---

# /wise-commit — draft a Conventional-Commits subject and commit

## Why this skill exists

`/wise-commit-message` is read-only on purpose: it drafts a subject
and hands it back, so the user can review, tweak, or reject the
prose before any commit object exists. That's the right shape when
the operator is sitting at the keyboard.

`/wise-commit` is the right shape when the operator already trusts
the drafter to pick the type, scope, and prose — they want one
call that stages tracked modifications, drafts the subject, and
commits. No clipboard round-trip, no second invocation.

The skill is a thin wrapper: read the shared
[`commit-routine.md`](./commit-routine.md), set `PUSH=no`
and `SIMPLIFY=yes`, and follow it. The routine is the source
of truth for staging discipline, the §2 pre-staging simplify
pass, Jira detection, type classification, and the final-line
emit format. Pushing is `/wise-commit-push`'s job; the per-caller
`SIMPLIFY` default policy lives in the routine's §"Inputs the
caller sets".

## Invocation

```
/wise-commit
/wise:wise-commit                    # canonical namespaced form
```

No positionals, no flags. If the argument string contains anything
other than optional whitespace, stop with:

```
Unknown argument(s): <the extra tokens>
Usage: /wise-commit
```

If the user wants `--no-verify`, partial staging, or `--amend`, they
run `git commit` directly. This skill's argument surface is
deliberately empty so the routine's behaviour is predictable.

## Procedure

### 1. Read the shared routine

Read `${CLAUDE_PLUGIN_ROOT}/skills/wise-commit/commit-routine.md` (the
file that ships alongside this `SKILL.md`).

### 2. Run the routine with `PUSH=no` and `SIMPLIFY=yes`

Set `PUSH=no` and `SIMPLIFY=yes`, then follow the routine
end-to-end. `PUSH=no` skips §8 (push) entirely; `SIMPLIFY=yes` runs
the §2 simplify pass before staging so its edits land in the same
commit (a simplify error stops the routine with
`COMMIT: failed reason="simplify errored: …"` and nothing staged).
The routine owns everything else — staging discipline, Jira scope,
classification, subject drafting, the commit, and the final-line emit.

### 3. Final line

The routine emits one of:

```
COMMIT: ok subject="<subject>" pushed=no
COMMIT: skip reason="nothing to commit"
COMMIT: failed reason="<verbatim git error>"
```

`/wise-commit` always emits `pushed=no` on success (since this skill
never pushes). The eventual push, if the user wants one, is theirs
to run — or use `/wise-commit-push` next time.

## Examples

**Example 1** — branch is `feat/PROJ-812-a11y-panel`, working tree
has one new tracked file `AuditPanel.tsx`.

```
$ /wise-commit
[main abc1234] feat(PROJ-812): add a11y audit panel to PR review surface
 1 file changed, 47 insertions(+)

COMMIT: ok subject="feat(PROJ-812): add a11y audit panel to PR review surface" pushed=no
```

**Example 2** — clean tree, nothing modified.

```
$ /wise-commit
COMMIT: skip reason="nothing to commit"
```

**Example 3** — pre-commit hook rejects the commit.

```
$ /wise-commit
[hook] eslint found 3 errors in src/components/Foo/index.tsx
husky - pre-commit hook exited with code 1 (error)

COMMIT: failed reason="pre-commit hook exited with code 1"
```

The hook failure stops the routine — no `--no-verify`, no retry.
The operator fixes the lint errors and re-runs.

## Guardrails

All of `commit-routine.md`'s guardrails apply — no `--amend` /
`--no-verify` / `--force`, never invent a Jira key, no AI attribution
trailer, no retry on failure, and `.gitignore` is the only place to
exclude files (the routine's `git add -A` sweeps in everything else).
This skill additionally **never pushes** — that's `/wise-commit-push`'s
job.
