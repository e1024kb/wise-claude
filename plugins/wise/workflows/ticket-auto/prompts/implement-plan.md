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

**2a. Dispatch executors in parallel.** For every task in the wave,
dispatch one `Task` subagent in a single message so they run
concurrently. Each subagent gets the `executor.md` persona prompt
(read it from this skill's `agents/executor.md`, or from
`${CLAUDE_PLUGIN_ROOT}/skills/wise-implement-plan-auto/agents/executor.md`)
parameterised with: the task description, the plan's Decisions +
codebase-context excerpt, `worktree`, and `project.kind`. Each
executor has **fresh context** — it sees only its task plus the
shared spec, never the other tasks' transcripts.

**Executors edit files but do NOT run git.** Parallel `git`
invocations on one worktree race the index. Each executor returns a
final line `TASK: <id> status=<done|failed> files=<comma-separated>`
plus a one-sentence summary of what it changed.

**2b. Simplify + commit each task sequentially.** After the wave's
subagents all return, the orchestrator processes each `done` task
**one at a time**, in wave order:

1. **Simplify the task's files.** Run the simplify pass (the
   `code-simplifier` agent) per
   `${CLAUDE_PLUGIN_ROOT}/references/simplify-pass.md`, scoped to that
   task's `files` (pass them as the explicit scope), so the cleanup
   lands in this task's commit and does not bleed into a sibling
   task's. On a simplify error, mark the task `failed` and continue —
   do not abort the wave.
2. **Commit.** Stage only that task's `files` (now including any
   simplify edits), draft a Conventional-Commits subject (Jira-scoped
   from the branch name when a key is present), `git commit`. One
   atomic commit per task.

The per-task simplify is the lightweight per-commit tier; the heavier
medium-depth code-review branch gate runs once over the whole branch
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
- Parallel executors are Claude Code's native `Task` subagents,
  running in this session. Never shell out to `claude -p`, spawn a
  separate Claude Code process, or invoke any external agent / LLM
  CLI to do the implementation.
