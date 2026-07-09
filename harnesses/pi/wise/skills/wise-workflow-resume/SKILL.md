---
name: wise-workflow-resume
description: >-
  Resume an interrupted or paused workflow run by ULID in the current
  workspace. Loads the run's state.yaml, re-tags it with the current
  Claude Code session, resets any in-flight steps to pending, and
  re-enters the workflow-run conductor's main loop. Invoked as
  `/wise-workflow-resume`. Use when the user says "resume the workflow",
  "continue the paused run", "pick up the run", "resume run <ulid>", or
  types `/wise-workflow-resume`.
---

## Harness adaptation note

This skill was authored for Claude Code and adapted for Pi. Where the steps below reference Claude-specific tools, substitute:

- **Task / subagent dispatch (`subagent_type: wise:<role>`)** — Pi has no subagents: adopt the role card at `${WISE_PLUGIN_ROOT}/agents/<role>.md` yourself and perform the steps sequentially.
- **AskUserQuestion** — ask the user the same question in plain chat and wait for their reply.
- **Skill tool (`/wise-*`)** — invoke the skill as `/skill:wise-<name>`, or open and follow its `SKILL.md` directly.
- **TodoWrite** — keep a visible checklist in your replies instead.
- **Shared files (`${WISE_PLUGIN_ROOT}`)** — defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/pi`, where `./install.sh pi` puts this pack; export `WISE_PLUGIN_ROOT` only to override. If neither the env var nor the default path exists, use the pack this skill ships in — two levels up from the directory containing this SKILL.md, i.e. `../../` from it (a `pi install`ed pack stays intact).

### Running workflows on Pi

Same execution model as `/wise-workflow-run`: resolve
`${WISE_PLUGIN_ROOT}` (it defaults to this pack's install directory —
see the shared-files bullet above), ensure the prerequisites (Python 3 +
`pyyaml` + `python-ulid`, `git`, `gh`), and map each step type to its
Pi primitive — Pi ships no subagents, so `prompt` steps are done by
adopting the role in-context and team steps run sequentially.
`ask`/`approval` become plain-chat questions. Resume works because the
engine tags runs with a synthetic per-workspace session id — no Claude
transcript required.

When composing a step prompt from a workflow's `prompts/*.md`,
substitute `${WISE_PLUGIN_ROOT}` with its resolved value — the
executing context is fresh and never saw this note.


# /wise-workflow-resume — resume an interrupted run

## Why this skill exists

A run can stop mid-execution for several reasons — the user picked
`Pause` in a wave-sync prompt, the Claude Code session closed while
a step was in flight, or a step failed and the user wants to try
again after fixing something out of band. The run directory and
`state.yaml` survive all of these. This skill picks the run back up
where it left off.

It shares almost all of its loop with `wise-workflow-run`; the key
differences are the preamble (load state instead of build state,
reset in-flight steps) and the fact that pre-flight prompts are
**skipped** — control mode and worktree choice were already made and
are recorded in `state.yaml`.

## Arguments

Read `$ARGUMENTS`. The first whitespace-separated token is the
`run-id` ULID. When `$ARGUMENTS` is empty, [§2](#2-resolve-the-run-id)
prompts the user to pick from the workspace's non-terminal runs.

- `run-id` — ULID of the run under `$RUNS_ROOT/<run-id>/` (where
  `$RUNS_ROOT` is `$(python3 .../workflows.py runs-root)` — resolves
  to `~/.local/share/wise/runs/<cwd-slug>/` by default). When absent,
  [§2](#2-resolve-the-run-id) prompts the user to pick from the
  workspace's non-terminal runs. If `$ARGUMENTS` is empty and no
  resumable runs exist in the workspace, stop with an error pointing
  at `/wise-workflow-status` to list runs on disk.

## Procedure

### 1. Init-check + resolve — in ONE message

Run the init-check per `${WISE_PLUGIN_ROOT}/references/init-check.md`,
firing `init-registry.py check` together with the data call for your
mode, in one message:

- **`$ARGUMENTS` empty** (user picks interactively) — data call
  `workflows.py list-resumable-runs`, plus a `select:AskUserQuestion`
  ToolSearch so the §2 picker is ready.
- **`$ARGUMENTS` non-empty** — data call `workflows.py runs-root`;
  capture its stdout as `$RUNS_ROOT`. Validate the ULID shape (26
  chars, Crockford base-32 `[0-9A-HJKMNP-TV-Z]`) and verify
  `$RUNS_ROOT/<run-id>/state.yaml` exists — reject a bad shape with a
  one-line error, a missing state file with
  `No run found for ULID <run-id> in this workspace.`

On `INIT:ok`, use the output directly. Otherwise follow the reference's
fallback; this skill's resolve step is read-only, so on
`BOOTSTRAP:need-python` relay and stop, and re-run the data call(s) on
`READY`.

### 2. Resolve the run ID (when absent)

Now interpret the `list-resumable-runs` output captured in §1.

stdout is a JSON array of `{run_id, workflow_name, status,
last_activity_at, session_label, claude_session_id}`, sorted most
recent first. If empty, stop with:

```
No resumable runs in this workspace. Runs you can resume are those
in `paused`, `failed`, `initializing`, or (historically) `running`
state. Terminal runs (completed, cancelled) stay on disk under
~/.local/share/wise/runs/<cwd-slug>/<run-id>/ for reference but
can't be re-entered.
```

Otherwise `AskUserQuestion`:

- Question: `Which run do you want to resume?`
- Header: `Resume run`
- One option per entry. Label: the `session_label` if set, otherwise
  the `run_id`. Description:
  `<workflow_name> — <status>, last activity <last_activity_at>`.
  Add a final `Abort` option.

On pick, set `run-id` to the chosen `run_id` and continue. On
`Abort`, stop cleanly.

### 3. Inspect the run

```bash
python3 "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/pi}/scripts/workflows.py" dump-state \
  "$RUNS_ROOT/<run-id>/state.yaml"
```

Pull the `status`, `workflow_name`, `control_mode`, `worktree`,
`steps`. Show the user a short summary (workflow name, status, which
step(s) were in flight, how long ago the last activity was).

If `status` is `completed` or `cancelled`, tell the user the run is
already terminal and stop. Resume is only meaningful on `paused`,
`failed`, `initializing`, or (historically) `running`.

### 4. Re-tag the session (if it changed)

`wise-workflow-run` [§5](../wise-workflow-run/SKILL.md#5-generate-the-run-id-tag-the-session-write-stub-state)
recorded the Claude Code session UUID the run was started in so
`state.yaml` always names its current host session. Resume doesn't
try to send the user back to the original session — a skill can't
invoke `/resume` on the user's behalf, so anything the skill does
here either blocks the run (printing "run this yourself") or just
gets in the way. Instead, resume silently re-tags: whatever session
the user is in now IS the new host, and a one-line note tells them
what happened.

Get the current session and the stored one:

```bash
CURRENT=$(python3 "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/pi}/scripts/workflows.py" current-session-id || true)
```

Read `claude_session_id` and `session_label` from the state.yaml you
already dumped in [§3](#3-inspect-the-run).

Decision tree (no prompts, ever):

- `STORED` absent/null **or** equal to `CURRENT` → say nothing, fall
  through to [§5](#5-re-resolve-the-definition). (Legacy runs and
  happy-path resumes land here.)
- Otherwise → overwrite the stored session and note it:

  ```bash
  python3 "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/pi}/scripts/workflows.py" update-run \
    "$RUNS_ROOT/<run-id>/state.yaml" claude_session_id=$CURRENT
  ```

  Then emit a single one-line info to the user — worded according to
  whether the original session's `.jsonl` is still on disk:

  ```bash
  python3 "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/pi}/scripts/workflows.py" session-path "$STORED"
  ```

  - Exit 0 (original still on disk):
    `(Previously started in session <stored-label or stored-uuid>; continuing here.)`
  - Exit 2 (original wiped):
    `(Previously started in session <stored-label or stored-uuid>, which is no longer available; continuing here.)`

  Either way, fall through to [§5](#5-re-resolve-the-definition).
  No `AskUserQuestion`, no "run /resume yourself" — the user
  already gave their intent by typing `/wise-workflow-resume` in this
  session.

### 5. Re-resolve the definition

The on-disk definition may have been edited (or even removed) since
the run started. You already have `workflow_name` from the §3
`dump-state` output (read it with the `Read` tool if not). Resolve
the definition path:

```bash
DEF=$(python3 "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/pi}/scripts/workflows.py" locate-def "<workflow_name>")
```

If the definition is gone, stop with:
```
Workflow <name> no longer exists in bundled or user definitions.
Re-create it with /wise-workflow-create <name>, or remove this run's
directory if you want to discard it:
  rm -rf $RUNS_ROOT/<run-id>/
```

### 6. Reset in-flight steps

```bash
python3 "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/pi}/scripts/workflows.py" reset-running \
  "$RUNS_ROOT/<run-id>/state.yaml"
```

This moves any step with `status: running` back to `pending` and
clears its `started_at`, `run_id`, `log` fields so a fresh attempt
gets its own step-run-ulid and log file. Previous logs stay on disk
for debugging.

If the interrupted run had any `supervised-prompt` steps (or invoked
the supervisor loop), a crash can orphan a background team + its
Monitor. Per `${WISE_PLUGIN_ROOT}/references/supervise-loop.md` §9,
`TeamDelete` any team named `wise-<run-id>-*` before re-dispatching —
live state is the truth, an orphaned team is torn down, not adopted.
The supervised step re-runs whole (it is atomic); stale heartbeat files
under `<run-dir>/workers/` are harmless (the fresh worker overwrites
its own).

### 7. TodoWrite (re-emit)

Read the resumed state. `TodoWrite` one todo per step, reflecting its
current status:

- `completed` / `skipped` / `cancelled` → `completed` / `cancelled`
- `failed` → `cancelled`
- `pending` → `pending`
- `running` — should not happen after [§6](#6-reset-in-flight-steps); treat as `pending` if it
  does.

Tell the user: `Resuming run <run-id> (<workflow-name>) — <N>
steps remaining.`

### 8. Re-enter the main loop

From here on, behaviour is identical to `wise-workflow-run` [§10](../wise-workflow-run/SKILL.md#10-main-loop) and [§11](../wise-workflow-run/SKILL.md#11-finalise) —
**including the turn-continuity rule** (every message ends with a
tool call; prose is bundled with the tool call that follows it) and
**the per-step reporting format** (9d announcements + 9e outcome
lines). A resumed run produces the same live chat output as a fresh
run; the only user-visible difference is the "Resuming run <id>…"
preamble from §7 instead of "Run <id> started".

Run the same algorithm against the existing `state.yaml`:

- Call `next-wave` for runnable steps.
- Apply `to_skip` (with 9b's skip-report prose).
- Announce the wave (9d) and dispatch runnable steps in a single
  message.
- Collect, score, log, update state (9e).
- In wave-sync mode (state.control_mode), yield between waves with
  the 9g menu.
- In synchronous or auto-advance mode, skip 9g and bundle the next
  `next-wave` call into the same message as 9e's results.
  (auto-advance still honors in-step prompts — asks, approvals, and
  AskUserQuestion inside interactive steps; only synchronous
  suppresses them.)
- On terminal state, write the final `update-run` and print the
  summary.

Refer to `wise-workflow-run/SKILL.md` for the exact step dispatch rules
and the full turn-continuity note.

## Stuck-run takeover

A run can wedge in `status: running` with no session driving it — classically
when a long autonomous run (`ticket-auto` / `impl-plan-auto`) is
**orphaned by a context compaction** partway through its `process-*` step: the
per-unit work is committed in the worktree but never pushed, no PR, and the run
never reached its `until:` line. Such a run is still resumable here — [§3](#3-inspect-the-run)
admits a `running` run and [§6](#6-reset-in-flight-steps)'s `reset-running` flips
its in-flight step back to `pending`.

Just resume it: `/wise-workflow-resume <run-ulid>` (or bare, then pick it from
the list). Those orchestrators are **idempotent on resume** — `process-tickets`
/ `process-plans` re-enter from the top, re-derive each unit's branch +
worktree, **adopt** whatever already exists (worktree, branch, pushed commits,
open PR) via live `git`/`gh` probes, and continue each unit from where it left
off — pushing committed-but-unpushed work, finding or creating the PR, watching,
and merging. No manual push / PR / merge is needed; recovery is the normal
resume path. (A worktree/branch a *different* run created is left untouched.)

If a run is genuinely unrecoverable, the manual escape hatches still apply:
re-arm a single step with `update-step <id> status=pending` (via
`/wise-workflow-status`), or discard the run entirely with
`rm -rf "$RUNS_ROOT/<run-id>/"`.

## Guardrails

- Never re-run a step already in `completed` / `failed` / `skipped`
  / `cancelled` state. Resume re-executes only `pending` work.
  If the user wants to re-run a failed step, they use the `Modify`
  option in wave-sync or manually `update-step <id> status=pending`
  via `/wise-workflow-status` workflows — not via resume.
- Never skip pre-flight — but also never prompt pre-flight questions
  here. Control mode and worktree are taken from state.yaml.
- Same invariant exception as `wise-workflow-run`: this skill may invoke
  wise action skills via `Skill`, but only as part of validated
  `type: skill` steps.
