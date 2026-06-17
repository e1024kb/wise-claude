---
name: wise-supervise
description: >-
  Attach a supervisor / watchdog loop to a running team of background agents and
  keep them on task — automates the manual "ping all your subagents, are you
  still on track?" nudge. Reads the team's members, actively probes each for its
  status, nudges any that are idle-but-unfinished or off-goal, and escalates a
  worker that stays stuck (TaskStop + surface). When the team's workers write
  heartbeats it also arms a background `stale-workers` Monitor to catch ones hung
  mid-turn. Invoked as `/wise-supervise` (bare alias) or `/wise:wise-supervise`
  (canonical), optionally with a team name. Use when the user says "ping all my
  subagents", "are my agents stuck", "supervise the team", "watch the agents",
  "nudge the stuck workers", "check my subagents are on track", or types
  `/wise-supervise`.
argument-hint: "[team-name]"
allowed-tools: Read, Bash(python3:*), SendMessage, Monitor, TaskList, TaskGet, TaskOutput, TaskStop
---

# /wise-supervise — keep a running team of agents on task

## Why this skill exists

Background teammates **go idle after every turn** and then wait forever unless
someone messages them. So a workflow or ad-hoc team can stall not because a
worker failed but because nobody told it what to do next — and today the only
fix is the user typing "ping all your subagents, verify they're on track." This
skill is that nudge, automated and repeatable: it probes the team, prods the
stuck members, and escalates the ones that stay stuck.

It runs the shared supervisor routine in
`${CLAUDE_PLUGIN_ROOT}/references/supervise-loop.md` — read that first; this
skill is the standalone, attach-to-any-team entry point for it. The workflow
engine runs the same routine automatically for `type: supervised-prompt` steps.

## Invocation

```
/wise-supervise                 # supervise the single active team (or ask which)
/wise-supervise ticket-pr-run   # supervise a named team
/wise:wise-supervise <team>     # canonical namespaced form
```

The positional, if present, is the team name. Anything else is ignored prose.

## Procedure

### 1. Resolve the team

If a team name was given, use it. Otherwise read which teams exist and pick the
single active one; if there is more than one, ask the user which to supervise
(this is the one allowed prompt — the rest of the loop is decision-free unless a
worker needs a human call). Read the team's members from its config:

```
Read ~/.claude/teams/<team-name>/config.json
```

The `members[]` array gives each teammate's `name` (always address members by
name) and `agentType`. The team-lead entry is you / the conductor — supervise
the worker members, not yourself.

### 2. Pick the detection mode

- **Heartbeat-aware** — if the run exposes a `workers/` heartbeat dir (a team
  spawned under the `supervised-prompt` contract, with a known `run.dir`): arm
  the supervisor Monitor per `supervise-loop.md §3` so workers hung mid-turn
  surface on their own, AND active-probe (below).
- **Legacy / ad-hoc team** (e.g. `ticket-pr-run` with `engineer-1/2/3`, spawned
  before the heartbeat contract): no heartbeat files, so out-of-band hung
  detection isn't available — use **active probing** as the primary mechanism.

### 3. Active-probe round

`TaskList` to see each member's current task + status. Then `SendMessage` every
member that is not `completed` a status-check (the template in
`supervise-loop.md §6`): ask for ONE line — `PROGRESS:` / `BLOCKED:` / `DONE:` —
and tell them to continue. Their replies arrive automatically as turns.

Classify each reply against the member's task goal (`TaskGet`):
- on track / `PROGRESS:` / `DONE:` → leave it; reset its nudge count.
- idle with no reply, or `in_progress` with no progress → it's the
  idle-and-forgotten case: re-nudge with the next concrete instruction.
- `BLOCKED:` → surface the blocker; resolve it or escalate to the user.
- off-goal (doing something other than its task) → redirect with a corrective
  message naming the actual task.

### 4. Escalate the persistently stuck

Walk the ladder in `supervise-loop.md §7`, tracking a per-worker nudge count:
status-check → directive re-nudge → `TaskStop` and surface to the user.
(Respawn-on-fail belongs to the workflow path, which still holds the worker
spec; for an attached ad-hoc team, stop the hung worker and report it so the
user — or the owning run — can re-dispatch.)

### 5. Loop until settled

Repeat probe → classify → nudge/escalate until every member's task is
`completed` or `failed`, the user stops the supervision, or no member has made
progress across two full rounds (the no-progress safety catch — stop and report
rather than nudge forever). Then `TaskStop` any Monitor you armed and give the
final summary.

### 6. Final line

```
SUPERVISE: team=<name> members=<m> nudged=<n> stuck=<s> done=<d>
```

## Guardrails

- Read-and-nudge only — never do a worker's task yourself, never edit files or
  run git on their behalf. You coordinate; they work.
- One Monitor at most, and always `TaskStop` it before exiting — a leaked
  Monitor keeps polling a finished team.
- The first nudge is a question, not a kill. Never `TaskStop` a worker on its
  first quiet round — it may be inside one long legitimate tool call.
- Do not `SendMessage {type: shutdown_request}` unless the user asks to shut the
  team down — supervising a team is not the same as ending it.
- Never invoke another wise action skill. Never shell out to `claude -p` or any
  external agent CLI.
