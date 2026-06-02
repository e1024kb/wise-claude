# review-branch-auto — autonomous high-depth code-review branch gate

The branch-review gate of `ticket-auto`'s per-ticket pipeline. Once a
ticket's branch is fully implemented and every task is committed — but
**before the branch is pushed / a PR is opened** — this reviews the
whole branch diff at **high** effort, applies the concrete findings,
and commits them. It is the heavyweight tier of the plugin's two-tier
quality model; the lightweight simplify tier already ran on each
individual commit.

Source of truth for the `/wise-code-review-auto` skill and the
`ticket-auto` workflow's review step. It is decision-free — it NEVER
calls `AskUserQuestion`.

## Context the caller supplies

- `worktree` — absolute path to the git working tree to review (a ticket
  worktree, when called from `ticket-auto`; the repo toplevel for the
  standalone skill).
- `base` — **optional** base branch to diff against. When absent, detect
  the repo's default branch (below).
- `ticket_ref`, `plan_path` — **optional** context; when supplied, weigh
  findings against the ticket's intent and the plan's `## Decisions Made`
  rather than second-guessing deliberate choices.
- `config_prompt` — **optional** operator standing guidance (may be
  empty). When supplied, weigh findings against its guardrails too:
  flag anything that violates a stated guardrail, and do not "fix"
  something the guidance deliberately chose.

## Procedure

Run all `git` / `gh` commands with `cd <worktree>` first.

### 1. Resolve the base and the diff range

```bash
BASE="${base:-$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name 2>/dev/null)}"
BASE="${BASE:-$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's@^origin/@@')}"
BASE="${BASE:-main}"
RANGE="origin/$BASE..HEAD"
```

The change set under review is `RANGE` — exactly the commits about to be
pushed. If `git rev-list --count "$RANGE"` is `0` there is nothing to
review: emit `REVIEW-AUTO: applied=0 skipped=0 committed=no` and stop.

### 2. Review + apply (high-effort panel)

Follow `${CLAUDE_PLUGIN_ROOT}/references/code-review-pass.md` end to end
at **high** effort over `RANGE`: dispatch the parallel reviewer panel
(read-only `Task` subagents — five lenses + the confidence-scoring
pass), curate the high-confidence
findings, and apply the **bounded** set via `Edit` / `Write` (concrete
correctness / security / clear-quality; skip judgement-call refactors,
behaviour changes, broad renames, new deps). One round — never re-run
the panel.

If `ticket_ref` / `plan_path` were supplied, do not "fix" something the
plan deliberately decided; note it as skipped with that rationale. If
`config_prompt` was supplied, the same applies to its guidance — respect
its guardrails and deliberate choices, and surface any finding that
contradicts a stated guardrail.

On a hard failure (the panel errors, or the fixes break the tree),
follow that reference's failure policy and emit
`REVIEW-AUTO: aborted reason="<one-line>"`.

### 3. Commit the fixes

If the apply step changed any files, commit them by following
`${CLAUDE_PLUGIN_ROOT}/skills/wise-commit/commit-routine.md` with
`SIMPLIFY=no PUSH=no` (the review already curated these edits — no extra
simplify pass; the caller owns the push). Capture the routine's
`COMMIT:` line: a `COMMIT: ok …` means `committed=yes`, a
`COMMIT: skip …` means `committed=no`.

If nothing was applied, there is nothing to commit (`committed=no`).

### 4. Final line

Emit, as the FINAL line — alone, no markdown, no backticks:

```
REVIEW-AUTO: applied=<n> skipped=<m> committed=<yes|no>
```

`applied` counts findings turned into edits, `skipped` counts findings
left alone, `committed` is whether a fix commit landed.

## Guardrails

- Fully autonomous — never call `AskUserQuestion`.
- Never `git push` — the caller's push step owns that.
- One review pass, one fix-apply, one commit; never iterate-to-clean.
- Bounded apply — guard the change, do not redesign it; respect the
  plan's deliberate decisions when `plan_path` is supplied.
- Never append an AI-attribution trailer to the fix commit.
- All work runs in this Claude Code session with native tools. Never
  shell out to `claude -p` or any external agent / LLM CLI.
