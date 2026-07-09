# implement-plan — execute a PLAN-*.md autonomously (parallel executor agents)

Autonomously implement a written implementation plan in a git
working tree using a phase-gated executor pattern: the plan's task **waves**
are phase gates, each task in a wave is handed to a fresh-context
executor agent running in parallel, and every task lands as one
atomic commit.

Source of truth for the `/wise-implement-plan-auto` skill and the
`ticket-auto` workflow's implement phase.

## Context the caller supplies

- `plan_path` — absolute path to the `PLAN-*.md` to implement.
- `worktree` — absolute path to the git worktree to implement in.
- `project.kind` — `frontend | backend | fullstack | other`, if known.
- `config_prompt` — **optional** operator standing guidance (may be
  empty): skills / libraries to prefer, conventions, guardrails, files
  to avoid. Passed into each executor's shared spec so the hands-on
  work honors it. The plan already baked most of it into its
  `## Decisions Made`; this carries it through to the edits.
- `SUPERVISE` — `yes` | `no` (default `no`). When `yes`, the wave's
  executors run as **supervised background teammates** (a leader loop
  nudges any that hang or go idle-without-finishing) instead of blocking
  `Task` subagents. The autonomous (`-auto`) callers pass `yes` —
  nobody is watching to un-stick a hung executor by hand; interactive
  `/wise-implement-plan-auto` leaves it `no` (the user is present). The
  two paths differ ONLY in how §2a dispatches; §2b/§2c are identical.

## Procedure

### 1. Parse the plan

Read `plan_path`. From `## Tasks`, extract the ordered list of
**waves**; from each wave, the list of tasks (description, Reuse/New
classification, SP). Also read the plan's `## Decisions Made`,
`## Assumptions`, and codebase-context — that shared spec is passed
to every executor so they reuse existing assets. Read `## Validation`
for the verification commands.

`TodoWrite` one item per task so progress is visible.

### 2. Process waves in order — each wave is a phase gate

For each wave, in plan order. Wave N+1 does not start until every
task in wave N is committed (or recorded `failed`). Within a wave the
tasks are independent by construction.

**2a. Dispatch executors in parallel.** For every task in the wave, run one
executor, all concurrently. Each gets the `executor.md` persona prompt (read it
from this skill's `agents/executor.md`, or from
`${WISE_PLUGIN_ROOT}/skills/wise-implement-plan-auto/agents/executor.md`)
parameterised with: the task description, the plan's Decisions +
codebase-context excerpt, `worktree`, `project.kind`, and — when non-empty —
the `config_prompt` guidance (preferred skills / libraries, conventions,
guardrails, files to avoid) appended to the shared spec. Each executor has
**fresh context** — it sees only its task plus the shared spec, never the other
tasks' transcripts.

How they're dispatched depends on `SUPERVISE`:

- **`SUPERVISE=no` (default).** Dispatch one `Task` subagent per task in a single
  message so they run concurrently, then wait for all to return. Simple, but a
  task that hangs mid-tool-call freezes this turn until it returns (`Task` has no
  timeout). No `worker-name`/`run.dir` is passed, so executors skip heartbeats.
- **`SUPERVISE=yes`.** Run the wave as supervised background teammates, following
  `${WISE_PLUGIN_ROOT}/references/supervise-loop.md`:
  1. `TeamCreate({ team_name: "wise-<run.id>-impl-w<N>" })` for the wave.
  2. For each task: `TaskCreate` its goal, then spawn a background worker —
     `Agent({ team_name, name: "exec-<task-id>", run_in_background: true,
     subagent_type: "general-purpose" (or the role the caller pins),
     prompt: <executor.md spec, parameterised as above, PLUS its `worker-name`
     = `exec-<task-id>` and `run.dir` so it heartbeats} )`.
  3. Arm the supervisor Monitor (§3) over the wave's worker names and run the
     loop (idle §4 + Monitor §5 + ladder §7) until every task reports
     `done`/`failed` via its `TaskUpdate` + final `TASK:` line.
  4. Teardown (§8): `TaskStop` the Monitor, collect each worker's `TaskOutput`,
     shut them down, `TeamDelete`. On resume, reclaim per §9.

Either way, **executors edit files but do NOT run git** — parallel `git`
invocations on one worktree race the index. Each executor returns a final line
`TASK: <id> status=<done|failed> files=<comma-separated>` plus a one-sentence
summary; under `SUPERVISE=yes` it also `TaskUpdate`s its task to `completed`/
`failed` so the supervisor knows it's done.

**2b. Simplify + commit each task sequentially.** After the wave's
subagents all return, the orchestrator processes each `done` task
**one at a time**, in wave order:

1. **Simplify the task's files.** Run the simplify pass (the
   `code-simplifier` agent) per
   `${WISE_PLUGIN_ROOT}/references/simplify-pass.md`, scoped to that
   task's `files` (pass them as the explicit scope), so the cleanup
   lands in this task's commit and does not bleed into a sibling
   task's. On a simplify error, mark the task `failed` and continue —
   do not abort the wave.
2. **Commit.** Stage only that task's `files` (now including any
   simplify edits), draft a Conventional-Commits subject (Jira-scoped
   from the branch name when a key is present), `git commit`. One
   atomic commit per task.

The per-task simplify is the lightweight per-commit tier; the heavier
high-depth code-review branch gate runs once over the whole branch
after the implement phase (the caller's review step), before the push.

**2c. Verify each task.** After a task's commit, run the plan's
`## Validation` subset relevant so far (type-check / lint / the
task's tests), inferred by `project.kind` when the plan does not name
exact commands (`npm run typecheck && npm run lint` frontend;
`go build ./... && go vet ./...` backend; etc.). On failure: one
auto-fix attempt by a fresh `Task` executor, then re-commit (amend is
fine here — the commit has not been pushed). Still failing → keep the
commit (so the diff is inspectable), mark the task `failed`, flag the
wave, and continue.

### 3. Final line

FINAL line — alone, no markdown, no backticks — MUST match:

```
IMPLEMENT: waves=<w> tasks=<t> done=<d> failed=<f>
```

## Guardrails

- Implement only in `<worktree>` — never touch the base repo or
  another ticket's worktree.
- One atomic commit per task — never bundle tasks, never one giant
  commit.
- Executors never run `git` and never simplify — they only edit. The
  orchestrator runs the per-task simplify (the `code-simplifier` agent,
  scoped to the task's files) and commits, serially.
- Never `git push` here — the caller's push step owns that.
- A failed task does not abort the run — finish the wave, flag it,
  carry on; the plan's wave ordering encodes the real dependencies.
- Never append an AI-attribution trailer to a commit.
- Parallel executors are Claude Code's native `Task` subagents (or, under
  `SUPERVISE=yes`, native background `Agent` teammates), running in this
  session. Both are subscription-covered. Never shell out to `claude -p`, spawn
  a separate Claude Code process, or invoke any external agent / LLM CLI to do
  the implementation or the supervision.
