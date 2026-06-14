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
short verdict lines in your own context. The plan phase, each implement
wave, every review↔fix cycle, the watch+fix loop, and the bot-review
fixes are all `Task`-delegated — each to the roster role named at that
step, brought in **fresh per phase** so its transcript releases on
return. Never inline heavy work directly in this step.

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

Dispatch a `Task` subagent — `subagent_type: wise:architect`,
`model: opus`, reason at **high** effort (the plan is this run's autonomous
decision spine) — : "Read `{{workflow.dir}}/prompts/plan-ticket.md` and
follow it." with context `ticket=<ticket>`, `worktree=$WT`,
`plan_path=$PLAN_PATH`, `project.kind={{project.kind}}`, and
`config_prompt={{config_prompt}}`. It writes the
plan to `plan_path` and returns `PLAN: written=<path> type=<ticket_type>`.
On failure → `verdict=failed reason=plan`, continue.

### 3. Implement

Dispatch a `Task` subagent — `subagent_type: wise:software-engineer`,
`model: sonnet`, reason at **high** effort — : "Read
`{{workflow.dir}}/prompts/implement-plan.md` and follow it." with
`plan_path=$PLAN_PATH`, `worktree=$WT`, `project.kind=<ticket_type>`, and
`config_prompt={{config_prompt}}`. (Its own parallel per-task executors
run as that fragment's fresh sub-`Task`s.) It returns
`IMPLEMENT: waves=… tasks=… done=… failed=…`. If `done=0` →
`verdict=failed reason=implement`, continue. If some tasks failed,
note it but proceed (the branch still gets reviewed in the next step).

### 4. Review ↔ fix loop (converge before push)

The implement phase already ran the simplify pass on each task commit. Now —
before anything is pushed — converge the branch through an **independent
reviewer and fixer**, cycling until the reviewer is satisfied.

Resolve `MAX_REVIEW_CYCLES` up front (default **10**; `config_prompt` may
override it). Set `CYCLE=0`. Loop:

1. **Review.** Dispatch a `Task` subagent — `subagent_type:
   wise:code-reviewer`, `model: opus`, reason at **high** effort — : "Read
   `{{workflow.dir}}/prompts/review-branch-auto.md` and follow it." with
   `worktree=$WT`, **`fixer=delegate`**, `ticket_ref=<ticket_ref>` (from §1),
   `plan_path=$PLAN_PATH` (from §1), and `config_prompt={{config_prompt}}`.
   In `fixer=delegate` it reviews `origin/<base>..HEAD` (five lenses +
   confidence-scoring), REPORTS its bounded findings as a numbered block,
   applies nothing, and returns
   `REVIEW-AUTO: mode=delegate verdict=<clean|issues> findings=<n>`.
   - `REVIEW-AUTO: aborted …` → record `verdict=failed reason=code-review`
     and continue to the next ticket (leave the loop).
   - `verdict=clean` → the branch passes the gate. **Leave the loop**, go to §5.
2. **Fix.** `verdict=issues`: dispatch a `Task` subagent — `subagent_type:
   wise:software-engineer`, `model: sonnet`, reason at **high** effort — :
   "Apply EXACTLY these review findings to the branch in `$WT`, nothing
   more:\n\n<paste the reviewer's numbered findings block>\n\nMake the
   concrete fix for each; respect the plan's deliberate decisions and the
   `config_prompt` guardrails; do not redesign or widen scope. Then commit by
   following `${CLAUDE_PLUGIN_ROOT}/skills/wise-commit/commit-routine.md` with
   `SIMPLIFY=no PUSH=no`." The fixer owns the edits + the fix commit; the
   reviewer never touches the code.
3. `CYCLE=CYCLE+1`. If `CYCLE < MAX_REVIEW_CYCLES`, loop back to step 1 (the
   re-review verifies the fixes); otherwise exit the loop.

After the loop:
- **Converged** (`verdict=clean`) → proceed to §5.
- **Did NOT converge** within `MAX_REVIEW_CYCLES` → **push anyway** (continue
  to §5), but set `review=not-converged cycles=<MAX_REVIEW_CYCLES>` on the
  ticket's row so §9's results table and the end-of-run report flag that the
  branch shipped with unresolved reviewer findings — left for the human + the
  CI/bot review (§7–§8) to catch.

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
with `pr_number=<n>`, `project.path=$WT`. It attaches Copilot **and**
triggers CodeRabbit (`@coderabbitai review`); both best-effort — a
request failure never blocks. Capture
`REVIEW-REQUEST: copilot=<…> coderabbit=<…>`. The watch step (§8) does
the real confirmation: it detects each bot, waits for the head review,
and handles CodeRabbit's out-of-credits (bypass) / rate-limit
(retry-then-give-up) states.

### 8. Watch + fix

Dispatch a `Task` subagent — `subagent_type: wise:software-engineer`,
`model: sonnet` — : "Read `{{workflow.dir}}/prompts/watch-pipelines-auto.md`
and follow it." with `pr_number=<n>`, `pr_url=<url>`,
`current_branch=<branch>`, `project.path=$WT`,
`max_fix_attempts=$MAX_FIX_ATTEMPTS` (resolved up front),
`ticket_ref=<ticket_ref>` (from §1), `plan_path=$PLAN_PATH` (from §1), and
`config_prompt={{config_prompt}}`. It watches CI,
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
