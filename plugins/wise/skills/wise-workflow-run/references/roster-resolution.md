# wise-workflow-run/references/roster-resolution — tuning override, model, effort

Companion to the conductor SKILL.md §10d. Read it the FIRST time a
wave dispatches a `type: prompt` step (any run that reaches dispatch);
it defines how a recorded tuning override binds at dispatch, how each
member's model is passed and falls back, and how effort is conveyed.
Schema/semantics live in `docs/wise/workflows.md § Agents, model and
effort`.

**Tuning override (only when the run recorded one).** Overrides come
from the run's state outputs, recorded by §6b2 (on resume, re-shell
`get-tuning "$DEF"` once and read the outputs from state rather than
trusting conversation memory). Precedence, most specific first:

1. `tuning_step_<step-id>` (Custom's per-step pick; hyphens in the
   step id appear as underscores in the output name) — always wins.
2. `tuning_<group.id>` that is not `default`, when this step's id is
   in that group's `steps:` list.
3. Nothing recorded → the step's declared pins stand.

With a winning `<model> / <effort>` value, append it to the call —
and, when the run recorded `team_mode=solo`, always add
`--team-mode solo` (also on calls with no model override). When the
run recorded `run_profile` as `low` / `medium` / `max`, ALWAYS add
`--profile <level>` too (on every `resolve-team` AND `resolve-model`
call, override or not; `custom` / unset → omit the flag):

```bash
python3 .../workflows.py resolve-team "$DEF" "<step.id>" --model <m> --effort <e> --team-mode <full|solo> --profile <low|medium|max>
```

**Low-profile Opus rule (MUST).** `--profile low` makes the engine
resolve every Opus-family pin — the `opus` alias, a `claude-opus-5*`
id, a retired id, a tuning override — to `claude-opus-4-8`, with the
swap in `reason` (`low profile: opus→claude-opus-4-8 (Opus 5 is never
used at low)`). A `low` run never dispatches Opus 5. Pass the
resolved `model` verbatim to `Task`; never re-substitute the alias.

The model/effort override wins over the step's AND every team
member's pinned model/effort (the engine notes `run tuning override`
in each member's `reason` — surface it on the 10e line like any other
reason). Under `--team-mode solo` a team step comes back
`mode: single` with an additive `collapsed: {from, dropped}` key —
append to the dispatched prompt:
`Solo mode: also cover these dropped lenses briefly: <dropped roles>.`
and log `team collapsed: <from>→1 (<dropped>)`. Groups
with no `steps:` binding (advisory groups) are not applied here —
their choice reaches the workflow through the `{{tuning_<id>}}` /
`{{tuning_summary}}` / `{{cap_<name>}}` template outputs instead. For
a step that resolves through `resolve-model` (mode `off` / `auto` /
policy fallback), apply the same winning override by passing `<m>` /
`<e>` as the `resolve-model` arguments in place of the step's own
pins.
→ JSON `{mode, lead, members:[{role, lead, model, effort, reason, fell_back,
next_fallback}], errors, collapsed?: {from, dropped}}` — `collapsed` is
present only under `--team-mode solo` (see below); its absence means
the run is not collapsing a team. These fields apply to `prompt` steps **only** —
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

