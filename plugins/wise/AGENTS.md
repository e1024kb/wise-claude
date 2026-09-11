<!-- Keep this catalog in sync with agents/*.md. One row per file; the
     "When to pick it" cell mirrors each role's frontmatter `description`
     for human reference. Claude picks a subagent by that frontmatter
     `description`, NOT by this table. -->

# wise agent roster

`wise` ships a roster of **SDLC role subagents** under
[`agents/`](agents/). Each file is a real Claude Code plugin subagent —
once the plugin is installed they appear in `/agents` and are invocable
as `subagent_type: wise:<name>` (e.g. `wise:architect`). They give a
Claude session, and every Claude child the workflow engine spawns, a
library of reusable expert personas to delegate to instead of the
generic `general-purpose` worker.

These plugin-level roster agents are distinct from the **skill-local**
agents that `wise-prd-architect`, `wise-trd-architect`, and
`wise-implement-plan-auto` keep inside their own skill directories —
those are task-specific personas spawned by one skill; the roster here is
shared across every workflow.

## The roster

| Agent (`wise:<id>`) | Role | Default effort | When to pick it |
|---|---|---|---|
| `wise:ceo` | Chief Executive — vision, prioritization, go/no-go | high | Business framing, cross-initiative prioritization, a go/no-go decision. |
| `wise:cto` | Chief Technology Officer — tech strategy, arbiter | high | Build-vs-buy, tech-stack direction, technical-risk calls, cross-team architecture disputes. |
| `wise:product-manager` | Product — requirements, user stories, scope | medium | Turning a problem into requirements, acceptance criteria, scope/MVP, prioritization. |
| `wise:engineering-manager` | Eng Manager — breakdown, sequencing, estimates | medium | Decomposing a plan into tasks/waves, estimating, surfacing risks & dependencies. |
| `wise:architect` | Architect — system & component design, ADRs | high | Designing a system/component, choosing patterns, weighing trade-offs, writing a design doc. |
| `wise:software-engineer` | Software Engineer — implement, fix, test | medium | Implementing a task, fixing a bug, refactoring, writing tests against existing code. |
| `wise:qa-engineer` | QA — test strategy, plans, automation | medium | Test strategy, edge-case enumeration, writing/running tests, precise bug reports. |
| `wise:security-engineer` | Security — threat model, audit, fixes | high | Threat-modelling a change, auditing for vulnerabilities, auth/crypto/secrets review. |
| `wise:devops-engineer` | DevOps — CI/CD, IaC, deploys | medium | Authoring/fixing pipelines, IaC, containers, deployment & rollback strategy. |
| `wise:sre` | SRE — reliability, SLOs, observability | high | SLI/SLO definition, monitoring/alerting, incident runbooks, capacity, failure modes. |
| `wise:ux-designer` | UX/UI — flows, usability, accessibility | medium | User flows, interaction design, usability & accessibility critique of UI. |
| `wise:technical-writer` | Tech Writer — docs, guides, references | low | READMEs, API docs, how-to guides, changelogs, doc comments. |
| `wise:code-reviewer` | Code Reviewer — diff/branch review | high | Reviewing a diff or branch for correctness/security/quality before it ships. |

`model` is `inherit` for every roster agent - they follow the model of
the session or harness child that spawns them. `effort` is the agent's
default reasoning level, set to match the role's cognitive load.

## Using the roster in workflows

The YAML v2 engine has no `agent:` step field: every `agent` step is one
headless harness child (`claude -p`, `codex exec`, ...) that runs the
step's `prompt`. A Claude child can delegate to a roster role with its
own `Task` / `Agent` tool, so a workflow uses the roster through the
prompt:

```yaml
steps:
  - id: design
    type: agent
    group: authoring
    prompt: |
      Act as the wise `architect` agent (see
      ${CLAUDE_PLUGIN_ROOT}/agents/architect.md). Design the ...
      Return the fields directly: design_path.
    schema:
      type: object
      properties: { design_path: { type: string } }
      required: [design_path]
      additionalProperties: false
    outputs: [design_path]
    allowed_tools: [Task, Agent]

  - id: review
    type: agent
    group: review
    prompt: |
      Run three reviewers in parallel with the Task tool, one each as
      wise:code-reviewer, wise:security-engineer and wise:qa-engineer,
      then merge their findings into one verdict: ship | block.
    allowed_tools: [Task, Agent]
```

The Python v2 executor owns these children through `engine/wise_engine/`;
`engine/engine.sh` is its managed Python entrypoint.

- The step's `group` (or `harness` / `model` / `effort`) decides which
  harness and model run the child; the role card decides the persona
  the child adopts or delegates to. `wise-engine migrate` rewrites a v1
  `agent: <role>` into the "Act as the wise `<role>` agent" prefix and
  folds a v1 team into its lead plus lenses with a MANUAL note.
- Subagents a Claude child spawns need the `Task` / `Agent` permission
  rules in `allowed_tools`; the engine pre-grants them for the `units`
  step's `implement` and `review` phases.
- Non-Claude harnesses (`codex`, `cursor`, `gemini`, `grok`) have no plugin
  subagents; the prompt text is the only persona they see.

See [`docs/wise/workflows.md`](../../docs/wise/workflows.md) for the
step schema.

## Adding or editing a role

1. Edit the role card at `agents/<role>.md` directly. Frontmatter is
   limited to `name` / `description` / `tools` / `model` / `effort` /
   `color` — plugin subagents **must not** declare `hooks`,
   `mcpServers`, or `permissionMode`. Keep `model: inherit`.
2. Add or update the row in the table above — the "When `auto` picks it"
   cell is the routing hint the conductor reads.
3. Run `just check` before committing.
