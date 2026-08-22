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
allowed-tools: Read, Write, Grep, Glob, Bash(git:*), Bash(gh:*), Bash(python3:*), Bash(${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py:*), Bash(date:*), Bash(mkdir:*), Bash(pwd:*), Bash(tr:*), Bash(basename:*), Bash(dirname:*)
---

# /wise-report - verified session status report

## Why this skill exists

Long sessions accumulate finished work, half-finished work, promises,
and open questions - and a summary written from memory alone drifts.
This skill runs the plugin's shared report pass over the current
session so the user gets a compact, ref-coded report where every line
is either evidence-tagged or explicitly `(unverified)`, and can reply
about any single item ("do A2 first", "drop L1") without quoting it
back.

The skill is a thin wrapper: parse the two flags, then read the shared
`${CLAUDE_PLUGIN_ROOT}/references/report-pass.md` and follow it. The
reference is the source of truth for the recall → verify → aggregate →
emit discipline, the report template, the handoff-store location, and
the final-line format - workflow steps and future `-auto` building
blocks read the same file, so a change to the routine lands everywhere
at once.

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
| `--full`  | Expanded detail (`MODE=full` - rendering defined in the pass). |
| `--save`  | Also write the report to the handoff store and print `Saved: <path>`. |

If `$ARGUMENTS` contains anything other than whitespace, `--full`,
and `--save`, stop with:

```
Unknown argument(s): <the extra tokens>
Usage: /wise-report [--full] [--save]
```

followed by `REPORT: failed reason="unknown arguments"` as the final
line, so a programmatic caller always gets a parseable terminator.
Do not interpret unknown tokens as a topic filter or anything else.

## Procedure

### 1. Read the shared pass

Read `${CLAUDE_PLUGIN_ROOT}/references/report-pass.md`. If the read
fails (missing file, unset `CLAUDE_PLUGIN_ROOT`), stop with
`REPORT: failed reason="report-pass.md unreachable"` - do not
improvise the routine from memory; the guardrails live in that file.

### 2. Run it for the current session

Set the pass inputs from the parsed flags and follow the reference
end to end:

- `SCOPE` = `current session`
- `MODE`  = `full` if `--full` was passed, else `brief`
- `SAVE`  = `yes` if `--save` was passed, else `no`

The reference owns everything else - the recall pass, the
verification probes, evidence labelling, the report template, the
handoff-store path, and the final `REPORT:` line.

### 3. Relay

The report itself is the output - print it in full, then the
reference's final line, which MUST be one of its documented shapes:

```
REPORT: ok scope="current session" mode=<brief|full> saved="<path>"
REPORT: ok scope="current session" mode=<brief|full> saved=-
REPORT: failed reason="<one line>"
```

When `--save` was passed, the saved path (also printed as
`Saved: <path>`) is the handoff file a later session's pass reads
back.

## Guardrails

All of `report-pass.md`'s guardrails apply - read-only except the
`--save` file, no fabrication (evidence beats memory), degrade
quietly on missing sources, no other skills, no subagents, no
padding. This skill adds nothing on top; if a rule needs changing,
change it in the reference so every caller inherits it.
