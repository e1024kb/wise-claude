---
name: wise-workflow-run
description: >-
  Start a workflow run on the wise engine: pre-flight questions, run
  context from the conversation, one line per engine event, gates.
  Invoked as `/wise-workflow-run` (bare alias) or
  `/wise:wise-workflow-run` (canonical). Use when the user says "run
  the workflow", "kick off <workflow-name>", or types
  `/wise-workflow-run`.
argument-hint: "[<workflow-name> [<input1> <free-form remainder…>]]"
allowed-tools: Read, Write, Skill, AskUserQuestion, TodoWrite, Task, Agent, TeamCreate, TeamDelete, SendMessage, Monitor, TaskCreate, TaskList, TaskGet, TaskUpdate, TaskOutput, TaskStop, Bash(${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap-deps.sh:*), Bash(${CLAUDE_PLUGIN_ROOT}/scripts/init-registry.py:*), Bash(${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py:*), Bash(${CLAUDE_PLUGIN_ROOT}/engine/engine.sh:*), Bash(bash:*), Bash(python3:*), Bash(cat:*), Bash(mkdir:*), Bash(git:*), Bash(test:*)
---

# /wise-workflow-run - the conductor

The `wise_*` tools (`wise_status`, `wise_preflight`, `wise_run`,
`wise_wait`, `wise_answer`, `wise_cancel`, `wise_nudge`, `wise_resume`)
come from the plugin's `wise-engine` MCP server; Claude Code shows them
with a server prefix. Errors return `{"error":{code,message,...}}`.

## Arguments

`$ARGUMENTS`: first token is the workflow name (empty: run
`bash ${CLAUDE_PLUGIN_ROOT}/engine/engine.sh list-defs`, pick with
AskUserQuestion plus Abort). Remaining tokens fill the declared inputs
in order; the last declared input absorbs the rest of the line.

## 1. Init check and profile (one message)

Call `wise_status` (no id) and, in the same message, read the session
profile per `${CLAUDE_PLUGIN_ROOT}/references/profile-read.md`.

- `wise_*` tools missing, or `DAEMON_UNAVAILABLE`: print
  `Run /wise-init, then retry.` and stop.
- `AUTH_REQUIRED`: print `login_cmd` verbatim and stop.
- Profile is known when the store file exists: pass it as `profile`
  to `wise_preflight` and `wise_run` and skip the `profile` question.

## 2. Pre-flight

`wise_preflight {workflow, cwd, profile?}`; `cwd` is the absolute git
toplevel, else pwd.

- `WORKFLOW_NOT_FOUND`: say so, stop.
- `WORKFLOW_INVALID` whose `issues[]` name a v1 construct (`path:
  version` with "v1 workflow", or hints that say `version: 2`): print
  `<name> is a v1 workflow; using the legacy conductor.` and follow
  `${CLAUDE_PLUGIN_ROOT}/references/legacy-conductor/run.md` from its
  §1 instead of the rest of this file. Any other issue: list
  `path: message (hint)` and stop.
- `requires_missing` non-empty: print one line per entry
  (`plugin:<name>` needs `/plugin install`, `tool:<name>` needs the
  binary on PATH) and stop; `wise_run` refuses with `REQUIRES_MISSING`
  until they are installed.

Render `questions` with one composite AskUserQuestion (four questions
per call at most): `choice` single-select with the default option
first, `multi` multiSelect with defaults listed first, `text` free text
with the default offered. Skip `locked: true` questions, `profile` when
known, and `input.<name>` filled positionally. Key answers by
question id.

## 3. Context and start

Build `context` from the conversation: `ticket[]` as `{ref, title,
body, url}` for tickets already fetched, `guidance` (operator text),
`decisions` settled here, `links`. Children never see the transcript;
include what they need, nothing they could not otherwise see.

`wise_run {workflow, cwd, answers, context, inputs, profile?}` returns
`run_id`. Print `Run <run_id> started (<workflow>).` `MISSING_ANSWERS`
lists required questions or inputs still without a value: ask them
with AskUserQuestion and call `wise_run` again.

## 4. Wait loop

`wise_wait {run_id, after}` with the default timeout; `after` is the
last `seq` seen (0 first). Never poll faster than the wait returns. If
the host moves a wait to a background task, its notification is the
wake-up: call again with the same `after`.

One line per event, never raw step output:

- `run.started`: the verdict.
- `step.started`: `> <step> (<harness> <model> <effort>)`.
- `step.progress`: `  <step>: <message>`.
- `step.done`: `ok <step>: <verdict>`.
- `warn`: `warn <step>: <message>`.
- `usage`: print nothing; add to running totals (input, output,
  cache_read, cache_write, cost_usd).
- `unit.phase` / `unit.done`: `<unit> <phase>` / `<unit>: <verdict>`.
- `gate.opened` / `gate.answered`: `gate <step> opened` / `answered`.

`gate` present: AskUserQuestion with `gate.message` and `gate.options`
(free text when `allow_text`), then `wise_answer {run_id, gate_id,
value}`. `GATE_STALE`: wait again. `done: true`: stop looping.
Ticket ids are markdown links when a URL is known.

## 5. Final report

Compact table from the collected events: step | verdict | harness and
model | tokens, then the run totals, then
`bash ${CLAUDE_PLUGIN_ROOT}/engine/engine.sh report <run_id>` for usage
by pool and harness plus per-step verdicts (`units` is empty until M4).
On `run.failed`: print `status` and `error` from `wise_status
{run_id}` and point to `/wise-workflow-resume <run_id>`.

## Rules

- Never print raw step output; logs live in the ledger.
- `wise_nudge` only when the user steers a running step;
  `wise_cancel` when the user aborts.
- Do not invoke other wise skills; the engine dispatches the steps.
