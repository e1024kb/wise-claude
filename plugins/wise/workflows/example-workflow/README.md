# example-workflow

<!-- This README is the source of truth for how the workflow
     LOOKS to users. Keep it in sync with workflow.yaml +
     prompts/*.md — every edit to the flow, steps, outputs,
     or fragment list belongs here too. See
     plugins/wise/CLAUDE.md for the invariant. -->

Reference workflow that exercises four of the six step types
(`skill`, `prompt`, `bash`, `approval` — `ask` and `interactive`
are not exercised here) AND the parallel-wave dispatcher.
Harmless to run — it only lists the available workflows, classifies
the current project, and runs six parallel fan-out steps that either
subagent-prompt or `echo` under a randomised sleep. Use it to
verify the workflow subsystem after an install, a schema change,
or a dep upgrade.

## When to use

- After installing / updating the wise plugin, to confirm the
  engine, `workflows.py` helpers, and step-type dispatch all
  work end-to-end.
- While developing the workflow subsystem — change an engine
  helper, run this, see if anything regressed.
- As a reference when authoring a new workflow — the YAML
  exercises parallel waves (same `depends_on`), `outputs` capture,
  `until:` regex, and an approval gate.

## When not to use

- It's not a "real" workflow — it doesn't do anything useful
  beyond proving the subsystem is wired correctly.

## Prerequisites

- `/wise-init` completed at least once so the workflow engine's
  fast-path finds Python + PyYAML.
- Run from inside a git repository — `project-selection: prompt`
  auto-detects the project from the current directory and asks
  you to confirm it.

## Flow

```mermaid
flowchart TD
    A[list-workflows<br/>skill wise:wise-workflow-list] --> B[classify<br/>prompt → release_kind]
    B --> C[summarize-project<br/>prompt]
    B --> D[suggest-next-step<br/>prompt]
    B --> E[pick-emoji<br/>prompt → project_emoji]
    B --> F[echo-greeting<br/>bash]
    B --> G[echo-timestamp<br/>bash]
    B --> H[echo-pwd<br/>bash]
    C --> I
    D --> I
    E --> I
    F --> I
    G --> I
    H --> I[approve-summary<br/>approval]
```

The six steps between `classify` and `approve-summary` share
`depends_on: [classify]`, so they run as one parallel wave. In
wave-sync mode the conductor batches them into a single turn;
in synchronous mode the approval is auto-approved.

## Steps

| Step | Type | Purpose |
|---|---|---|
| `list-workflows` | `skill` | Invokes `wise:wise-workflow-list` so Claude sees the available workflows. |
| `classify` | `prompt` | Asks the subagent to reply with one word — `frontend` / `backend` / `fullstack` / `other` — validated against an `until:` regex with 2 retries. Captures `release_kind`. |
| `summarize-project` | `prompt` | One-sentence summary of a `{{project.kind}}` project. |
| `suggest-next-step` | `prompt` | One-sentence improvement suggestion. |
| `pick-emoji` | `prompt` | Single emoji matching the project kind. Captures `project_emoji`. |
| `echo-greeting` | `bash` | `sleep RANDOM; echo "hello from {{project.name}}"`. |
| `echo-timestamp` | `bash` | `sleep RANDOM; date -u`. |
| `echo-pwd` | `bash` | `sleep RANDOM; pwd`. |
| `approve-summary` | `approval` | Final gate. In wave-sync mode an AskUserQuestion confirms the run; in sync it auto-approves. |

## Inputs

None.

## Outputs

| Name | Source | Used for |
|---|---|---|
| `release_kind` | `classify` | Templated into downstream step prompts. |
| `project_emoji` | `pick-emoji` | Included in the `approve-summary` message. |

## Examples

```
/wise-workflow-run example-workflow
```

## Related

- [Definition YAML](./workflow.yaml)
- [`docs/wise/workflows.md`](../../../../docs/wise/workflows.md) —
  user-facing workflow reference.
