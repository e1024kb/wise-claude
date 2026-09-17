# branch-naming — the ticket-scoped feature branch rule

Single source of truth for naming the git branch a piece of ticket work lives
on. Read by every workflow / skill that creates, switches to, or suggests a
ticket-scoped branch - `ticket-plan`, the engine's unit pipelines behind
`ticket-auto` / `impl-plan-auto` (`engine/src/phases/common.ts`,
`ticketBranch` / `planBranch`), and the PR helpers' protected-branch
suggestions.

## The rule

A ticket-scoped feature branch name is the **ticket ref, and nothing else** —
no prefix, no folder, no slug, no decoration. Given the resolved `ticket_ref`,
the branch name (`target_branch`) is:

| `ticket_ref` shape | example | `target_branch` |
|---|---|---|
| has a project acronym — `^[A-Za-z][A-Za-z0-9]*-\d+$` | `PROJ-777`, `ENG-45` | **verbatim** — `PROJ-777` (case preserved; keys are uppercase) |
| bare number — `^#?\d+$` | `#678`, `678` | `abstract-task-<digits>` — `abstract-task-678` |

Normalize first: strip a leading `#`, trim surrounding whitespace. The result
must be a legal git ref (no spaces, no `~^:?*[`, no `..`, no trailing `/` or
`.lock`); if a ref somehow isn't, sanitize the disallowed characters to `-`
rather than adding a prefix.

**Forbidden, always:**
- No prefixes — not `ticket/`, `jira/`, `linear/`, `feat/`, `bugfix/`,
  `feature/`, `wise/`, nor any tracker slug.
- No `/` at all (a ticket branch is a flat name, never a folder).
- No `-<slug>` / `-<summary>` suffix.
- Never lowercase a project-acronym key.

So `ticket/jira-proj-777` and `feat/PROJ-777-add-panel` are both wrong; the
branch is exactly `PROJ-777`.

## The one exception — the conductor's per-run worktree branch

`wise-workflow-run`'s worktree option creates a branch named
`wise/<workflow-name>-<run-ulid>` (e.g. `wise/ticket-plan-01HF…`). That branch
is **run-scoped, not ticket-scoped** — it's keyed on the run ULID and exists to
isolate a workflow run's edits, not to track a ticket. It is intentionally
exempt from this rule and keeps its `wise/` prefix.

## The other exception — a taken branch gets a numeric suffix

A fresh run never reuses a branch it does not own. When `target_branch`
already exists locally, on `origin`, or in a registered worktree, the run
takes the first free `<target_branch>-N` with `N` counting from 2
(`PROJ-777-2`, `PROJ-777-3`, ...) and says so. A run that resumes keeps the
branch it created, suffixed or not. The engine's unit pipelines do this in
`claim` (`engine/wise_engine/phases/claim.py`, `free_branch`); ticket-plan's
`setup` step follows the same rule in prose. The existing branch is never
deleted, reset or overwritten.

## For implementers

Compute `target_branch` from `ticket_ref` per the table above, then:

```bash
# on a clean tree: resume your own branch (suffixed or not), else create
# the first free name
case "$(git rev-parse --abbrev-ref HEAD)" in
  "$target_branch" | "$target_branch"-[0-9]*)
    : ;;
  *)
    candidate="$target_branch"; n=1
    while git show-ref --verify --quiet "refs/heads/$candidate" \
       || git ls-remote --exit-code --heads origin "$candidate" >/dev/null 2>&1; do
      n=$((n + 1)); candidate="$target_branch-$n"
    done
    git checkout -b "$candidate" ;;
esac
```

The worktree *directory* name (e.g. `<project>.wise-ticket-<ref>`) is a path, not
a branch, and is out of scope for this rule — only the branch `-b` value must
follow it.
