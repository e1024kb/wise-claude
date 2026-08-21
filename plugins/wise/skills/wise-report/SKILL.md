---
name: wise-report
description: >-
  Produce a verified status report of the current session - what is
  done, what is not, the plan, next steps, open questions, leftovers,
  and postponed tasks - in compact ref-coded form (F facts, A actions,
  Q questions, R risks, L leftovers, P postponed), with every claim
  backed by checkable evidence (git SHA, PR number, file path, test
  output) or explicitly marked unverified. Pass `--full` for expanded
  detail, `--save` to also write the report to a handoff file that
  survives compaction and session end. Invoked as `/wise-report` (bare
  alias) or `/wise:wise-report` (canonical). Use when the user says
  "session status", "status report", "where are we", "what's done",
  "what's left", "summarize the session", "session summary", "what's
  next", "handoff report", or types `/wise-report`.
argument-hint: "[--full] [--save]"
model: opus
effort: low
allowed-tools: Read, Write, Grep, Glob, Bash(git:*), Bash(gh:*), Bash(python3:*), Bash(ls:*), Bash(test:*), Bash(date:*), Bash(mkdir:*), Bash(pwd:*), Bash(tr:*)
---

# /wise-report - verified session status report

## Why this skill exists

Long sessions accumulate finished work, half-finished work, promises,
and open questions. A summary written from conversation memory drifts:
it reports planned things as done, and forgets leftovers that never
made it back into the conversation. This skill separates *recall* from
*verification*: collect claims from memory and context, check each one
against the real state of the world (git, PRs, CI, workflow runs, todo
state, files on disk), and only then report - with evidence attached
and unproven claims labelled as such.

The output is deliberately compact and ref-coded so the user can
reply about any single item ("do A2 first", "drop L1") without
quoting it back.

## Invocation

```
/wise-report
/wise-report --full
/wise-report --save
/wise-report --full --save
/wise:wise-report [--full] [--save]   # canonical namespaced form
```

## Arguments

Parse `$ARGUMENTS` yourself. Only two flags exist, in any order:

| Token     | Meaning                                                        |
|-----------|----------------------------------------------------------------|
| _(empty)_ | Brief report, printed to the conversation only.                |
| `--full`  | Expanded report: each item gets 1-3 indented context lines with a proof excerpt. |
| `--save`  | Also write the report to a handoff file (see §4) and print its path. |

If `$ARGUMENTS` contains anything other than whitespace, `--full`,
and `--save`, stop with:

```
Unknown argument(s): <the extra tokens>
Usage: /wise-report [--full] [--save]
```

Do not interpret unknown tokens as a topic filter or anything else.

## Procedure

### 1. Recall pass - collect claims, not facts

From the conversation context, any loaded memory, and todo state,
list every candidate item under these headings: done, not done / in
progress, plan, next steps, open questions, leftovers (small things
noticed but not handled), postponed (explicitly deferred). Include
promises you made and requests the user withdrew or changed.

Everything from this pass is a **claim**. Nothing goes into the
report as fact until step 2 confirms it or it is labelled
`(unverified)`.

Also read, when present in the workspace (skip silently when
absent):

- `.remember/now.md` and the newest `.remember/today-*.md` - recent
  session notes.
- Any handoff file from a previous `/wise-report --save` run (see §4
  for the location) - its open items may still be open.

### 2. Verification sweep - gather evidence

Probe the sources below, running independent probes in one message. An
unavailable source (no remote, no `gh`, no wise scripts, no
`.remember/`) is skipped quietly and listed in the report footer as
skipped - never an error.

**Git (always):**

```bash
git status --porcelain
git log --oneline -15
git branch --show-current
git stash list
git diff --stat
```

Confirms: commits that landed, uncommitted work, stashes, branch
position.

**GitHub (when a remote exists and `gh` is available):**

```bash
gh pr list --state all --limit 10 --json number,title,state,statusCheckRollup
```

Confirms: PR states and CI results for claims like "PR #54 merged"
or "CI green".

**Wise workflow runs (when the plugin scripts are reachable):**

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" list-runs "$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" runs-root 2>/dev/null)" 2>/dev/null || true
```

Confirms: paused or running workflow runs - the classic forgotten
leftover.

**Files (targeted):** for claims about specific edits, check the file
exists and contains the change (`Read` / `Grep`). One check per claim -
do not re-read whole files you already know.

**Tests / checks:** do NOT re-run test suites just to verify a "tests
pass" claim - report the last observed result with its evidence
(e.g. output seen earlier this session) or mark it `(unverified)`.
Re-running is the user's call.

### 3. Aggregate and label

Match each claim against the evidence:

- **Proof found** - keep it, attach an evidence tag:
  `[git 0f21b98]`, `[PR #54 merged, CI green]`,
  `[plugins/wise/skills/wise-report/SKILL.md exists]`,
  `[131 tests passed, seen this session]`.
- **No proof reachable** - keep it, append `(unverified)`. Never
  silently drop a claim and never present it as fact.
- **Evidence contradicts the claim** - report the evidence, not the
  memory (e.g. memory says committed, `git status` shows the file
  dirty: it goes under "not done" with the git evidence).

### 4. Emit the report

Use this exact structure. Omit any section with no items (print
`- none` only for Done and Not done, which the user always wants to
see). Number refs per prefix (F1, F2, A1, ...) and keep them stable
within the conversation.

```
# Session report - <YYYY-MM-DD HH:MM>

## Summary
<2-4 plain sentences: what this session was about and where it stands.>

## Done
F1 <fact> [evidence]

## Not done / in progress
F5 <fact> [evidence or (unverified)]

## Plan / next
A1 <next action, imperative>

## Open questions
Q1 <question the user has not answered / decision pending>

## Leftovers
L1 <small item noticed but not handled> [evidence]

## Postponed
P1 <explicitly deferred item> - <why / until when>

## Risks
R1 <only if real: something likely to bite later>

Sources: git, gh, workflows, .remember | skipped: <list or none>
```

**Brief mode (default):** one line per item. **`--full` mode:** under
each item add 1-3 indented lines of context and a short proof excerpt
(a log line, a diff stat, a PR check name). Same structure, same
refs.

**`--save` mode:** after printing, write the exact same report as
markdown to the per-workspace handoff location:

```bash
root="$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" runs-root 2>/dev/null)"
```

- If that succeeds, the reports dir is `root` with its trailing
  `/runs/<slug>` component's `runs` replaced by `reports` (same
  workspace slug, sibling tree).
- If it fails, fall back to
  `${XDG_DATA_HOME:-$HOME/.local/share}/wise/reports/$(pwd | tr '/' '-')`.

`mkdir -p` the dir, write `report-<YYYYMMDD-HHMM>.md`, and print the
absolute path on its own final line: `Saved: <path>`. This file is
what step 1 reads back as "previous handoff" in a later session.

## Guardrails

- **Read-only except the `--save` file.** Never commit, push, edit
  project files, re-run test suites, or mutate workflow state.
- **No fabrication.** Every line is either evidence-tagged or marked
  `(unverified)`. When evidence and memory disagree, evidence wins.
- **Degrade quietly.** A missing source (no gh, no remote, no
  `.remember/`, no wise scripts) is a footer note, not a failure.
- **Do not invoke other skills and do not spawn subagents.** The
  report must stay cheap enough to run casually.
- **Do not pad.** Empty sections are omitted, not filled. A short
  honest report beats a long speculative one.
