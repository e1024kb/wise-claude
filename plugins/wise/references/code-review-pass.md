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

### Panel shape → profile sets the EFFORT, never the lens count

The panel is **always the 3-lens set** — (a) correctness & logic bugs,
(b) security & input handling, (c) test-coverage gaps: the lenses that
catch shipping-blockers. The caller passes a `profile` level (the
session token-budget profile from `references/profile-read.md`, or a
pinned value); it maps to the reasoning-effort directive each reviewer
subagent gets. The reviewer **model is never downgraded** — effort is
the budget knob:

| profile | lenses | per-reviewer effort directive | extra |
|---|---|---|---|
| `low` | 3 | `medium` | — |
| **`medium`** (default) | 3 | `high` | — |
| `max` | 3 | `high` | **verification pass** (below) |

The effort directive is appended to each reviewer's prompt the same
way workflow dispatch conveys effort (a prompt directive, best-effort).
`max` deliberately does NOT reach for a higher effort word — `xhigh`
clamps to `high` under Opus 5's policy ceiling, so it would be a
no-op; instead `max` buys a **verification pass**, a real extra
dispatch (step 3b below): after curation, each kept finding gets one
adversarial re-check by a fresh read-only subagent prompted to REFUTE
it against the actual code; findings the checker refutes are dropped
(noted as skipped). What `max` buys is fewer false positives at the
gate, not more findings.

There is a second shape for one caller only — **`panel=universal`**,
used by the PR watcher's review fallback (`review-fallback-auto.md`):
ONE reviewer subagent covering all three focus areas in a single
read-only pass, at `medium` effort, profile-independent. The watcher's
fallback substitutes for a bot review of a branch that already passed
the pre-push gate, so one universal reviewer is the right weight
there; the lens panel stays the shape for the pre-push gate itself.
(Pre-4.15 this table was keyed by `effort` with a 5-lens default;
4.15.0 briefly scaled lens count by profile; effort-scaling replaced
that deliberately.)

## The pass (review → curate → apply → commit)

1. **Resolve the diff.** The change set is `origin/<base>..HEAD` — exactly
   the commits about to be pushed (the caller supplies `base` / detects
   the default branch).

2. **Dispatch the panel.** In a single message, dispatch the three
   reviewer `Task` subagents **in parallel** (or the one universal
   reviewer under `panel=universal`), each **read-only**
   (`subagent_type: "Explore"` is a good fit). Give each its lens, the
   diff range, and the worktree. Each returns a list of findings —
   `file:line`, a one-line description, and a severity
   (critical / warning / info). Reviewers **report only — they never
   mutate the working tree.** No `Edit` / `Write`, and **no file-mutating
   Bash command** either: never run a formatter or linter in write mode
   (`gofumpt -w`, `go fmt`, `prettier --write`, `eslint --fix`,
   `ruff --fix`, …), and never `git add` / `commit` / `checkout` /
   `reset` or any codegen. To inspect formatting, use a read-only mode
   instead (`gofumpt -l` / `-d`, `--check`, `--dry-run`, `git diff`).
   Applying any fix is the **caller's** job — in `ticket-auto` a separate
   `wise:software-engineer` fixer, looping with re-review until clean
   (the review↔fix loop); in `fixer=self` the curate-then-apply step (§4)
   — never the reviewer.

3. **Curate.** Collect every finding, dedupe by `file:line`, and keep
   only the **high-confidence, concrete** ones: correctness bugs,
   security issues, and clear code-quality problems (dead code, unused
   imports, obviously redundant logic). **Drop** "consider X" judgement
   calls the work didn't ask for — behaviour changes, broad renames, new
   dependencies, large refactors — and any finding on lines the branch
   didn't touch. If `plan_path` is supplied, drop anything the plan's
   `## Decisions Made` deliberately chose; note it as skipped.

3b. **Verify (`max` profile only).** Dispatch one fresh read-only
   subagent per kept finding — in a single parallel message — each
   prompted to REFUTE its finding against the current code ("is this
   actually wrong as claimed? Default to refuted when the evidence is
   ambiguous."). A finding the checker refutes is dropped and counted
   as skipped with a one-line reason. Skip this step entirely at
   `low` / `medium`.

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
