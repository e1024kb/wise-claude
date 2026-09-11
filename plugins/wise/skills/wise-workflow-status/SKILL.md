---
name: wise-workflow-status
description: >-
  Show workflow runs on the wise engine. With no argument, list every
  run with its status, workflow, start time, cwd and live children.
  With a run ULID, show that run; when it is gated, show the gate and
  offer to answer it. Invoked as `/wise-workflow-status` (bare alias) or
  `/wise:wise-workflow-status` (canonical). Use when the user says "list
  workflow runs", "show workflow status", "status of my workflow",
  "which runs are paused", "inspect run <ulid>", or types
  `/wise-workflow-status`.
argument-hint: "[<run-ulid>]"
model: opus
effort: low
allowed-tools: Read, AskUserQuestion, Bash(${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap-deps.sh:*), Bash(${CLAUDE_PLUGIN_ROOT}/scripts/init-registry.py:*), Bash(bash:*), Bash(python3:*), Bash(test:*)
---

# /wise-workflow-status

`wise_status` comes from the plugin's `wise-engine` MCP server. On
`DAEMON_UNAVAILABLE` print `Run /wise-init, then retry.` and stop.

## No argument

`wise_status {}` returns `RunSummary[]`, newest activity first. Render
one table: run id | workflow | status | started | cwd | children. The
children column lists each live child as `<step> turn <n> <tool>`, or
`-`. Empty list: `No engine runs yet.` If legacy history is requested,
  get the canonical root with
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/wise-helpers.py" runs-root`,
  then use that helper's `list-runs <root>` command. It reports v1
  entries as read-only legacy notices; they cannot resume. Keep all
  history files unchanged.

## With a run id

`wise_status {run_id}` returns one `RunSummary`. Print run id,
workflow, status, started, last activity, completed (when set), cwd,
children as above. `RUN_NOT_FOUND`: `No run <run_id>.`

Status `gated`: print `gate.step` and `gate.message`, then
AskUserQuestion `Answer this gate now?` with the gate's options plus
`Not now` (free text when `allow_text`). On a choice, `wise_answer
{run_id, gate_id, value}`, print `gate answered`, point to
`/wise-workflow-resume <run_id>` to follow the run.

## Rules

- Read-only apart from the offered gate answer.
- Do not invoke other wise skills.
