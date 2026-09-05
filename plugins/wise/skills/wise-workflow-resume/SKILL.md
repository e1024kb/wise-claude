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
allowed-tools: Read, Write, Skill, AskUserQuestion, TodoWrite, Task, Agent, TeamCreate, TeamDelete, SendMessage, Monitor, TaskCreate, TaskList, TaskGet, TaskUpdate, TaskOutput, TaskStop, Bash(${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap-deps.sh:*), Bash(${CLAUDE_PLUGIN_ROOT}/scripts/init-registry.py:*), Bash(${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py:*), Bash(${CLAUDE_PLUGIN_ROOT}/engine/engine.sh:*), Bash(bash:*), Bash(python3:*), Bash(test:*)
---

# /wise-workflow-resume

Tools come from the plugin's `wise-engine` MCP server. Errors return
`{"error":{code,message,...}}`: `DAEMON_UNAVAILABLE` means
`Run /wise-init, then retry.`; `AUTH_REQUIRED` means print `login_cmd`
verbatim. Stop on either.

## 1. Pick the run

`wise_status` (no id) lists every run. Resumable: `paused`, `failed`.
`gated` runs are answered, not resumed. `running` runs are only
followed.

- Argument given: `wise_status {run_id}`. On `RUN_NOT_FOUND`, if
  `~/.local/share/wise/runs/<cwd-slug>/<run_id>/state.yaml` exists it
  is a legacy v1 run: follow
  `${CLAUDE_PLUGIN_ROOT}/references/legacy-conductor/resume.md`.
  Otherwise print `No run <run_id>.` and stop.
- No argument: AskUserQuestion over the `paused`, `failed`, `gated`
  runs (label `run_id`; description `<workflow>, <status>, last
  activity <last_activity_at>, <cwd>`) plus Abort. None: print
  `No resumable runs. /wise-workflow-status lists all runs.` and stop.

## 2. Resume

- `paused` / `failed`: `wise_resume {run_id}` returns `{run_id,
  status}`. Print `Resuming <run_id> (<workflow>).`
- `gated`: show `gate.message`, AskUserQuestion with `gate.options`
  (free text when `allow_text`), `wise_answer {run_id, gate_id, value}`.
- `completed` / `cancelled`: say the run is terminal and stop.

## 3. Follow

`wise_wait {run_id, after: 0, timeout_ms: 0}` returns the history at
once: print one line, `<n> steps done, <m> pending`, not the replay,
and set `after` to the last `seq`. Then run the wait loop and final
report exactly as `/wise-workflow-run` §4 and §5 (one line per event,
no raw step output, gates through AskUserQuestion and `wise_answer`,
`done: true` ends the loop).

## Rules

- Never re-run finished steps; the engine resumes only in-flight and
  pending work.
- Do not invoke other wise skills.
