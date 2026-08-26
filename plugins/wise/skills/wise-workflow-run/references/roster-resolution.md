# wise-workflow-run/references/roster-resolution — tuning override, model, effort

Companion to the conductor SKILL.md §10d. Read it the FIRST time a
wave dispatches a `type: prompt` step (any run that reaches dispatch);
it defines how a recorded tuning override binds at dispatch, how each
member's model is passed and falls back, and how effort is conveyed.
Schema/semantics live in `docs/wise/workflows.md § Agents, model and
effort`.

**Tuning override (only when the run recorded one).** If the workflow
declares a `tuning:` block and the run's state carries a
`tuning_<group.id>` output that is not `default` (recorded by §6b2 —
on resume, re-shell `get-tuning "$DEF"` once and read the outputs
from state rather than trusting conversation memory), and this step's
id is in that group's `steps:` list, append the user's choice to the
call:

```bash
python3 .../workflows.py resolve-team "$DEF" "<step.id>" --model <m> --effort <e>
```

The override wins over the step's AND every team member's pinned
model/effort (the engine notes `run tuning override` in each member's
`reason` — surface it on the 10e line like any other reason). Groups
with no `steps:` binding (advisory groups) are not applied here —
their choice reaches the workflow through the `{{tuning_<id>}}` /
`{{tuning_summary}}` template outputs instead. For a step that
resolves through `resolve-model` (mode `off` / `auto` / policy
fallback), apply the same override by passing `<m>` / `<e>` as the
`resolve-model` arguments in place of the step's own pins.
→ JSON `{mode, lead, members:[{role, lead, model, effort, reason, fell_back,
next_fallback}], errors}`. These fields apply to `prompt` steps **only** —
`interactive` steps run inline in this conductor (your own model) and `skill`
steps run under the invoked skill's frontmatter; ignore `agent:` / `model:` /
`effort:` on every other step type. A step pinning none of them inherits the
parent session's model + effort (the harness setup at run time).

**Model + availability (per member).** All execution is **in-conversation**
(`Task` subagents under the active subscription — no extra API billing; no
subprocess/headless backend). Each member's `model` is already resolved (a
known-retired id swapped for its maintained alias) — pass it as the Task
`model` param, **omitting** the param when it is `inherit`. If a member's
`reason` is non-null, **surface it** (10e line + log: `<role>: <reason>`).
Prefer aliases (`opus`/`sonnet`/`haiku`/`fable`) — they auto-resolve and
rarely retire. On a LIVE model-unavailable failure for a member (the subagent
errors model-not-found), retry that member ONCE with its `next_fallback`; if
that also fails, fail the step.

**Effort (per member).** `Task` has **no per-call effort param**, so each
member's resolved `effort` is conveyed as a **prompt directive only** —
best-effort, may be ignored today (forward-looking, Claude-Code-first). Append
`\n\nReason at <EFFORT> effort — <gloss>.` Glosses: `low` = "be quick, minimal
exploration"; `medium` = "balance speed and rigour"; `high` = "think
carefully, weigh alternatives"; `xhigh`/`max` = "reason exhaustively; weigh
edge cases and failure modes before answering". The `wise:<role>` agent's
frontmatter `effort:` is its standing baseline; a member with null/unset
resolved effort → append nothing. Use the **resolved** effort verbatim —
`resolve-model` / `resolve-team` already applied the model's policy
ceiling (Opus 5 tops out at `high`, so an authored `xhigh` comes back as
`high`, with the step-down in `reason`). Never re-raise it to the step's
authored value.

