# supervise-loop — keep background workers on task (the watchdog)

Shared supervisor routine. Read by the `type: supervised-prompt` dispatch in
`wise-workflow-run` and by the standalone `/wise-supervise` skill. It is the
automation of the manual "ping all your subagents, are you still on track?"
nudge — a leader loop that detects a stalled or idle-but-unfinished worker and
prods it back to work, escalating only if prodding fails.

## Why this exists (and the one hard constraint)

A live watchdog is **only possible when the workers are addressable BACKGROUND
teammates** — spawned via `Agent(team_name=…, name=…, run_in_background: true)`
so the conductor's turn stays free to poll and `SendMessage`. In the default
blocking-`Task` model the conductor's turn is frozen until every Task returns,
so it cannot watch anything. Claude Code's `Task` has no timeout/heartbeat of
its own (a hung subagent can hang the orchestrator indefinitely), so the
supervision has to live here, at the orchestration layer.

Background teammates have one behaviour that is the whole reason this routine
is needed: **a teammate goes idle after every turn — that is normal, not an
error — and an idle teammate waits forever unless someone messages it.**
`SendMessage` to an idle teammate wakes it. So "a worker is stuck" almost
always means "a worker finished a turn, went idle, and nobody told it what to
do next." This loop is the someone.

## Two failure modes, two detectors

1. **Idle-and-forgotten.** The worker finished a turn and went idle, and its
   idle notification *did* wake you (the conductor) — you just have to act on
   it instead of treating idle as "done." Detector: **the idle notification
   itself** (§4). No polling needed.
2. **Hung-mid-turn.** The worker is stuck inside a long or blocked tool call
   and never emits an idle notification, so nothing wakes you. Detector: **the
   supervisor Monitor** (§3 + §5), an out-of-band poll of heartbeat files.

You need both. Idle notifications cover the first; the Monitor covers the second.

## 1. The liveness contract

Every supervised worker writes a heartbeat as the **first action of every
turn** and **after each significant tool call**, by shelling:

```bash
python3 "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/cursor}/scripts/workflows.py" worker-heartbeat \
  "<run.dir>" "<worker-name>" "<phase>" "<task-id>"
```

That refreshes `<run.dir>/workers/<worker-name>.hb` with the current UTC stamp.
The worker's spec (e.g. the executor persona) carries this instruction; you
just need to have passed each worker its name + `run.dir` when you spawned it.

`<run.dir>/workers/` is the per-worker heartbeat directory — distinct from
`state.yaml`'s `last_activity_at`, which tracks *conductor* liveness and is
useless as a per-worker signal. Per-worker files also avoid write contention.

## 2. Resolve the knobs once

At the start of a supervised wave, read the thresholds in one shell call:

```bash
python3 "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/cursor}/scripts/workflows.py" supervise-config
```
→ `{"stale_secs":180,"poll_secs":30,"max_nudges":2,"max_respawns":1}` (each
overridable via `WISE_WORKER_STALE_SECS` / `_POLL_SECS` / `_MAX_NUDGES` /
`_MAX_RESPAWNS`). Hold these in working memory for the wave. `stale_secs` is
deliberately tight (default 180s) — far tighter than the 1800s run-abandonment
window — because a worker silent for minutes with no heartbeat is hung, not
thinking.

## 3. Arm the supervisor Monitor

Spawn exactly **one** persistent Monitor per supervised wave. Its job is to
emit a line only when a worker looks hung — `stale-workers` is silent when all
workers are fresh, so the Monitor stays quiet until there is something to act on:

```text
Monitor({
  description: "wise supervisor: stale/hung workers in run <run.id> wave <N>",
  persistent: true,
  command:
    'RUN_DIR="<run.dir>"; EXP="<worker-1,worker-2,…>"; POLL=<poll_secs>; \
     while true; do \
       python3 "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/cursor}/scripts/workflows.py" stale-workers "$RUN_DIR" "$EXP" 2>&1 \
         || echo "SUPERVISOR-ERROR: stale-workers probe failed"; \
       sleep "$POLL"; \
     done'
})
```

`2>&1` routes a probe crash into the event stream (silence must never be
mistaken for "all healthy"). `EXP` is the comma-separated list of the worker
names you spawned, so a worker that never wrote a heartbeat surfaces as
`missing`, not just absent. **`TaskStop` this Monitor at wave end** (when every
task is `completed`/`failed`), before advancing — one Monitor per wave, never
leaked across waves.

## 4. On an idle notification (failure mode #1)

When a teammate idle notification arrives as a turn, do NOT assume "done."
`TaskGet` (or `TaskList`) the worker's task and branch on status:

- `completed` → genuinely finished. Collect its output (`TaskOutput`), mark the
  wave-slot done, no nudge.
- `in_progress`, heartbeat recent → it self-reported progress then went idle
  expecting the next instruction. **Re-nudge** with the next concrete step
  (or "continue with the remainder of task `<id>`").
- `in_progress`, no progress since spawn → ambiguous. Send the **status-check**
  template (§6), not a blind "continue."
- `blocked` (has `blockedBy`) → resolve the blocker or escalate; do not nudge
  into a wall.

## 5. On a Monitor stale/missing event (failure mode #2)

A line `<worker>\t<last-hb>\t<stale|missing>\t<age>` means that worker is hung.
Re-read its goal (its `TaskGet` body + the wave's step definition — cheap), then
`SendMessage` it and walk the escalation ladder (§7). Sending to an idle worker
wakes it; sending to a busy one queues — either way it gets prodded.
`SUPERVISOR-ERROR:` lines mean the probe itself failed — surface and re-arm.

## 6. Message templates

**Status-check (nudge 1):**
```text
to: <worker-name>
summary: status check on <task-id>
message: |
  Supervisor check on task <task-id> (<short goal>). You've been quiet for
  <age>s with no heartbeat. Reply in ONE line:
    PROGRESS: <what you just did>  /  BLOCKED: <what you need>  /  DONE: <result>
  Then continue. Write your heartbeat each step:
  python3 "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/cursor}/scripts/workflows.py" worker-heartbeat "<run.dir>" "<name>" "<phase>" "<task-id>"
```

**Directive re-nudge (nudge 2):**
```text
to: <worker-name>
summary: resume <task-id> now
message: |
  Resume task <task-id> NOW from where you stopped (<last known phase>). Do the
  next concrete action and write your heartbeat. If you are genuinely blocked,
  reply `BLOCKED: <reason>` — do not stay idle.
```

**Final warning before reclaim (logged):**
```text
to: <worker-name>
summary: reclaiming <task-id>
message: |
  No progress after <max_nudges> checks. I'm reclaiming task <task-id> and
  retrying it fresh. Stop work now.
```

## 7. The escalation ladder

Track a per-worker nudge count in working memory, keyed by name (display-only,
not persisted — like the wave counter; it does not survive resume).

```text
nudge 1            → SendMessage status-check (§6)        — "progressing? blocked?"
nudge 2            → SendMessage directive re-nudge (§6)  — "resume now, or say BLOCKED"
nudge ≥ max_nudges → TaskStop(<task_id>) → respawn a FRESH teammate for the same task
                     (TaskCreate stays; new Agent, name <worker>-r1) — retry up to max_respawns
respawn cap hit    → TaskUpdate status=failed → mark the wave-slot failed and surface
                     (wave-sync: tell the user; auto: record verdict=failed reason="hung")
```

A failed slot is not fatal: it maps onto wise's existing "a failed task does
not abort the run" rule — finish the wave, flag it, carry on.

**Reset the nudge count to 0** whenever the worker reports fresh progress
(a new heartbeat or a `PROGRESS:`/`DONE:` reply) — only *consecutive* silence
escalates, so a worker that was briefly slow is not punished later.

## 8. Teardown

When every task in the wave is `completed` or `failed`:
1. `TaskStop` the supervisor Monitor.
2. Collect each worker's output (`TaskOutput`) for the step result / log.
3. Shut workers down (`SendMessage {type: shutdown_request}`) and `TeamDelete`
   the team. Never reuse a teammate across tasks — spawn fresh per task so
   context does not accumulate (a blocking `Task` releases its transcript on
   return; a long-lived teammate does not).

## 9. Resume / leak cleanup

A conductor crash (compaction) can orphan a team + Monitor. On resume, before
re-dispatching, `TeamDelete` any team named `wise-<run-ulid>-*` and treat its
tasks as not-started — mirroring the idempotent worktree-reclaim discipline in
the auto-orchestrators' §1 (live state is the truth; a team the run did not
re-claim is torn down, not adopted).

## Guardrails

- The supervisor never does the worker's work — it only nudges, escalates, and
  collects. All file edits / git stay with the workers (and, for git, with the
  conductor's serial commit step), exactly as in the blocking model.
- One Monitor per wave; always `TaskStop` it before advancing. A leaked Monitor
  keeps polling a finished wave.
- First ladder rung is a **question, not a kill** — a worker inside one long
  legitimate tool call cannot heartbeat either, so never `TaskStop` on the
  first stale event.
- Background teammates are Claude Code native `Agent`/team subagents in this
  session (subscription-covered). Never shell out to `claude -p` or any
  external agent CLI to supervise or to work.
