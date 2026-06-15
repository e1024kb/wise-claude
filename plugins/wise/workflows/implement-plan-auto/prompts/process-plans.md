# process-plans — the implement-plan-auto orchestrator

The per-plan pipeline driver for the `implement-plan-auto` workflow. Run
by the workflow's `process-plans` step (`type: interactive`, so this runs
inline in the conductor with full `Bash` / `Task` / `Read` access). It
loops over the plan-file list and, for each plan, drives re-plan →
implement → review → push → PR → watch in an isolated worktree — fully
autonomously, no prompts.

It is the plan-file analogue of `ticket-auto`'s `process-tickets.md`: the
only phase that differs is the first one (**re-plan from the provided
file** instead of fetch-a-ticket-and-author-a-plan). Every downstream
phase reuses `ticket-auto`'s shared prompts verbatim, so this workflow
and `ticket-auto` stay one implementation.

## Context the caller supplies

- `plan_list` — the plan files to process (semicolon-joined). Each entry
  is a path to a ready-made `PLAN-*.md` (relative paths resolve against
  `project.path`). Existence is NOT checked in preflight — §1 below checks
  each plan and fails just that one if it is missing.
- `plan_count` — how many.
- `config_prompt` — the operator's free-form standing guidance for the
  run (may be empty): preferred skills / libraries, guidelines,
  guardrails, files to avoid, knob overrides. Pass it verbatim to every
  per-plan phase below so the guidance reaches the actual work; never
  prompt the user about anything it implies — predict the answer and
  proceed, taking the max-value option for anything it leaves open.
- `project.path` — absolute path to the base repo.
- `project.name`, `project.kind`, `run.dir`, `workflow.dir` — run context.

Resolve the CI-fix cap once, up front: `MAX_FIX_ATTEMPTS` = the value
`config_prompt` names if it specifies one (e.g. "cap CI fixes at 4"),
else the default **10**. Pass it to the watch phase (§8).

## Context-budget rule (read first)

This step runs in the conductor's main conversation, so its tool output
accumulates. **Delegate every heavy or parallel sub-task to a `Task`
subagent** (its transcript releases on return) and keep only short
verdict lines in your own context. The re-plan phase, each implement
wave, every review↔fix cycle, the watch+fix loop, and the bot-review
fixes are all `Task`-delegated — each to the roster role named at that
step, brought in **fresh per phase** so its transcript releases on
return. Never inline heavy work directly in this step.

## Procedure

Keep a results table — one row per plan: `plan_file`, `branch`,
`worktree`, `pr_url`, `verdict`. Process plans **sequentially** (they are
independent; sequential keeps the context bounded).

For each `plan` in `plan_list`:

### 1. Resolve the plan + create the worktree

Resolve the plan path to an absolute path (if `plan` is relative, it is
relative to `{{project.path}}`), then confirm it exists and is readable:

```bash
case "$plan" in
  /*) SEED_PLAN="$plan" ;;
  *)  SEED_PLAN="{{project.path}}/$plan" ;;
esac
[ -r "$SEED_PLAN" ] || echo "MISSING: $SEED_PLAN"
```

If the plan file is missing or unreadable → record `verdict=failed
reason=missing` and **continue to the next plan** (a missing file never
aborts the batch).

Derive `<slug>` from the seed plan's filename — the branch name comes
**from the plan slug**, used verbatim with no prefix:

- take the basename without the `.md` extension
  (`docs/plans/001-api-caching.md` → `001-api-caching`);
- strip a leading `PLAN-` if present (`PLAN-PROJ-1.md` → `PROJ-1`);
- replace any character that is not `[A-Za-z0-9._-]` with `-`;
- if the result is empty or all digits, fall back to `plan-<digits>`.
- NEVER add a `ticket/` `jira/` `linear/` `feat/` `wise/` prefix or any
  `/` — the slug is used verbatim as `<branch>`.

Then, from the base repo (the worktree DIR keeps a descriptive,
collision-safe name; only the branch follows the rule above):

```bash
WT="{{project.path}}.wise-plan-<slug>"
git -C "{{project.path}}" worktree add "$WT" -b "<branch>"
```

If `git worktree add` fails (branch or dir already exists from a prior
run) → record the plan `verdict=failed reason=worktree`, and **continue
to the next plan**. Everything below runs against `$WT`.

The refreshed plan file lives in the **run directory** (not the
worktree) so it persists with the run state and never lands in the
branch. Establish it once per plan and ensure the dir exists:

```bash
PLAN_PATH="{{run.dir}}/plans/PLAN-<slug>.md"
mkdir -p "{{run.dir}}/plans"
```

### 2. Re-plan from the file

Dispatch a `Task` subagent — `subagent_type: wise:architect`,
`model: opus`, reason at **high** effort (the re-plan is this run's
autonomous decision spine) — : "Read
`{{workflow.dir}}/prompts/replan-from-file.md` and follow it." with
context `seed_plan=$SEED_PLAN`, `worktree=$WT`, `plan_path=$PLAN_PATH`,
`project.kind={{project.kind}}`, and `config_prompt={{config_prompt}}`.
It reads the provided plan as the seed, re-verifies it against the
worktree's current HEAD, writes the refreshed plan to `plan_path`, and
returns `PLAN: written=<path> type=<plan_type>`.
On failure → `verdict=failed reason=replan`, continue.

### 3. Implement

Dispatch a `Task` subagent — `subagent_type: wise:software-engineer`,
`model: sonnet`, reason at **high** effort — : "Read
`${CLAUDE_PLUGIN_ROOT}/workflows/ticket-auto/prompts/implement-plan.md`
and follow it." with `plan_path=$PLAN_PATH`, `worktree=$WT`,
`project.kind=<plan_type>`, and `config_prompt={{config_prompt}}`. (Its
own parallel per-task executors run as that fragment's fresh sub-`Task`s.)
It returns `IMPLEMENT: waves=… tasks=… done=… failed=…`. If `done=0` →
`verdict=failed reason=implement`, continue. If some tasks failed, note
it but proceed (the branch still gets reviewed in the next step).

### 4. Review ↔ fix loop (converge before push)

The implement phase already ran the simplify pass on each task commit.
Now — before anything is pushed — converge the branch through an
**independent reviewer and fixer**, cycling until the reviewer is
satisfied.

Resolve `MAX_REVIEW_CYCLES` up front (default **10**; `config_prompt`
may override it). Set `CYCLE=0`. Loop:

1. **Review.** Dispatch a `Task` subagent — `subagent_type:
   wise:code-reviewer`, `model: opus`, reason at **high** effort — : "Read
   `${CLAUDE_PLUGIN_ROOT}/workflows/ticket-auto/prompts/review-branch-auto.md`
   and follow it." with `worktree=$WT`, **`fixer=delegate`**,
   `ticket_ref=<slug>` (from §1, the plan slug stands in as the change's
   ref), `plan_path=$PLAN_PATH` (from §1), and
   `config_prompt={{config_prompt}}`. In `fixer=delegate` it reviews
   `origin/<base>..HEAD` (five lenses + confidence-scoring), REPORTS its
   bounded findings as a numbered block, applies nothing, and returns
   `REVIEW-AUTO: mode=delegate verdict=<clean|issues> findings=<n>`.
   - `REVIEW-AUTO: aborted …` → record `verdict=failed reason=code-review`
     and continue to the next plan (leave the loop).
   - `verdict=clean` → the branch passes the gate. **Leave the loop**, go to §5.
2. **Fix.** `verdict=issues`: dispatch a `Task` subagent — `subagent_type:
   wise:software-engineer`, `model: sonnet`, reason at **high** effort — :
   "Apply EXACTLY these review findings to the branch in `$WT`, nothing
   more:\n\n<paste the reviewer's numbered findings block>\n\nMake the
   concrete fix for each; respect the plan's deliberate decisions and the
   `config_prompt` guardrails; do not redesign or widen scope. Then commit
   by following
   `${CLAUDE_PLUGIN_ROOT}/skills/wise-commit/commit-routine.md` with
   `SIMPLIFY=no PUSH=no`." The fixer owns the edits + the fix commit; the
   reviewer never touches the code.
3. `CYCLE=CYCLE+1`. If `CYCLE < MAX_REVIEW_CYCLES`, loop back to step 1
   (the re-review verifies the fixes); otherwise exit the loop.

After the loop:
- **Converged** (`verdict=clean`) → proceed to §5.
- **Did NOT converge** within `MAX_REVIEW_CYCLES` → **push anyway**
  (continue to §5), but set `review=not-converged cycles=<MAX_REVIEW_CYCLES>`
  on the plan's row so §9's results table and the end-of-run report flag
  that the branch shipped with unresolved reviewer findings — left for the
  human + the CI/bot review (§7–§8) to catch.

### 5. Push the branch

From `$WT`, follow
`${CLAUDE_PLUGIN_ROOT}/skills/wise-commit/commit-routine.md` with
`PUSH=yes` `SIMPLIFY=yes` to sweep any final uncommitted state and commit
it (the branch was already code-reviewed in the prior step; this just
cleans + commits any stragglers). Then set upstream and push (the routine
refuses to auto-set upstream, so do it explicitly):

```bash
git -C "$WT" push -u origin "<branch>"
```

On push failure → `verdict=failed reason=push`, continue.

### 6. Create the PR

Read
`${CLAUDE_PLUGIN_ROOT}/workflows/ticket-auto/prompts/ensure-pr-auto.md`
and follow it with `current_branch=<branch>`, `project.path=$WT`. Capture
`PR-CREATE: number=<n> url=<url>`. On failure → `verdict=failed
reason=pr-create`, continue.

### 7. Request review

Read
`${CLAUDE_PLUGIN_ROOT}/workflows/ticket-auto/prompts/request-review-auto.md`
and follow it with `pr_number=<n>`, `project.path=$WT`. It attaches
Copilot **and** triggers CodeRabbit (`@coderabbitai review`); both
best-effort — a request failure never blocks. Capture
`REVIEW-REQUEST: copilot=<…> coderabbit=<…>`. The watch step (§8) does
the real confirmation: it detects each bot, waits for the head review,
and handles CodeRabbit's out-of-credits (bypass) / rate-limit
(retry-then-give-up) states.

### 8. Watch + fix

Dispatch a `Task` subagent — `subagent_type: wise:software-engineer`,
`model: sonnet` — : "Read
`${CLAUDE_PLUGIN_ROOT}/workflows/ticket-auto/prompts/watch-pipelines-auto.md`
and follow it." with `pr_number=<n>`, `pr_url=<url>`,
`current_branch=<branch>`, `project.path=$WT`,
`max_fix_attempts=$MAX_FIX_ATTEMPTS` (resolved up front),
`ticket_ref=<slug>` (from §1), `plan_path=$PLAN_PATH` (from §1), and
`config_prompt={{config_prompt}}`. It watches CI, auto-fixes failures,
waits for CodeRabbit / Copilot to finish reviewing, fixes or dismisses
every bot comment, and — when the PR is fully resolved — merges it.
Capture the `WATCH-AUTO:` verdict and record it as the plan's `verdict`
(`merged` / `all-green` / `blocked …` / `partial …` / `exhausted …` /
`human-intervention`).

### 9. Record and continue

Append the plan's row to the results table. The watch step already merged
the PR if it reached fully-green; this step merges nothing itself. Move
to the next plan. One stuck plan never aborts the run.

## Final output

After every plan, print the full per-plan results table (for the `report`
step to surface), then emit — as the FINAL line, alone, no markdown, no
backticks:

```
PLANS-DONE: processed=<N> merged=<M> open=<O> failed=<F>
```

where `merged` counts `merged` verdicts, `open` counts the PRs left open
for a human (`all-green` + `blocked` + `partial` + `human-intervention`),
and `failed` counts `failed` + `exhausted`.

## Guardrails

- Fully autonomous — never call `AskUserQuestion`.
- One worktree + branch + PR per plan. A PR is merged only by the watch
  step, only when fully green; everything else is left open.
- Worktrees are left in place for inspection — the `report` step lists
  the `git worktree remove` commands.
- A failure on one plan is recorded and the run continues with the next
  plan.
- All work runs inside this Claude Code session. Parallelism uses Claude
  Code's native `Task` and `TeamCreate` only — never shell out to
  `claude -p`, spawn a separate Claude Code process, or invoke any
  external agent / LLM CLI.
