# code-review-pass — the canonical multi-agent branch review

Single source of truth for **how** the plugin runs its heavyweight
branch gate. Read by:

- `workflows/ticket-auto/prompts/review-branch-auto.md` — the autonomous
  pre-push gate in the `ticket-auto` pipeline.
- `skills/wise-code-review-auto/SKILL.md` — the standalone building block.

This is the **heavy tier** of the two-tier quality model: it runs
**once** over a whole branch, after every change is committed but
**before that branch reaches GitHub** (push / PR open). The lightweight
per-commit tier is [`simplify-pass.md`](./simplify-pass.md).

## The mechanism — a wise-native panel of reviewer subagents

The review is performed by a **panel of parallel `Task` reviewer
subagents** that wise dispatches itself, then the caller applies the
high-confidence findings and commits them. This mirrors how Anthropic's
own `/code-review` works internally (independent reviewer agents over
the diff), but stays fully autonomous and self-contained.

> Why not `Skill({ skill: "code-review", args: "medium" })`: in this
> marketplace that resolves to **CodeRabbit's** skill (needs the
> `coderabbit` CLI + auth, sends diffs to a third-party API, and ignores
> the effort argument), not an Anthropic effort-graded reviewer. And the
> effort-graded `/code-review` is a slash command, which an autonomous
> workflow cannot type. So wise runs its own reviewer panel via `Task`.
> CodeRabbit / Copilot still review the PR later, in the watch loop — this
> gate is the *pre-push* catch.

### Effort → panel depth

`effort` maps to how many lenses the panel covers:

| effort | reviewer subagents (lenses) |
|---|---|
| `low` | 1 — correctness/bugs only |
| **`medium`** (default) | **3 — (a) correctness & logic bugs, (b) security & input handling, (c) conventions, dead code, and `CLAUDE.md` adherence** |
| `high` | 5 — the medium three plus (d) git-history/context regressions and (e) test-coverage gaps, with a confidence-scoring pass |

The plugin standardises on **`medium`** for this gate.

## The pass (review → curate → apply → commit)

1. **Resolve the diff.** The change set is `origin/<base>..HEAD` — exactly
   the commits about to be pushed (the caller supplies `base` / detects
   the default branch).

2. **Dispatch the panel.** In a single message, dispatch the effort's
   reviewer `Task` subagents **in parallel**, each **read-only**
   (`subagent_type: "Explore"` is a good fit). Give each its lens, the
   diff range, and the worktree. Each returns a list of findings —
   `file:line`, a one-line description, and a severity
   (critical / warning / info). Reviewers **do not edit**; they report.

3. **Curate.** Collect every finding, dedupe by `file:line`, and keep
   only the **high-confidence, concrete** ones: correctness bugs,
   security issues, and clear code-quality problems (dead code, unused
   imports, obviously redundant logic). **Drop** "consider X" judgement
   calls the work didn't ask for — behaviour changes, broad renames, new
   dependencies, large refactors — and any finding on lines the branch
   didn't touch. If `plan_path` is supplied, drop anything the plan's
   `## Decisions Made` deliberately chose; note it as skipped.

4. **Apply + commit.** Apply the kept findings via `Edit` / `Write`, then
   commit them (the caller owns the commit step and the final verdict).
   One round — do **not** re-run the panel to iterate-to-clean.

## On failure

If dispatching the panel errors, or applying fixes leaves the tree in a
state `git status` (or a syntax check) reports as broken, treat it as a
**hard failure**: do **not** retry or invent a recovery. Surface
`code-review errored: <summary>` and let the **caller** map it to its
abort contract. The pass never re-validates inside itself — the
project's pre-commit hook / CI is the final guard.
