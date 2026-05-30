# plan-ticket — turn one ticket into an implementation plan, autonomously

The autonomous planning procedure for a **single** ticket. It is the
plan phase of `ticket-auto`'s per-ticket pipeline — the same logic the
interactive `ticket-plan` workflow runs, but decision-free: every
choice it would ask a human for is made by the **Lead Architect**.

Source of truth for `ticket-auto`'s plan phase. The `ticket-auto`
orchestrator runs this against each ticket's worktree.

## Context the caller supplies

- `ticket` — a ticket URL or bare id.
- `worktree` — absolute path to this ticket's git worktree (the codebase
  audit reads here).
- `plan_path` — absolute path to write the plan to. The orchestrator
  points this at the **run directory** (off the project tree), so the
  plan persists with the run state and never lands in the branch.
- `project.kind` — `frontend | backend | fullstack | other`, if known.

## Procedure

### 1. Identify the tracker

Parse `ticket`. If it is a URL, match the host (`*.atlassian.net` →
Jira, `linear.app` → Linear, `github.com/*/issues` → GitHub,
`gitlab.com` → GitLab, `app.asana.com` → Asana, …); WebSearch an
unknown host to classify it. If it is a bare id whose tracker is
ambiguous, the Lead Architect picks the most likely tracker and
records the assumption — this run never asks the user. Derive a
lowercase `tracker_slug` and the bare `ticket_ref`.

### 2. Confirm access (autonomous, graceful)

Probe for a tracker integration: a tracker MCP (a one-time permission
prompt is expected), or a CLI (`command -v gh` / `glab`). If none,
`WebFetch` a public ticket URL. If nothing works, record
`access=degraded` and plan from the ref + the input string alone —
do NOT block and do NOT invent ticket content.

### 3. Fetch and classify the ticket

Fetch via the established access and normalise — omit fields the
tracker lacks rather than faking them:

```
## Ticket <tracker_slug>:<ticket_ref> — <Title>
- Status / Priority / Assignee / Labels / Iteration / Parent (if present)
### Description
### Acceptance Criteria   (or "none stated")
### Related items   — linked tickets / sub-issues / blockers + relation
### Design links    — Figma / Sketch / image-attachment URLs
### Reference docs  — Confluence / Notion / wiki / doc URLs
```

Classify the ticket type: combine `project.kind` (or infer from the
worktree's manifest — `go.mod`/`pom.xml` → backend, `package.json`
with React/Vue/Svelte → frontend, both → fullstack) with the ticket's
vocabulary. Record `ticket_type`.

### 4. Parallel research wave

Dispatch three `Task` (`subagent_type: "Explore"`) subagents
concurrently, each against `<worktree>`:

- **Design** — for each design link, summarise layout / typography /
  color / states / responsive per component; reply `NO-DESIGN` for a
  backend ticket or when there are none.
- **Related** — fetch each related item + reference doc; one section
  with a one-paragraph summary each; reply `NO-RELATED` if empty.
- **Codebase audit** — reuse-first audit routed by `ticket_type`:
  discover the worktree's layout, then audit UI components/hooks/
  styling (frontend) or API handlers/models/services (backend) or
  both (fullstack); for each asset record path, line, and
  reuse-as-is vs. needs-extension.

### 5. Consolidate

The Lead Architect folds the three outputs into one investigation
summary: requirements, design notes, related resources, codebase
context / reusable assets.

### 6. Decide

The Lead Architect makes — autonomously, with rationale — every
scope / technical-approach / component / design / testing decision
the interactive wizard would ask a human for. Output a `## Decisions`
section: each decision + a one-line rationale.

### 7. Build the plan

Compose the implementation plan and **write it to the supplied
`plan_path`** with the `Write` tool (`mkdir -p` its parent dir first if
needed). Structure:

```
# <tracker_slug>:<ticket_ref> — <Title>
## Summary
## Assumptions   (every autonomous decision; any access gap)
## Decisions Made
## Design Notes  (frontend / fullstack tickets with designs only)
## Tasks         (parallelizable WAVES; each task "Reuse: …" / "New: …";
                  per-task SP and total SP)
## Testing
## Validation    (type-check / lint / tests checklist)
```

The `## Tasks` section MUST be authored as ordered **waves** of
independent tasks — the implement phase dispatches one executor per
task and runs waves strictly in sequence. For SP estimation consult
the `wise-estimation` reference skill (Fibonacci, ≤ 3 SP per
sub-task, round the total to the nearest Fibonacci).

### 8. Final line

FINAL line — alone, no markdown, no backticks — MUST match:

```
PLAN: written=<plan_path> type=<ticket_type>
```
