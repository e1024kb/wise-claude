# implement-plan — execute a PLAN-*.md autonomously (executor agents or inline)

This fragment has **no model preference and no required tool**: it runs on
the model the caller already has (the tuning group the workflow pre-flight
selected, or the current session model), so the
[model fallback](../../../references/workflow-host-control.md#model-fallback)
picker never opens for it. Pick the dispatch route once in §2a from the
tools the session actually exposes; a missing subagent or team tool is a
normal environment, not an error.

Autonomously implement a written implementation plan in a git
working tree using a phase-gated executor pattern: the plan's task **waves**
are phase gates, each task in a wave is handed to a fresh-context
executor (in parallel when the session can dispatch subagents, one at a
time inline otherwise), and every task lands as one atomic commit.

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
  nobody is watching to un-stick a hung executor by hand. `yes` is
  honoured **only when the session exposes the team tools**
  (`TeamCreate`, background `Agent`, `SendMessage`, `Monitor`); on any
  other session (Codex, Cursor, Gemini, Grok, or a Claude child without
  them) it degrades to `no` with one log line — the engine's own stale
  policy (`stale_after`: nudge, then kill) is the watchdog there. The
  two paths differ ONLY in how §2a dispatches; §2b/§2c are identical.

## Procedure

### 1. Parse the plan

Read `plan_path`. From `## Tasks`, extract the ordered list of
**waves**; from each wave, the list of tasks (description, Reuse/New
classification, SP). Also read the plan's `## Decisions Made`,
`## Assumptions`, and codebase-context — that shared spec is passed
to every executor so they reuse existing assets. Read `## Validation`
for the verification commands.

When the session has `TodoWrite`, add one item per task so progress is
visible; otherwise keep the task list in your reply.

### 2. Process waves in order — each wave is a phase gate

For each wave, in plan order. Wave N+1 does not start until every
task in wave N is committed (or recorded `failed`). Within a wave the
tasks are independent by construction.

**2a. Run the wave's executors.** Each task gets the `executor.md` persona
prompt (read it from this skill's `agents/executor.md`, or from
`${CLAUDE_PLUGIN_ROOT}/skills/wise-implement-plan-auto/agents/executor.md`)
parameterised with: the task description, the plan's Decisions +
codebase-context excerpt, `worktree`, `project.kind`, and — when non-empty —
the `config_prompt` guidance (preferred skills / libraries, conventions,
guardrails, files to avoid) appended to the shared spec. Executors run on the
**current model** at the caller's effort; this fragment pins no model and
opens no picker.

Pick the route once per run from the tools the session exposes:

- **Route 1 — parallel subagents** (the session can dispatch a `Task` /
  `Agent` subagent). For every task in the wave, run one executor, all
  concurrently, inheriting the current model. Each executor has **fresh
  context** — it sees only its task plus the shared spec, never the other
  tasks' transcripts.
- **Route 2 — sequential inline** (no subagent tool: Codex, Cursor, Gemini
  and Grok children, or a Claude child without `Task`). Take the wave's
  tasks **one at a time, in plan order**, and for each apply `executor.md`
  as your own instructions in the current context: read only that task's
  spec, edit the files, then produce the same `TASK:` line before moving
  to the next task. Do not open the model-fallback picker, do not ask
  which route to use, and do not shell out to any agent CLI. Fresh context
  is not available inline; compensate by not carrying one task's
  scratch reasoning into the next.

Prepend the current repository instruction contract, including every applicable
CLAUDE.md and AGENTS.md file, to each executor prompt. Require each executor to
check for closer instruction files before touching a path and to pass the same
contract recursively if it delegates again. Native Task or Agent inheritance is
not a substitute for including the contract explicitly.

Under route 1, how the subagents are dispatched depends on `SUPERVISE`
(route 2 always behaves as `SUPERVISE=no`; there is nothing to supervise):

- **`SUPERVISE=no` (default).** Dispatch one `Task` subagent per task in a single
  message so they run concurrently, then wait for all to return. Simple, but a
  task that hangs mid-tool-call freezes this turn until it returns (`Task` has no
  timeout). No `worker-name`/`run.dir` is passed, so executors skip heartbeats.
- **`SUPERVISE=yes`, team tools present.** Run the wave as supervised
  background teammates, following
  `${CLAUDE_PLUGIN_ROOT}/references/supervise-loop.md`:
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
- **`SUPERVISE=yes`, team tools absent.** Log
  `supervise: degraded to no (no team tools on this session)` once and run
  the wave as `SUPERVISE=no`. Under the workflow engine the run's
  `stale_after` policy covers a hung child.

Either way, **executors edit files but do NOT run git** — parallel `git`
invocations on one worktree race the index (inline, the same rule keeps
the edit and the commit steps separate). Each executor returns a final line
`TASK: <id> status=<done|failed> files=<comma-separated>` plus a one-sentence
summary; under `SUPERVISE=yes` it also `TaskUpdate`s its task to `completed`/
`failed` so the supervisor knows it's done.

**2b. Simplify + commit each task sequentially.** After the wave's
subagents all return, the orchestrator processes each `done` task
**one at a time**, in wave order:

1. **Simplify the task's files.** Run the simplify pass per
   `${CLAUDE_PLUGIN_ROOT}/references/simplify-pass.md`, scoped to that
   task's `files` (pass them as the explicit scope), so the cleanup
   lands in this task's commit and does not bleed into a sibling
   task's. The reference picks the route: the `code-simplifier` agent
   when this Claude Code session lists it, otherwise the same cleanup
   inline on the current model per
   `${CLAUDE_PLUGIN_ROOT}/references/simplify-instructions.md` (Codex,
   Cursor, Gemini, Grok, or Claude without the plugin). A missing agent
   never blocks the wave and never opens a picker. On a simplify error
   (the pass ran and broke the tree), mark the task `failed`, do **not**
   stage, validate, or commit that task's files (the pass-failure
   policy forbids staging after a broken run), and continue with the
   next task — do not abort the wave.
2. **Commit.** Stage only that task's `files` (now including any
   simplify edits), draft a Conventional-Commits subject (scoped with any
   verified ticket reference suitable for a commit scope), `git commit`. One
   atomic commit per task.

The per-task simplify is the lightweight per-commit tier; the heavier
high-depth code-review branch gate runs once over the whole branch
after the implement phase (the caller's review step), before the push.

**2c. Verify each task.** After a task's commit, run the plan's
`## Validation` subset relevant so far (type-check / lint / the
task's tests), inferred by `project.kind` when the plan does not name
exact commands (`npm run typecheck && npm run lint` frontend;
`go build ./... && go vet ./...` backend; etc.). On failure: one
auto-fix attempt (route 1: a fresh `Task` executor; route 2: yourself,
inline, scoped to the failure), then re-commit (amend is
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
  orchestrator runs the per-task simplify (the `code-simplifier` agent
  or the inline pass, scoped to the task's files) and commits, serially.
- Never `git push` here — the caller's push step owns that.
- A failed task does not abort the run — finish the wave, flag it,
  carry on; the plan's wave ordering encodes the real dependencies.
- Never append an AI-attribution trailer to a commit.
- Executors are the session's own native subagents (`Task`, or under
  `SUPERVISE=yes` background `Agent` teammates) or, without a subagent
  tool, this context itself working sequentially. All run on the current
  model. Never shell out to `claude -p`, spawn a separate harness process,
  or invoke any external agent / LLM CLI to do the implementation or the
  supervision, and never stop because `Task`, the team tools or
  `TodoWrite` are missing.
