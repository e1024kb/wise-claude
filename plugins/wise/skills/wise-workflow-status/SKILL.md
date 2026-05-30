---
name: wise-workflow-status
description: >-
  Show workflow runs in the current workspace. With no argument, list
  every run under the current workspace's runs root (per-workspace by slug under `~/.local/share/wise/runs/`) with its status,
  workflow name, and last activity. With a run ULID, print the full
  state YAML of that run. Read-only. Invoked as `/wise-workflow-status`
  (bare alias) or `/wise:wise-workflow-status` (canonical). Use when the
  user says "list workflow runs", "show workflow status", "status of
  my workflow", "which runs are paused", "inspect run <ulid>", or
  types `/wise-workflow-status`.
argument-hint: "[<run-ulid>]"
model: opus
effort: low
allowed-tools: Read, Bash(${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap-deps.sh:*), Bash(${CLAUDE_PLUGIN_ROOT}/scripts/init-registry.py:*), Bash(${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py:*), Bash(bash:*), Bash(python3:*), Bash(test:*)
---

# /wise-workflow-status — list runs or dump one

## Why this skill exists

Workflow runs leave per-run state at
`~/.local/share/wise/runs/<cwd-slug>/<ulid>/state.yaml`
(honours `XDG_DATA_HOME`; off the project tree on purpose — avoids
`.claude/**` sensitive-path prompts AND keeps gitignore clean).
Users need a cheap way to see what's out there (am I mid-run? which
run is paused?) and to inspect a specific one without reading YAML by
hand.

## Arguments

Read `$ARGUMENTS`. The first whitespace-separated token is the
`run-id` — an optional ULID of the run to dump. When present, print
the full state.yaml content via `workflows.py dump-state`. When
`$ARGUMENTS` is empty, list every run in the current workspace via
`workflows.py list-runs`.

## Procedure

### 1. Init-check + fetch — in ONE message

Run the init-check per `${CLAUDE_PLUGIN_ROOT}/references/init-check.md`,
firing `init-registry.py check` together with the data call for your
mode, in one message (the inline `$(… runs-root)` subshell keeps it a
single tool call):

- **`run-id` absent** (list every run):
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" list-runs "$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" runs-root 2>/dev/null)" 2>/dev/null || true
  ```
- **`run-id` present** — validate the ULID shape FIRST (26-char
  Crockford base-32); reject with
  `Not a ULID: <value>. Expected 26-character Crockford base-32 string.`
  Then:
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" dump-state "$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" runs-root 2>/dev/null)/<run-id>/state.yaml" 2>/dev/null || true
  ```

### 2. Interpret

On `INIT:ok`, use the `list-runs` / `dump-state` output directly.
Otherwise follow the reference's fallback — this skill is read-only, so
on `BOOTSTRAP:need-python` relay and stop; on `READY:<py-path>`, re-run
the appropriate data call.

**When `run-id` is absent** — relay `list-runs` stdout verbatim. The
script handles the "no runs yet" case with its own message.

**When `run-id` is present** — if `dump-state` failed (exit non-zero,
missing state file), stop with:
`No run found for ULID <run-id> in this workspace.`
Otherwise relay its stdout as a code block. Follow with a two-line footer:

```
State file: <absolute path>
Logs: <absolute path>/logs/
```

## Guardrails

- Read-only. Never modify a run's state.yaml — resume, retry, and
  modify flows live in `workflow-resume` and `workflow-run`.
- Do not invoke any other skill.
- Do not spawn subagents.
