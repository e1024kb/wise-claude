# branch-naming — the ticket-scoped feature branch rule

Single source of truth for naming the git branch a piece of ticket work lives
on. Read by every workflow / skill that creates, switches to, or suggests a
ticket-scoped branch — `ticket-plan`, `ticket-auto` (`process-tickets.md`), and
the PR helpers' protected-branch suggestions.

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
`wise/<workflow-name>-<run-ulid>` (e.g. `wise/pr-interactive-01HF…`). That branch
is **run-scoped, not ticket-scoped** — it's keyed on the run ULID and exists to
isolate a workflow run's edits, not to track a ticket. It is intentionally
exempt from this rule and keeps its `wise/` prefix.

## For implementers

Compute `target_branch` from `ticket_ref` per the table above, then:

```bash
# create or switch, idempotently, on a clean tree
if git rev-parse --verify --quiet "$target_branch" >/dev/null; then
  git checkout "$target_branch"
else
  git checkout -b "$target_branch"
fi
```

The worktree *directory* name (e.g. `<project>.wise-ticket-<ref>`) is a path, not
a branch, and is out of scope for this rule — only the branch `-b` value must
follow it.
