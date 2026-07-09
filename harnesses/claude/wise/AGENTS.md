<!-- Keep this catalog in sync with agents/*.md. One row per file; the
     "When auto picks it" cell mirrors each role's frontmatter `description`
     for human reference. The conductor's `agent: auto` routing matches the
     step intent against the frontmatter `description` (read via
     `workflows.py list-agents`), NOT this table. -->

# wise agent roster

`wise` ships a roster of **SDLC role subagents** under
[`agents/`](agents/). Each file is a real Claude Code plugin subagent —
once the plugin is installed they appear in `/agents` and are invocable
as `subagent_type: wise:<name>` (e.g. `wise:architect`). They give the
workflow engine a library of reusable expert personas to dispatch
`prompt` steps to, instead of the generic `general-purpose` worker.

These plugin-level roster agents are distinct from the **skill-local**
agents that `wise-prd-architect`, `wise-trd-architect`, and
`wise-implement-plan-auto` keep inside their own skill directories —
those are task-specific personas spawned by one skill; the roster here is
shared across every workflow.

## The roster

| Agent (`wise:<id>`) | Role | Default effort | When `auto` picks it |
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

`model` is `inherit` for every roster agent — they follow the session
model (or a workflow step's `model:` override). `effort` is the agent's
default reasoning level, set to match the role's cognitive load; a
workflow step's `effort:` can nudge it per invocation.

## Using the roster in workflows

A workflow `prompt` step binds to the roster via a scalar (one role) or a
list (a team), see
[`docs/wise/workflows.md`](../../../docs/wise/workflows.md) for the full schema:

```yaml
# workflow-level default — opt every prompt step into auto-selection
agents: auto

steps:
  - id: design
    type: prompt
    agent: architect          # force a specific role
    effort: high
    prompt: |
      Design the …

  - id: research
    type: prompt
    agent: auto               # let the conductor pick the best-fit role
    prompt: |
      Investigate …

  - id: raw-step
    type: prompt
    agent: off                # plain general-purpose subagent (no persona)
    prompt: |
      …

  - id: review                # a TEAM — several roles at once, then synthesized
    type: prompt
    model: sonnet             # shared default for members that don't override
    agent:
      - role: architect
        lead: true            # integrates the panel before final synthesis
        model: opus           # per-member override
      - role: security-engineer
        effort: high
      - qa-engineer           # bare string → inherits step model/effort
    prompt: |
      Review the change for …
```

- **`agent: <role>`** → dispatched as `subagent_type: wise:<role>`.
- **`agent: auto`** → the conductor reads this roster (via
  `scripts/workflows.py list-agents`) and routes the step to the role
  whose description best matches the step's intent, falling back to
  `general-purpose` when nothing fits.
- **`agent: off`** (or omitted, when the workflow's `agents:` policy is
  `off`) → the generic `general-purpose` subagent, exactly as before.
- **`agent: [ … ]`** (a list) → a **team**: every member runs as a parallel
  `wise:<role>` subagent, an optional single `lead` integrates the peers'
  drafts, and the conductor **synthesizes** one step result. `auto`/`off` are
  scalar-only — not valid team members. Per-member `model`/`effort` override
  the step-level ones; a bare-string member inherits them.

`agent:`, `model:`, and `effort:` apply to **`prompt` steps only** —
`interactive` steps run inline in the conductor (its own model), and
`skill` steps run under the invoked skill's own frontmatter. Steps run
**in-conversation** (`Task` subagents, subscription-covered — no
subprocess backend): `model:` is a real per-call override (the primary
knob), and `effort:` is conveyed as a prompt directive (best-effort, may
be ignored today). A pinned model that has retired auto-falls-back to its
alias with a notice. See
[`docs/wise/workflows.md`](../../../docs/wise/workflows.md#agents-model-and-effort).

## Adding or editing a role

1. Edit the **canonical neutral card** at `core/agents/<role>.md` first
   (name + description + persona prose only). This port's
   `agents/<role>.md` is **generated** from it — the Claude frontmatter
   (`tools` / `model: inherit` / `effort` / `color`) comes from
   `core/ports/profiles/claude.yaml`. Plugin subagents **must not**
   declare `hooks`, `mcpServers`, or `permissionMode`. The other ports
   take the neutral card verbatim (see the root `CONTRIBUTING.md` §10).
2. Run `python3 scripts/build_ports.py` from the repo root to regenerate
   every port's copy — never hand-edit `agents/` files in any port.
3. Add or update the row in the table above — the "When `auto` picks it"
   cell is the routing hint the conductor reads.
4. Run `python3 scripts/workflows.py list-agents` to confirm it parses,
   and `python3 scripts/build_ports.py --check` to confirm the tree
   matches a fresh render.
