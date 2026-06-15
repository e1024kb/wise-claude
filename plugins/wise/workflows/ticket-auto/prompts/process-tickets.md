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
- `project.name`, `project.kind`, `run.dir`, `workflow.dir` — run context.

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

Process tickets **sequentially** (they are independent; sequential keeps the
context bounded). Per-ticket progress is checkpointed to a small ledger under the
run directory, which makes this step **idempotent on resume**: if a context
compaction orphans it mid-run, `/wise-workflow-resume` re-enters here from the
top, re-derives the same branch + worktree for each ticket, and continues each one
from where it left off — instead of colliding on the existing worktree and failing
(the orphan bug this design fixes).

### Ledger — per-ticket checkpoint + ownership token

Each ticket gets one line-oriented `KEY=VALUE` file under `{{run.dir}}/units/`,
named by its `<branch>` (a legal git ref — no `/`, so a safe flat filename). It
lives off the git tree and **survives interrupt + resume**. Its existence is this
run's **claim** on the ticket: present → this run owns it (adopt + resume);
absent while a worktree/branch already exists on disk → another run or a stale
leftover owns it (skip — never stomp). **Live `git`/`gh` state is always the
source of truth; the ledger is only a hint** (to skip already-finished phases and
to recognise the merged-then-cleaned terminal case, where nothing live remains).

Fields: `ref`, `branch`, `worktree`, `base`, `pr_url`, `pr_number`, `last_phase`
(`claiming` → `worktree` → `planned` → `implemented` → `reviewed` → `pushed` →
`pr` → `review-requested` → `watched`), `verdict`, `reason`, `review`, `cleaned`.

The ledger is **append-only** — to checkpoint, append one `KEY=VALUE` line; a
later line for the same key overrides an earlier one (readers take the **last**
match), so this is safe even if the shell does not persist between commands:

```bash
UNITS_DIR="{{run.dir}}/units"; mkdir -p "$UNITS_DIR"
# checkpoint:   printf 'last_phase=pushed\n' >> "$LEDGER"    ($LEDGER is set per ticket in §1)
# read a field: grep '^verdict=' "$LEDGER" | tail -n1 | cut -d= -f2-
```

Re-establish `UNITS_DIR` and the per-ticket `$LEDGER` / `$BR` / `$WT` / `$BASE`
in any `bash` block that needs them — they are plain values carried for the ticket.

For each `ticket` in `ticket_list`:

### 1. Ensure the ticket worktree (idempotent)

Derive `tracker_slug` + `ticket_ref` cheaply from the ticket string
(full detection happens in the plan phase). Compute `<branch>` per
`${CLAUDE_PLUGIN_ROOT}/references/branch-naming.md` — the branch is the
**ticket ref EXACTLY, no prefix**: an acronym ref (`PROJ-777`) used
verbatim; a bare number (`#678`) → `abstract-task-678`; never `ticket/`
`jira/` `feat/` `wise/` or any `/`.

Set the per-ticket variables (the worktree DIR keeps a descriptive,
collision-safe name; only the branch follows the rule), and resolve the base
branch live:

```bash
BR="<branch>"
WT="{{project.path}}.wise-ticket-<tracker_slug>-<ticket_ref>"
LEDGER="$UNITS_DIR/$BR"
BASE="$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name 2>/dev/null)"
[ -n "$BASE" ] || BASE="$(git -C "{{project.path}}" remote show origin 2>/dev/null | sed -n 's/.*HEAD branch: //p')"
[ -n "$BASE" ] || BASE="main"
```

**Ownership & dedup gate.** Before touching git, decide whether this run may
process the ticket:

```bash
if [ -f "$LEDGER" ]; then
  :   # OWN — this run already claimed this ticket (fresh start earlier, or a
      # resume). Fall through to "Ensure the worktree" + §1b and continue it.
else
  MERGED="$(gh pr list --head "$BR" --state merged --json number --jq 'length' 2>/dev/null || echo 0)"
  if [ "${MERGED:-0}" != 0 ]; then
    # Already shipped (a re-run, or a prior run that merged then cleaned up).
    printf 'ref=%s\nbranch=%s\nworktree=%s\nverdict=merged\n' "<ticket_ref>" "$BR" "$WT" >> "$LEDGER"
    # → run the §9 cleanup (no-op-safe when the worktree/branch are already gone),
    #   checkpoint `cleaned=yes`, append the row, and CONTINUE to the next ticket.
  elif [ -e "$WT" ] || git -C "{{project.path}}" show-ref --verify --quiet "refs/heads/$BR"; then
    # A worktree/branch THIS run did not create → a concurrent run or a stale
    # leftover owns it. Never stomp it.
    printf 'ref=%s\nbranch=%s\nworktree=%s\nverdict=failed\nreason=worktree-exists\n' "<ticket_ref>" "$BR" "$WT" >> "$LEDGER"
    # → append the row and CONTINUE to the next ticket.
  else
    # Fresh ticket → claim it in the ledger BEFORE any git op (closes the window
    # where an interrupt could strand an unclaimed worktree).
    printf 'ref=%s\nbranch=%s\nbase=%s\nlast_phase=claiming\n' "<ticket_ref>" "$BR" "$BASE" >> "$LEDGER"
  fi
fi
```

If the gate recorded `verdict=merged` or `verdict=failed reason=worktree-exists`,
append the ticket's row from its ledger and **continue to the next ticket** (the
merged path first runs the §9 cleanup, which is safe even if nothing remains).

**Ensure the worktree** (the OWN path and the freshly-claimed path reach here).
Bring `$WT` into being from whatever state the disk is in:

```bash
if git -C "{{project.path}}" worktree list --porcelain | grep -qxF "worktree $WT"; then
  # (b) already a registered worktree → confirm it is OUR branch, then adopt.
  WT_BR="$(git -C "$WT" rev-parse --abbrev-ref HEAD 2>/dev/null)"
  if [ "$WT_BR" != "$BR" ]; then
    printf 'verdict=failed\nreason=worktree-conflict\n' >> "$LEDGER"   # (e) different branch → bail this ticket
  fi
elif [ -e "$WT" ]; then
  # (d) dir present but NOT a registered worktree → stale admin entry / corruption.
  git -C "{{project.path}}" worktree prune
  if git -C "{{project.path}}" worktree list --porcelain | grep -qxF "worktree $WT"; then
    :   # prune re-surfaced it
  elif git -C "{{project.path}}" show-ref --verify --quiet "refs/heads/$BR" && [ -z "$(ls -A "$WT" 2>/dev/null)" ]; then
    rmdir "$WT" 2>/dev/null; git -C "{{project.path}}" worktree add "$WT" "$BR"   # empty dir → safe re-attach
  else
    printf 'verdict=failed\nreason=worktree-corrupt\n' >> "$LEDGER"
  fi
elif git -C "{{project.path}}" show-ref --verify --quiet "refs/heads/$BR"; then
  git -C "{{project.path}}" worktree add "$WT" "$BR"        # (c) branch exists, dir gone → re-attach (no -b)
else
  git -C "{{project.path}}" worktree add "$WT" -b "$BR"     # (a) nothing exists → fresh
fi
```

If the ensure step recorded `verdict=failed` (`worktree-conflict` / `-corrupt`),
append the row and **continue to the next ticket**. Otherwise checkpoint and
proceed — everything below runs against `$WT`:

```bash
printf 'worktree=%s\nbase=%s\nlast_phase=worktree\n' "$WT" "$BASE" >> "$LEDGER"
```

The plan file lives in the **run directory** (not the worktree) so it persists
with the run state and never lands in the branch (`<ref>` is `ticket_ref` with any
leading `#` stripped):

```bash
PLAN_PATH="{{run.dir}}/plans/PLAN-<ref>.md"
mkdir -p "{{run.dir}}/plans"
```

### 1b. Determine the resume point

The worktree now exists (freshly created, re-attached, or adopted). For a **fresh**
ticket every probe below is empty, so you fall straight through to §2 — no special
case. For an **adopted** ticket (resume), live probes pick which phase to enter so
finished work is never redone or stranded:

```bash
HAS_COMMITS="$(git -C "$WT" rev-list --count "origin/$BASE..HEAD" 2>/dev/null || echo 0)"
UPSTREAM="$(git -C "$WT" rev-parse --abbrev-ref '@{u}' 2>/dev/null || true)"
if [ -n "$UPSTREAM" ]; then
  AHEAD="$(git -C "$WT" rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)"
else
  AHEAD="$HAS_COMMITS"
fi
PR_STATE="$(gh pr list --head "$BR" --state all --json state  --jq '.[0].state  // ""' 2>/dev/null || true)"
PR_URL="$(  gh pr list --head "$BR" --state all --json url    --jq '.[0].url    // ""' 2>/dev/null || true)"
PR_NUMBER="$(gh pr list --head "$BR" --state all --json number --jq '.[0].number // ""' 2>/dev/null || true)"
```

Choose the entry phase (live state wins; consult the ledger only to break ties):

1. `PR_STATE = MERGED` → the work already shipped. Go to **§9** (record
   `verdict=merged`, run the idempotent cleanup). Skip §2–§8.
2. `PR_STATE = OPEN` → a live PR exists. If `AHEAD > 0`, push the stragglers first
   (`git -C "$WT" push` when `$UPSTREAM` is set, else `git -C "$WT" push -u origin "$BR"`),
   then go to **§8** (watch + fix + merge) with `pr_number=$PR_NUMBER`, `pr_url=$PR_URL`.
3. `PR_STATE = CLOSED` (closed without merging) → a human closed it deliberately.
   Append `verdict=human-intervention` + `reason=pr-closed` and **continue to the
   next ticket** — never silently re-create it.
4. No PR but the commits are already on the remote (`$UPSTREAM` set, or
   `HAS_COMMITS > 0` with the remote holding them) → push any stragglers, then go
   to **§6** (create the PR) → §7 → §8.
5. No PR, no upstream, `HAS_COMMITS > 0` → committed-but-unpushed work (the classic
   orphan). Go to **§5** (push) → §6 → §7 → §8; run §4 (review) first only if the
   ledger's `last_phase` is before `reviewed` (don't re-review reviewed commits).
6. `HAS_COMMITS = 0` → nothing built yet. Go to **§2** (plan) — but if the ledger's
   `last_phase` is `planned` and `$PLAN_PATH` already exists, skip to **§3**
   (implement).

### 2. Plan

Dispatch a `Task` subagent — `subagent_type: wise:architect`,
`model: opus`, reason at **high** effort (the plan is this run's autonomous
decision spine) — : "Read `{{workflow.dir}}/prompts/plan-ticket.md` and
follow it." with context `ticket=<ticket>`, `worktree=$WT`,
`plan_path=$PLAN_PATH`, `project.kind={{project.kind}}`, and
`config_prompt={{config_prompt}}`. It writes the
plan to `plan_path` and returns `PLAN: written=<path> type=<ticket_type>`.
On success, checkpoint `last_phase=planned`. On failure → append
`verdict=failed` + `reason=plan` and continue.

### 3. Implement

Dispatch a `Task` subagent — `subagent_type: wise:software-engineer`,
`model: sonnet`, reason at **high** effort — : "Read
`{{workflow.dir}}/prompts/implement-plan.md` and follow it." with
`plan_path=$PLAN_PATH`, `worktree=$WT`, `project.kind=<ticket_type>`, and
`config_prompt={{config_prompt}}`. (Its own parallel per-task executors
run as that fragment's fresh sub-`Task`s.) It returns
`IMPLEMENT: waves=… tasks=… done=… failed=…`. If `done=0` → append
`verdict=failed` + `reason=implement` and continue. If some tasks failed,
note it but proceed (the branch still gets reviewed in the next step).
Otherwise checkpoint `last_phase=implemented`.

### 4. Review ↔ fix loop (converge before push)

The implement phase already ran the simplify pass on each task commit. Now —
before anything is pushed — converge the branch through an **independent
reviewer and fixer**, cycling until the reviewer is satisfied.

Resolve `MAX_REVIEW_CYCLES` up front (default **10**; `config_prompt` may
override it). Set `CYCLE=0`. Loop:

1. **Review.** Dispatch a `Task` subagent — `subagent_type:
   wise:code-reviewer`, `model: opus`, reason at **high** effort — : "Read
   `{{workflow.dir}}/prompts/review-branch-auto.md` and follow it." with
   `worktree=$WT`, **`fixer=delegate`**, `findings_file=$UNITS_DIR/$BR.findings.md`,
   `ticket_ref=<ticket_ref>` (from §1), `plan_path=$PLAN_PATH` (from §1), and
   `config_prompt={{config_prompt}}`. In `fixer=delegate` it reviews
   `origin/<base>..HEAD` (five lenses + confidence-scoring), WRITES its bounded
   findings as a numbered block to `findings_file`, applies nothing, and returns
   `REVIEW-AUTO: mode=delegate verdict=<clean|issues> findings=<n> findings_file=<path>`.
   Keeping the findings in the file (not pasted into this conductor's context each
   cycle) is what bounds the loop's context growth.
   - `REVIEW-AUTO: aborted …` → append `verdict=failed` + `reason=code-review`
     and continue to the next ticket (leave the loop).
   - `verdict=clean` → the branch passes the gate. Checkpoint `last_phase=reviewed`,
     **leave the loop**, go to §5.
2. **Fix.** `verdict=issues`: dispatch a `Task` subagent — `subagent_type:
   wise:software-engineer`, `model: sonnet`, reason at **high** effort — :
   "Apply EXACTLY the review findings written in `<findings_file>` to the branch
   in `$WT`, nothing more. Make the concrete fix for each; respect the plan's
   deliberate decisions and the `config_prompt` guardrails; do not redesign or
   widen scope. Then commit by following
   `${CLAUDE_PLUGIN_ROOT}/skills/wise-commit/commit-routine.md` with
   `SIMPLIFY=no PUSH=no`." The fixer reads the findings from the file and owns the
   edits + the fix commit; the reviewer never touches the code.
3. `CYCLE=CYCLE+1`. If `CYCLE < MAX_REVIEW_CYCLES`, loop back to step 1 (the
   re-review verifies the fixes); otherwise exit the loop.

After the loop:
- **Converged** (`verdict=clean`) → proceed to §5.
- **Did NOT converge** within `MAX_REVIEW_CYCLES` → **push anyway** (continue
  to §5), but checkpoint `review=not-converged cycles=<MAX_REVIEW_CYCLES>` (and
  `last_phase=reviewed`) so §9's results table and the end-of-run report flag that
  the branch shipped with unresolved reviewer findings — left for the human + the
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

On push failure → append `verdict=failed` + `reason=push` and continue. On
success, checkpoint `last_phase=pushed`.

### 6. Create the PR

Read `{{workflow.dir}}/prompts/ensure-pr-auto.md` and follow it with
`current_branch=<branch>`, `project.path=$WT`. Capture
`PR-CREATE: number=<n> url=<url>`. On failure → append `verdict=failed` +
`reason=pr-create` and continue. On success, checkpoint `pr_number=<n>`,
`pr_url=<url>`, `last_phase=pr`.

### 7. Request review

Read `{{workflow.dir}}/prompts/request-review-auto.md` and follow it
with `pr_number=<n>`, `project.path=$WT`. It attaches Copilot **and**
triggers CodeRabbit (`@coderabbitai review`); both best-effort — a
request failure never blocks. Capture
`REVIEW-REQUEST: copilot=<…> coderabbit=<…>`. The watch step (§8) does
the real confirmation: it detects each bot, waits for the head review,
and handles CodeRabbit's out-of-credits (bypass) / rate-limit
(retry-then-give-up) states. Checkpoint `last_phase=review-requested`.

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
`blocked …` / `partial …` / `exhausted …` / `human-intervention`) — checkpoint
`verdict=<verdict>` and `last_phase=watched`.

### 9. Record, clean up merged tickets, and continue

The ticket's ledger row is already written by the per-phase checkpoints; this
step finalizes the `verdict` and `cleaned` fields. The watch step already merged
the PR if it reached fully-green; this step merges nothing itself.

**Clean up on merge.** If — and ONLY if — the ticket's `verdict` is
`merged`, the branch's work is now preserved on the remote, so the
worktree and local branch are safe to discard. Remove them so the base
repo stays clean (each step is no-op-safe when the worktree/branch are already
gone — e.g. a resume that re-entered here for an already-merged ticket):

```bash
if git -C "{{project.path}}" worktree list --porcelain | grep -qxF "worktree $WT"; then
  git -C "{{project.path}}" worktree remove "$WT" \
    || git -C "{{project.path}}" worktree remove --force "$WT"   # --force only if leftover untracked / build artifacts block removal
fi
git -C "{{project.path}}" branch -D "$BR" 2>/dev/null || true   # local branch is merged on the remote; -D since the GitHub squash/merge isn't in local history
printf 'cleaned=yes\n' >> "$LEDGER"
```

Any ticket NOT `merged` — left open for a human, or failed — **keeps its worktree
and branch** so the human can inspect or finish the work; never clean those up
(checkpoint `cleaned=no`). Move to the next ticket. One stuck ticket never aborts
the run.

## After all tickets — final sweep

Once every ticket has been processed, prune stale worktree
administrative entries so the base repo's `git worktree list` reflects
reality (the merged tickets' worktrees were already removed in §9):

```bash
git -C "{{project.path}}" worktree prune
```

Only the worktrees + branches for tickets left **open / failed** remain
— intentionally, for a human. If every ticket merged, the base repo is
now fully clean.

## Final output

The per-ticket ledgers under `{{run.dir}}/units/` are the source of truth for the
roll-up — they survive a compaction that would have eaten an in-chat table. Read
every ledger file in `{{run.dir}}/units/` (skip the `*.findings.md` files) and
print the per-ticket results table from their fields (`ref`, `branch`, `worktree`,
`pr_url`, `verdict`, `review`, `cleaned`; for each key take the last line) for the
`report` step to surface. Then compute the tally from the same files and emit — as
the FINAL line, alone, no markdown, no backticks:

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
- **Idempotent on resume.** §1 *ensures* (creates, re-attaches, or adopts) the
  worktree — it never collides-and-fails on an existing one. Live `git`/`gh` state
  is the source of truth; the ledger under `{{run.dir}}/units/` is a hint. A
  worktree/branch this run did not claim is **skipped, never adopted**.
- A **merged** ticket's worktree and local branch are removed in §9
  (its work is safe on the remote); worktrees + branches for tickets
  left **open / failed** are kept for inspection, and the `report` step
  lists the `git worktree remove` command for each one that remains.
- A failure on one ticket is recorded and the run continues with the
  next ticket.
- All work runs inside this Claude Code session. Parallelism uses
  Claude Code's native `Task` and `TeamCreate` only — never shell out
  to `claude -p`, spawn a separate Claude Code process, or invoke any
  external agent / LLM CLI.
