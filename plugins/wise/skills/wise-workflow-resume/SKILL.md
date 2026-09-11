---
name: wise-workflow-resume
description: >-
  Resume a paused or failed workflow run on the wise engine, or answer
  the gate of a gated one, then follow its events in this conversation.
  Invoked as `/wise-workflow-resume` (bare alias) or
  `/wise:wise-workflow-resume` (canonical). Use when the user says
  "resume the workflow", "continue the paused run", "pick up the run",
  "resume run <ulid>", or types `/wise-workflow-resume`.
argument-hint: "[<run-ulid>]"
allowed-tools: Read, Write, Skill, AskUserQuestion, TodoWrite, Task, Agent, TeamCreate, TeamDelete, SendMessage, Monitor, TaskCreate, TaskList, TaskGet, TaskUpdate, TaskOutput, TaskStop, Bash(bash:*), Bash(python3:*), Bash(test:*)
---

# /wise-workflow-resume

Before asking any user question, read and follow the
[question lifecycle](../../references/workflow-host-control.md#keep-asynchronous-questions-open).
Keep asynchronous prompts open until answered; this rule does not authorize
questions in autonomous or otherwise prompt-free procedures.

First read [host control](../../references/workflow-host-control.md). Resolve the
loaded installation, set `WISE_HOST` to this conductor and `WISE_PLUGIN_ROOT`
to that installation. Use its managed launcher for shell commands. Follow the
reference's diagnostics and explicit-answer fallback when MCP or a native picker
is unavailable. Conductor host and child provider are independent.


Tools come from the managed `wise-engine` MCP server. Errors return
`{"error":{code,message,...}}`. Diagnose `DAEMON_UNAVAILABLE` through host
control before retrying. For `AUTH_REQUIRED`, print the selected provider's
`login_cmd` verbatim and stop.

## 1. Pick the run

`wise_status` (no id) lists every run. Resumable: `paused`, `failed`.
`gated` runs are answered, not resumed. `running` runs are only
followed.

- Argument given: `wise_status {run_id}`. On `RUN_NOT_FOUND`, use
  `python3 "${WISE_PLUGIN_ROOT}/scripts/wise-helpers.py" runs-root`
  to locate history. A `state.yaml` entry is a legacy v1 run: report
  `Legacy run cannot resume. Import its definition, review it, then
  start a new v2 run.` Keep its files unchanged. Otherwise print
  `No run <run_id>.` and stop.
- No argument: use the host control reference's explicit-answer route over the `paused`, `failed`, `gated`
  runs (label `run_id`; description `<workflow>, <status>, last
  activity <last_activity_at>, <cwd>`) plus Abort. None: print
  `No resumable runs. /wise-workflow-status lists all runs.` and stop.

## 2. Resume

Inspect the step statuses before resuming a failed run. Resume resets steps left
`running` to `pending`; it does not reset a step already marked `failed` and is
not a retry-failed-step command. If no runnable pending work remains, the run
fails again. Explain that limit, review completed side effects, and use a new
run after correcting the cause when a failed step needs another attempt.


- `paused` / `failed`: `wise_resume {run_id}` returns `{run_id,
  status}`. Print `Resuming <run_id> (<workflow>).`
- `gated`: show `gate.message`, collect an explicit answer with `gate.options`
  (free text when `allow_text`), `wise_answer {run_id, gate_id, value}`.
- `completed` / `cancelled`: say the run is terminal and stop.

## 3. Follow

`wise_wait {run_id, after: 0, timeout_ms: 0}` returns the history at
once: print one line, `<n> steps done, <m> pending`, not the replay,
and set `after` to the last `seq`. Then run the wait loop and final
report exactly as `/wise-workflow-run` §4 and §5 (one line per event,
no raw step output, gates through the host control reference's explicit-answer
route and `wise_answer`,
`done: true` ends the loop).

## Rules

- Never re-run finished steps; the engine resumes only in-flight and
  pending work.
- Do not invoke other wise skills.
