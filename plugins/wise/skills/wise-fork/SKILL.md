---
name: wise-fork
description: >-
  Reorient the agent after a session fork: inherited context becomes
  read-only background and pre-fork work stays with the original
  session. Every in-flight task, plan, and promise from before the
  fork is dropped, remembered file state is distrusted, and this
  session starts fresh parallel work - optionally on a new goal
  passed as the argument. Invoked as `/wise-fork` (bare alias) or
  `/wise:wise-fork` (canonical). Use when the user says "this is a
  fork", "forked session", "we branched this conversation", "start
  fresh but keep context", "don't continue the old work", or types
  `/wise-fork`.
argument-hint: "[<new goal>]"
allowed-tools: Bash(git status:*), Bash(git branch:*), Bash(git log:*), Bash(git rev-parse:*)
---

# /wise-fork - reorient a forked session

## Why this skill exists

Forking a session copies the whole conversation into a new, parallel
session. The copy inherits every open thread: half-finished tasks,
promised next steps, an in-progress plan. Left alone, the agent in
the fork will try to finish them - duplicating work the original
session is still doing, and possibly fighting it over the same
working tree. This skill draws a hard line: context before the fork
is background, work before the fork is not yours.

## Invocation

```
/wise-fork
/wise-fork <new goal in free prose>
/wise:wise-fork [<new goal>]   # canonical namespaced form
```

## Arguments

Read `$ARGUMENTS`. Everything (if anything) is free-form text: the
first task of this forked session. No flags, no validation - prose
is prose. When empty, acknowledge the fork and wait for the user's
next message.

## Procedure

### 1. Adopt the fork contract

From this point on, apply these rules to the entire conversation
above this invocation:

- **Context is background, not a backlog.** Facts, decisions,
  codebase knowledge, and user preferences learned before the fork
  stay valid - use them freely as reference.
- **In-flight work belongs to the original session.** Do not
  continue, resume, retry, re-verify, or "quickly finish" any task,
  plan, todo, or promise made before the fork - even ones that look
  one step from done. The original session runs in parallel and owns
  them; doing them here duplicates work and creates conflicts.
- **Pending questions and offers are void.** An unanswered question
  or an offer you made before the fork ("say the word and I'll ...")
  is not carried over. Do not act on it and do not re-ask it.
- **Distrust remembered file state.** The original session may be
  modifying the same working tree in parallel. Anything you recall
  about file contents, branch position, or uncommitted changes may
  already be stale - re-check before relying on it or editing.

### 2. Ground the starting state (git repos only)

Detect with `git rev-parse --is-inside-work-tree 2>/dev/null`; on
failure or `false`, skip this step silently. Inside a working tree,
snapshot where this fork actually starts - one call:

```bash
git status --porcelain && git branch --show-current && git log -3 --oneline
```

This is the fork's baseline, superseding whatever the inherited
context says about the tree. Outside a git repo, skip silently.

### 3. Acknowledge

Reply with a short confirmation:

- One line: fork acknowledged, prior context now background only.
- The in-flight items you are explicitly dropping, one line each
  (so the user can see nothing important falls between the two
  sessions). None is fine: "no in-flight work to drop".
- The baseline from step 2, one line (branch + clean/dirty), when it
  ran.

Then:

- **`$ARGUMENTS` non-empty** - treat it as the user's first request
  of this session and start on it immediately.
- **`$ARGUMENTS` empty** - stop after the confirmation and wait. Do
  not propose next steps drawn from pre-fork work; that is exactly
  the backlog this skill severs.

## Guardrails

- Never resume pre-fork work, even if the user's new request seems
  related - if the overlap is real, say so and ask one question
  instead of silently merging the threads.
- Never assume the inherited working-tree state is current (step 1's
  distrust rule). Re-probe before any edit.
- The fork contract persists for the rest of the session - it is not
  a one-message notice. Re-apply it whenever an old thread tries to
  resurface (e.g. a stale todo list).
- This skill itself mutates nothing: no commits, no file edits, no
  state files - its only tool use is the read-only git probe in
  step 2. Work on a new goal passed via `$ARGUMENTS` happens after
  the reorientation, under the session's normal permissions, exactly
  as if the user had typed it as their next message.
