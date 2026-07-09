---
name: wise-commit-message
description: >-
  Draft one Conventional-Commits subject line from your pending git
  changes — both staged (`git diff --cached`) and unstaged-but-modified
  tracked files (`git diff`) — Jira-scoped (e.g. `feat(PROJ-777): …`) when
  a key is detectable from the branch, diff, recent commits, or the live
  session, and unscoped (e.g. `feat: …`) otherwise. Pass `--copy` to also
  drop the subject onto the macOS clipboard. Strictly read-only against
  git — never runs `git commit`, `git add`, or anything that mutates the
  repo. Invoked as `/wise-commit-message`. Use when the user says "write a
  commit message", "draft a commit", "conventional commit", "prepare
  commit message", "one-line commit", or types `/wise-commit-message`.
---

# /wise-commit-message — draft a Conventional-Commits subject from pending changes

## Why this skill exists

Every PR should land with a Conventional-Commits first line, and the
"right" first line is mostly mechanical:

1. Pick the type (`feat` / `fix` / `refactor` / `docs` / …) from what
   the diff actually does.
2. Scope it with the Jira ticket the branch is working on — when
   there is one.
3. Summarise the change in the imperative voice, short enough that
   the reader can scan a list of commits without hitting the wrap.

This skill automates the mechanical part. It looks at everything
**pending** — both what's staged (`git diff --cached`) and what's
modified-but-not-yet-staged in tracked files (`git diff`), but **not**
the last commit and **not** untracked files — prints one line in
Conventional-Commits form, and — with `--copy` — drops that line onto
the clipboard so the user can paste it straight into
`git commit -m "…"`. When some of the described changes are not yet
staged, the suggested commit command uses `git add -A` so the user
doesn't end up committing only half of what the subject describes.

The user stays in control of the actual `git commit` call. Drafting
and committing are intentionally separate here so the user can read
the subject, tweak it, combine it with a body of their own, or
reject it entirely without ever having created a commit object they'd
need to amend.

> **Shared rules.** §3–§5 below follow
> `${WISE_PLUGIN_ROOT}/references/subject-drafting.md` — the single
> source of truth that `/wise-commit`, `/wise-commit-push`, and
> `draft-body.md` also read. Change a Jira-detection, classification,
> or subject-formatting rule there, not here.

## Invocation

```
/wise-commit-message
/wise-commit-message --copy
/wise:wise-commit-message          # canonical namespaced form
/wise:wise-commit-message --copy
```

No positionals. The only supported flag is `--copy`. Anything else
the user types is treated as an error (see [§Arguments](#arguments)).

This is a **standalone slash-command skill** — invoke it directly,
either as `/wise-commit-message` (bare alias) or
`/wise:wise-commit-message` (canonical).

## Arguments

Claude Code hands this skill the user's raw argument string (whatever
comes after `/wise-commit-message` on the prompt line). Parse `$ARGUMENTS`
yourself.

| Token      | Meaning                                                     |
|------------|-------------------------------------------------------------|
| _(empty)_  | Draft the message and print it. No clipboard write.         |
| `--copy`   | Draft, print, AND copy the subject line to the clipboard.   |

If the argument string contains anything other than optional
whitespace or the single literal token `--copy`, stop with:

```
Unknown argument(s): <the extra tokens>
Usage: /wise-commit-message [--copy]
```

Do not try to "helpfully" interpret unknown tokens as a path, a
subject override, or anything else. The argument surface of this
skill is deliberately tiny; silent interpretation of extra input
produces surprises.

## Procedure

### 1. Verify we're inside a git working tree

```bash
git rev-parse --show-toplevel
```

If that fails (exit code ≠ 0), stop immediately with:

```
Not inside a git working tree. /wise-commit-message only makes sense
when you have staged changes in a repository.
```

No fallback, no prompt. There is nothing to draft from.

### 2. Inspect pending changes (staged + unstaged tracked)

Collect views of both the staged index and the unstaged
working-tree changes to tracked files — one compact, one full — so
you can reason about scope and content without re-running git
repeatedly:

```bash
git diff --cached --stat                    # staged: file list + line deltas
git diff --cached --unified=0 --no-color    # staged: the actual changes
git diff --cached --name-only               # staged: just paths

git diff --stat                             # unstaged tracked: file list + line deltas
git diff --unified=0 --no-color             # unstaged tracked: the actual changes
git diff --name-only                        # unstaged tracked: just paths
```

Let `STAGED` = the paths from `git diff --cached --name-only` and
`UNSTAGED` = the paths from `git diff --name-only`. Either (or both)
may be non-empty.

- If **both** `STAGED` and `UNSTAGED` are empty, stop with:

  ```
  No pending changes. /wise-commit-message describes what you're about
  to commit; there's nothing staged and nothing modified in tracked
  files. Make some changes (or stage existing ones) and re-run.
  ```

- Otherwise continue. For the purpose of classifying the diff in
  [§4](#4-classify-the-change) and summarising it in [§5](#5-write-the-subject), treat **the union of both
  diffs** as one combined change set. In practice this means when
  you read diff content for reasoning, read `git diff HEAD`
  (index + working tree combined for tracked files) so you see the
  full picture in one pass:

  ```bash
  git diff HEAD --stat
  git diff HEAD --unified=0 --no-color
  ```

Remember whether `UNSTAGED` is non-empty — [§6](#6-print-the-output) uses that to decide
whether the suggested commit command should use `git add -A` first.

Do not look at untracked files (`git status --porcelain | grep '^??'`).
A user that hasn't `git add`ed a brand-new file may be deliberately
excluding it — scooping it in silently is the same class of surprise
as falling back to `HEAD~1`. If the user wants untracked files in
the commit, they stage them first and re-run.

Do not silently fall back to `HEAD~1` — that would draft a message
for a different set of changes than what the user is about to
commit.

### 3. Detect a Jira ticket key

Follow `${WISE_PLUGIN_ROOT}/references/subject-drafting.md` §1. Your
change set is the **pending** diff — grep `git diff HEAD` (staged +
unstaged tracked) for source 2. Run the chain in full before drafting;
never skip straight to §4.

### 4. Classify the change

Pick the type per `${WISE_PLUGIN_ROOT}/references/subject-drafting.md`
§2, judging from the diff *content* — not the branch name, not the
ticket, not what sounds nicest.

### 5. Write the subject

Draft the subject per `${WISE_PLUGIN_ROOT}/references/subject-drafting.md`
§3 (format with/without Jira key, imperative, lowercase, ≤72 chars, no
attribution trailer).

If the change genuinely warrants a body (breaking change,
context-heavy fix that won't fit in 72 chars), note that as a
*separate sentence* in the rationale line below — do **not** extend
the commit subject into a multi-line message. This skill's one and
only output is a single subject.

### 6. Print the output

Print exactly three things, in this order, each separated by a
blank line:

```
<the commit subject>

<one short sentence explaining why you picked this type and scope —
 reference the file(s) or diff region(s) that drove the call. If
 the change warrants a body, flag that here in plain prose. If
 some of the described changes are unstaged, mention it in the
 same sentence.>

Commit with:
  <suggested commit command>
```

The rationale sentence exists so the user can override your call if
they disagree — if the line reads "chose `chore` because the diff is
mostly lockfile churn", the user can retype it as `fix` themselves.
It's cheap to include and it makes your decisions legible. Keep it
to one sentence; this is a drafter, not a code review.

**Suggested commit command.** Pick one based on what you saw in
[§2](#2-inspect-pending-changes-staged--unstaged-tracked):

- If `UNSTAGED` was **empty** (everything described is already
  staged):
  ```
  git commit -m "<the commit subject>"
  ```
- If `UNSTAGED` was **non-empty** (some or all of the described
  changes are still in the working tree):
  ```
  git add -A && git commit -m "<the commit subject>"
  ```

The `git add -A` form is correct here because the subject we drafted
describes the union of staged + unstaged tracked changes — committing
without staging the rest would leave the commit describing more than
it actually contains, which is exactly the kind of drift the skill is
trying to prevent.

### 7. Copy to clipboard (only if `--copy` was passed)

If `--copy` was passed you MUST actually run the Bash tool — printing
"Copied to clipboard" as prose without running it is this skill's #1
failure mode (the user pastes stale content). If `--copy` was not
passed, skip this step entirely: no clipboard write, no prompt.

1. Verify the backend: `command -v pbcopy`. Non-zero (e.g. Linux) →
   print `Skipped --copy: pbcopy not found.` and stop the step. Do
   **not** substitute `xclip` / `wl-copy` — this skill is macOS-scoped.
2. Copy AND verify in one Bash call:
   ```bash
   printf '%s' '<subject>' | pbcopy && pbpaste
   ```
   The `&& pbpaste` tail is mandatory — it echoes the clipboard back
   so success is *observable*, not claimed. `printf '%s'` avoids the
   trailing newline `echo` would add. Single-quote `<subject>`,
   escaping any literal single quote as `'\''`.
3. Stdout matches `<subject>` exactly → print
   `Copied to clipboard: <subject>`. Otherwise print the actual
   problem (include pbpaste's output / the error) — never claim
   success the verification didn't confirm.

## Examples

**Example 1** — staged: new React component for an a11y audit panel;
branch is `feat/PROJ-812-a11y-panel`.

```
feat(PROJ-812): add a11y audit panel to PR review surface

Picked feat because the diff introduces a new user-visible panel
(`AuditPanel.tsx`) rather than touching existing behaviour; branch
name supplied the PROJ-812 scope.

Commit with:
  git commit -m "feat(PROJ-812): add a11y audit panel to PR review surface"
```

**Example 2** — staged: one-line fix in `SectionQuery.php`; branch is
`main`; no ticket key anywhere.

```
fix: prevent null dereference when section has no active teacher

Picked fix because the diff adds a `!== null` guard in
`SectionQuery::getActiveTeacher()`; no ticket key found in branch,
diff, recent log, or this session, so the subject is unscoped.

Commit with:
  git commit -m "fix: prevent null dereference when section has no active teacher"
```

**Example 3** — staged: dependency bump in `package-lock.json`;
`--copy` passed.

```
build: bump vitest 1.6.0 → 1.6.2

Picked build because the only changed file is `package-lock.json`
with a vitest version delta; no tests or source code are touched.

Commit with:
  git commit -m "build: bump vitest 1.6.0 → 1.6.2"

Copied to clipboard: build: bump vitest 1.6.0 → 1.6.2
```

(The closing line is only printed after the Bash tool actually ran
`printf '%s' 'build: …' | pbcopy && pbpaste` and its stdout came
back as the exact subject. If pbpaste returned something else, the
skill prints the failure instead.)

**Example 4** — nothing staged, but `src/IndexController.php` is
modified in the working tree; branch is `fix/PROJ-901-null-teacher`.

```
fix(PROJ-901): guard against null teacher in IndexController

Picked fix because the diff adds a null-check before dereferencing
`getActiveTeacher()`; changes are unstaged, so the command below uses
`git add -A` to include them.

Commit with:
  git add -A && git commit -m "fix(PROJ-901): guard against null teacher in IndexController"
```

## Guardrails

- **Never run `git commit`, `git add`, `git stash`, `git reset`, or
  any other mutating git operation.** This skill is strictly
  read-only against git, plus one optional write to the clipboard.
  Drafting a subject and committing it are intentionally separate
  concerns — the user reviews the subject before any commit object
  exists.
- **Never invent a Jira ticket key.** If all four detection sources
  come up empty, emit an unscoped subject. An unscoped subject is
  better than a wrong scope — the latter attributes commits to the
  wrong ticket in Jira's activity log and takes real cleanup to
  untangle.
- **Never reach for `HEAD~1` or untracked files as a "recovery" when
  nothing is pending.** [§2](#2-inspect-pending-changes-staged--unstaged-tracked)'s
  "no pending changes" stop is intentional; silently describing a
  different set of changes (the previous commit, or files the user
  deliberately didn't `git add`) would be strictly worse than an
  error. Staged and unstaged tracked changes are both fair game;
  untracked files and the prior commit are not.
- **Never prompt the user for missing information.** This skill runs
  off what it can see. If the diff or branch context is too thin for
  a confident subject, say so in the rationale line and let the user
  either add context (comments, a ticket in the branch name) and
  re-run, or write the subject themselves.
- **Never append an Anthropic or Claude attribution trailer.** No
  `Co-Authored-By: Claude …`, no `🤖 Generated with Claude Code`,
  no `Co-Authored-By: Anthropic …`. The subject is the user's prose
  describing their own work; this skill is a drafter, not a
  co-author.
- **Never reach for `xclip` / `wl-copy` / other clipboard backends.**
  `pbcopy` is the only supported clipboard integration; on
  non-macOS, report the skip and let the user copy manually.
