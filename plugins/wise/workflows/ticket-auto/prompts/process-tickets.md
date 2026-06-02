# process-tickets — the ticket-auto orchestrator

The per-ticket pipeline driver for the `ticket-auto` workflow. Run by
the workflow's `process-tickets` step (`type: interactive`, so this
runs inline in the conductor with full `Bash` / `Task` / `Read`
access). It loops over the ticket list and, for each ticket, drives
plan → implement → review → push → PR → watch in an isolated
worktree — fully autonomously, no prompts.

## Context the caller supplies

- `ticket_list` — the tickets to process (semicolon-joined).
- `ticket_count` — how many.
- `config_prompt` — the operator's free-form standing guidance for the
  run (may be empty): preferred skills / libraries, guidelines,
  guardrails, files to avoid, knob overrides. Pass it verbatim to every
  per-ticket phase below so the guidance reaches the actual work; never
  prompt the user about anything it implies — predict the answer and
  proceed, taking the max-value option for anything it leaves open.
- `project.path` — absolute path to the base repo.
- `project.name`, `project.kind`, `workflow.dir` — run context.

Resolve the CI-fix cap once, up front: `MAX_FIX_ATTEMPTS` = the value
`config_prompt` names if it specifies one (e.g. "cap CI fixes at 4"),
else the default **10**. Pass it to the watch phase (§8).

## Context-budget rule (read first)

This step runs in the conductor's main conversation, so its tool
output accumulates. **Delegate every heavy or parallel sub-task to a
`Task` subagent** (its transcript releases on return) and keep only
short verdict lines in your own context. The plan phase, each
implement wave, and the bot-review fixes are all `Task`-delegated.
Never inline heavy work directly in this step.

## Procedure

Keep a results table — one row per ticket: `ref`, `branch`,
`worktree`, `pr_url`, `verdict`. Process tickets **sequentially**
(they are independent; sequential keeps the context bounded).

For each `ticket` in `ticket_list`:

### 1. Create the ticket worktree

Derive `tracker_slug` + `ticket_ref` cheaply from the ticket string
(full detection happens in the plan phase). Compute `<branch>` per
`${CLAUDE_PLUGIN_ROOT}/references/branch-naming.md` — the branch is the
**ticket ref EXACTLY, no prefix**: an acronym ref (`PROJ-777`) used
verbatim; a bare number (`#678`) → `abstract-task-678`; never `ticket/`
`jira/` `feat/` `wise/` or any `/`.

Then, from the base repo (the worktree DIR keeps a descriptive,
collision-safe name; only the branch follows the rule):

```bash
WT="{{project.path}}.wise-ticket-<tracker_slug>-<ticket_ref>"
git -C "{{project.path}}" worktree add "$WT" -b "<branch>"
```

If `git worktree add` fails (branch or dir already exists from a
prior run) → record the ticket `verdict=failed reason=worktree`, and
**continue to the next ticket**. Everything below runs against `$WT`.

The plan file lives in the **run directory** (not the worktree) so it
persists with the run state and never lands in the branch. Establish it
once per ticket and ensure the dir exists (`<ref>` is `ticket_ref` with
any leading `#` stripped):

```bash
PLAN_PATH="{{run.dir}}/plans/PLAN-<ref>.md"
mkdir -p "{{run.dir}}/plans"
```

### 2. Plan

Dispatch a `Task` subagent: "Read `{{workflow.dir}}/prompts/plan-ticket.md`
and follow it." with context `ticket=<ticket>`, `worktree=$WT`,
`plan_path=$PLAN_PATH`, `project.kind={{project.kind}}`, and
`config_prompt={{config_prompt}}`. It writes the
plan to `plan_path` and returns `PLAN: written=<path> type=<ticket_type>`.
On failure → `verdict=failed reason=plan`, continue.

### 3. Implement

Dispatch a `Task` subagent: "Read `{{workflow.dir}}/prompts/implement-plan.md`
and follow it." with `plan_path=$PLAN_PATH`,
`worktree=$WT`, `project.kind=<ticket_type>`, and
`config_prompt={{config_prompt}}`. It returns
`IMPLEMENT: waves=… tasks=… done=… failed=…`. If `done=0` →
`verdict=failed reason=implement`, continue. If some tasks failed,
note it but proceed (the branch still gets reviewed in the next step).

### 4. Review the branch (high-depth code-review)

The implement phase already ran the simplify pass on each task commit. Now —
before anything is pushed — run the heavyweight gate once over the whole
branch. Dispatch a `Task` subagent: "Read
`{{workflow.dir}}/prompts/review-branch-auto.md` and follow it." with
`worktree=$WT`, `ticket_ref=<ticket_ref>` (from §1),
`plan_path=$PLAN_PATH` (from §1), and `config_prompt={{config_prompt}}`.
It reviews `origin/<base>..HEAD` at
high effort (five reviewer lenses + a confidence-scoring pass), applies
the bounded findings, commits them, and returns
`REVIEW-AUTO: applied=<n> skipped=<m> committed=<yes|no>`.

On `REVIEW-AUTO: aborted …` (the review errored) → record
`verdict=failed reason=code-review`, continue to the next ticket.
Otherwise proceed — a `committed=yes` just means the gate added a fix
commit that the push step carries along.

### 5. Push the branch

From `$WT`, follow `${CLAUDE_PLUGIN_ROOT}/skills/wise-commit/commit-routine.md`
with `PUSH=yes` `SIMPLIFY=yes` to sweep any final uncommitted state and
commit it (the branch was already code-reviewed in the prior step; this
just cleans + commits any stragglers). Then set upstream and push (the routine refuses to
auto-set upstream, so do it explicitly):

```bash
git -C "$WT" push -u origin "<branch>"
```

On push failure → `verdict=failed reason=push`, continue.

### 6. Create the PR

Read `{{workflow.dir}}/prompts/ensure-pr-auto.md` and follow it with
`current_branch=<branch>`, `project.path=$WT`. Capture
`PR-CREATE: number=<n> url=<url>`. On failure → `verdict=failed
reason=pr-create`, continue.

### 7. Request review

Read `{{workflow.dir}}/prompts/request-review-auto.md` and follow it
with `pr_number=<n>`, `project.path=$WT`. Best-effort — a Copilot
attach failure never blocks. CodeRabbit auto-runs on the push; nothing
to attach.

### 8. Watch + fix

Read `{{workflow.dir}}/prompts/watch-pipelines-auto.md` and follow it
with `pr_number=<n>`, `pr_url=<url>`, `current_branch=<branch>`,
`project.path=$WT`, `max_fix_attempts=$MAX_FIX_ATTEMPTS` (resolved up
front), `ticket_ref=<ticket_ref>` (from §1),
`plan_path=$PLAN_PATH` (from §1), and `config_prompt={{config_prompt}}`.
It watches CI,
auto-fixes failures, waits for CodeRabbit / Copilot to finish
reviewing, fixes or dismisses every bot comment, and — when the PR is
fully resolved — merges it. Capture the `WATCH-AUTO:` verdict and
record it as the ticket's `verdict` (`merged` / `all-green` /
`blocked …` / `partial …` / `exhausted …` / `human-intervention`).

### 9. Record and continue

Append the ticket's row to the results table. The watch step already
merged the PR if it reached fully-green; this step merges nothing
itself. Move to the next ticket. One stuck ticket never aborts the
run.

## Final output

After every ticket, print the full per-ticket results table (for the
`report` step to surface), then emit — as the FINAL line, alone, no
markdown, no backticks:

```
TICKETS-DONE: processed=<N> merged=<M> open=<O> failed=<F>
```

where `merged` counts `merged` verdicts, `open` counts the PRs left
open for a human (`all-green` + `blocked` + `partial` +
`human-intervention`), and `failed` counts `failed` + `exhausted`.

## Guardrails

- Fully autonomous — never call `AskUserQuestion`.
- One worktree + branch + PR per ticket. A PR is merged only by the
  watch step, only when fully green; everything else is left open.
- Worktrees are left in place for inspection — the `report` step
  lists the `git worktree remove` commands.
- A failure on one ticket is recorded and the run continues with the
  next ticket.
- All work runs inside this Claude Code session. Parallelism uses
  Claude Code's native `Task` and `TeamCreate` only — never shell out
  to `claude -p`, spawn a separate Claude Code process, or invoke any
  external agent / LLM CLI.
