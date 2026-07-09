# CLAUDE.md — operational context for the `wise` plugin

Loaded automatically when a developer opens this plugin in Claude Code.
This file records **invariants** — things that must be true whenever the
plugin is at rest. Procedures (how to add an action, how to test) live
in the root [`CONTRIBUTING.md`](../../CONTRIBUTING.md); do not duplicate
them here.

**Before making any non-trivial change, read `CONTRIBUTING.md`.** It
documents the decisions that led to the current shape of the plugin —
most "obvious improvements" have already been considered and rejected
for reasons that are listed there.

---

## What this plugin is

`wise` is a standalone copilot plugin in the `wise-claude`
marketplace: a workflow engine, shared scripts, and a set of
tech-neutral action skills (`/wise-init`, `/wise-workflow-*`,
`/wise-skills-*`, `/wise-pr-*`, `/wise-commit-*`) plus the
`wise-estimation` reference skill and the `/wise` natural-language
helper.

Every user-facing skill is a **flat, autocomplete-visible slash
command**. No dispatcher-style routing: typing `/wise-` fans out in
Claude Code's slash menu to every action. The plugin also exposes the
`/wise` bare command as a **natural-language helper** — typing `/wise`
alone prints the catalog of every command, and typing `/wise <free-form
text>` (e.g. `/wise open a PR`) classifies the request via LLM
judgement, proposes the matching `/wise-*` command, and offers to run
it through `AskUserQuestion`.

Current actions (all standalone):

- `/wise-init` — first-time dep-install wizard; walks the user through
  Python + Node + gh, caches results for the workflow engine fast-path.
- `/wise-skills-create` — scaffold a new action skill via `skill-creator`.
- `/wise-skills-edit` — modify an existing action skill via `skill-creator`.
- `/wise-workflow-list` — list bundled + user workflow definitions.
- `/wise-workflow-create` — wizard to scaffold a new user workflow.
- `/wise-workflow-run` — start a new workflow run (this conversation is the conductor).
- `/wise-workflow-resume` — continue an interrupted or paused run.
- `/wise-workflow-status` — inspect runs in the current workspace.
- `/wise-workflow-remove` — delete a user workflow definition.
- `/wise-feedback` — file a feedback issue against the marketplace repo.
- `/wise-insights-mine` — the self-improvement loop (the "harvest"
  pass). Mines the local insights ledger (fed by the SessionEnd hook)
  for recurring task patterns, frequency-gates them, and drafts the
  strongest ones into user-global `~/.claude/skills/` after
  per-candidate approval.
- `/wise-insights-refine` — the "garden" pass. Enumerates the learned
  skills, finds overlapping ones, and (with approval) merges them into
  one aggregated skill and retires the originals — reversibly (backed
  up first). Acts only on wise-managed skills (marker-tagged); never
  deletes hand-written ones.
- `/wise-insights-reset` — reversible cleanup + rollback. Snapshots
  then removes the auto-created skills and/or the insights index into
  `snapshots/<ts>/`, and restores any snapshot. Only managed skills;
  never hard-deletes (that's `insights.py purge`).
- `/wise-commit-message`, `/wise-commit`, `/wise-commit-push`,
  `/wise-pr-create`, `/wise-pr-add-reviewers`, `/wise-pr-watch` —
  standalone PR / git helpers. The three commit skills are a graded
  trio: `/wise-commit-message` is read-only (drafts and hands back),
  `/wise-commit` drafts + commits locally, `/wise-commit-push` drafts
  + commits + pushes.
- `/wise-pr-create-auto`, `/wise-pr-request-review-auto`,
  `/wise-pr-watch-auto`, `/wise-implement-plan-auto`,
  `/wise-code-review-auto`, `/wise-simplify-auto` — the autonomous
  (`-auto`) building blocks: decision-free, `AskUserQuestion`-free
  variants of the PR / implement / quality steps, each a thin reader
  of a shared fragment or reference. `/wise-simplify-auto` (the
  lightweight per-commit tier — the `code-simplifier` agent) and
  `/wise-code-review-auto` (the heavyweight branch gate — a high-depth
  panel of reviewer subagents) are the two quality passes; the
  `ticket-auto` workflow follows the same fragments / references.
- `/wise-supervise` — attach a watchdog / supervisor loop to a running
  team of background agents and keep them on task: probe each member,
  nudge the idle-but-unfinished or off-goal ones, escalate the
  persistently stuck (the automation of manually typing "ping all your
  subagents, are you on track?"). Reads the shared
  `references/supervise-loop.md`; the workflow engine runs the same
  routine for `type: supervised-prompt` steps and the `-auto` implement
  phase (`SUPERVISE=yes`).
- `/wise-revise` — the proactive planner: investigates a scope (folder /
  component / whole project) against a free-form improvement intent,
  read-only, via a panel of roster lenses; ranks findings by leverage and
  writes self-contained `PLAN-*.md` plans + an index into `docs/plans/`
  for the user to execute later. Writes only under `docs/plans/`; never
  edits source and never runs a plan (execution is delegated). Reads its
  own skill-local `references/audit-lenses.md` + `references/plan-format.md`.
- `/wise-grill` — the subject-understanding pass: classifies its input
  (tracker ticket, doc link, free-form prompt, or question), then
  deep-researches it across every reachable source (tracker comments +
  screenshots, wiki, Slack, Drive, design, codebase + git history),
  gap-checks the evidence, and forks by type — a ready
  `docs/plans/PLAN-<ref>.md`; a `BLUEPRINT-<ref>.md` with targeted
  questions (per-person for tickets / docs, asked inline when the user
  is the one who can answer — prompts); or a researched
  `ANSWER-<ref>.md` for a pure question (re-run with answers to
  upgrade a blueprint). Facts get researched; only decisions get
  asked. Writes only under `docs/plans/`; read-only against every
  external system. Reads the shared `references/grill/*` routines —
  the same ones the `ticket-plan` / `ticket-auto` workflows run in
  their plan phases (ticket subjects only there).
- `/wise` — the natural-language helper (bare = catalog; with free-form
  text = intent classifier).

Two model-invoked document-authoring skills round out the plugin —
they carry no `argument-hint`, so they auto-trigger on matching prose
rather than being typed as a flat command:

- `wise-prd-architect` — drives a structured multi-phase process for
  writing Product Requirements Documents; auto-triggers on "PRD",
  "product spec", "feature spec", and similar.
- `wise-trd-architect` — the engineering counterpart; auto-triggers on
  "TRD", "technical design", "architecture document", "system design".

Each ships its own `agents/` and `references/` files *inside its
skill directory* (the skill spawns those agents via `Task` and reads
the reference templates). These skill-local agents are distinct from the
**plugin-level agent roster** under `agents/` (see below) — the
skill-local ones are private to one skill; the roster is shared across
every workflow.

The plugin also ships a **plugin-level `agents/` roster** — a set of
SDLC role subagents (`wise:ceo`, `wise:cto`, `wise:architect`,
`wise:software-engineer`, `wise:qa-engineer`, `wise:security-engineer`,
`wise:devops-engineer`, `wise:sre`, `wise:code-reviewer`, …), catalogued
in `AGENTS.md`. They are real Claude Code plugin subagents (invocable as
`subagent_type: wise:<name>`) that the workflow engine dispatches
`prompt` steps to via the step-level `agent:` field and the
workflow-level `agents:` policy.

---

## Layout

```
plugins/wise/
├── .claude-plugin/plugin.json      # manifest (declares plugin-level `dependencies:`)
├── .mcp.json                       # bundled MCP servers (currently empty; see README § Bundled tooling)
├── hooks/                          # the ONE sanctioned hook (see CONTRIBUTING §2.4)
│   ├── hooks.json                  # auto-discovered; registers the SessionEnd hook
│   └── session-end-ingest.sh       # SessionEnd → insights.py ingest (stdlib-only, exit 0, no LLM)
├── agents/                         # plugin-level SDLC agent roster (auto-discovered; wise:<name>)
│   ├── ceo.md  cto.md  product-manager.md  engineering-manager.md
│   ├── architect.md  software-engineer.md  qa-engineer.md
│   ├── security-engineer.md  devops-engineer.md  sre.md
│   └── ux-designer.md  technical-writer.md  code-reviewer.md
├── CLAUDE.md                       # this file (invariants)
├── AGENTS.md                       # catalog/index of the plugin-level agent roster
├── README.md                       # slim overview + links into /docs/wise/*
├── LICENSE
├── .gitignore                      # keeps .wise-init-registry.yaml out of source control
├── .wise-init-registry.yaml        # RUNTIME ONLY — written by `/wise-init`, wiped on reinstall
├── scripts/
│   ├── engine.sh                   # thin bash bootstrap → execs engine.py
│   ├── engine.py                   # skill-catalog emitter (`list-skills` subcommand) — consumed by the /wise helper
│   ├── bootstrap-deps.sh           # full dep probe (python3 + pyyaml/ulid/typing_extensions, node ≥22, gh + auth); cold-start fallback
│   ├── init.sh                     # bash-only per-dep probes used by `/wise-init` (works before Python is installed)
│   ├── init-registry.py            # YAML I/O for .wise-init-registry.yaml + fast-path `check` for workflow engine
│   ├── workflows.py                # workflow subsystem: YAML + state + ULID + dep-probe
│   └── insights.py                 # self-improvement engine: ingest/mine/gate sessions → skill candidates (STDLIB ONLY)
├── workflows/                      # bundled workflow definitions (shipped defaults)
│   └── <name>/                     # folder form: workflow.yaml + sibling artifacts
│       ├── workflow.yaml           # the definition
│       ├── templates/              # optional — workflow-shipped templates; addressable as {{workflow.dir}}/templates/…
│       └── prompts/                # optional — e.g. watch-pipelines-auto.md; addressable as {{workflow.dir}}/prompts/…
├── references/                     # cross-skill shared prose (addressed as ${CLAUDE_PLUGIN_ROOT}/references/<file>.md)
│   ├── subject-drafting.md         # Conventional-Commits scope / type / subject rules
│   ├── branch-naming.md            # the ticket = branch-name rule
│   ├── init-check.md               # shared init-registry fast-path protocol
│   ├── simplify-pass.md            # canonical per-commit simplify pass (code-simplifier agent)
│   ├── code-review-pass.md         # canonical high-depth branch review (reviewer-subagent panel)
│   ├── supervise-loop.md           # the watchdog routine (idle/hung detection → nudge → escalate); read by supervised-prompt + /wise-supervise
│   ├── insights-init-guard.md      # /wise-init gate read by wise-insights-mine / -refine
│   ├── grill/                      # the subject-understanding routines (context sweep + gap analysis + blueprint schema) — read by /wise-grill (any subject), ticket-plan, ticket-auto (tickets)
│   └── pr/                         # shared PR/commit fragments (draft-body, ensure-pr, watch-pipelines, handle-*, commit-from-fix, paged-bulk-mode) + templates/pr-template.md — read by the wise-pr-* skills + ticket-auto
└── skills/
    ├── wise/SKILL.md               # natural-language helper (bare catalog + intent classifier)
    ├── wise-init/SKILL.md          # dep-install wizard
    ├── wise-skills-create/SKILL.md
    ├── wise-skills-edit/SKILL.md
    ├── wise-workflow-list/SKILL.md
    ├── wise-workflow-create/SKILL.md
    ├── wise-workflow-run/SKILL.md   # the conductor
    ├── wise-workflow-resume/SKILL.md
    ├── wise-workflow-status/SKILL.md
    ├── wise-workflow-remove/SKILL.md
    ├── wise-prd-architect/           # model-invoked PRD authoring (SKILL.md + agents/ + references/)
    ├── wise-trd-architect/           # model-invoked TRD authoring (SKILL.md + agents/ + references/)
    ├── wise-feedback/SKILL.md       # file a feedback issue
    ├── wise-insights-mine/SKILL.md  # self-improvement loop: mine sessions → draft skills
    ├── wise-insights-refine/SKILL.md # consolidate learned skills: merge overlaps → retire originals
    ├── wise-insights-reset/SKILL.md  # reversible cleanup + rollback (snapshot → clear → restore)
    ├── wise-commit-message/SKILL.md # Conventional-Commits drafter (read-only)
    ├── wise-commit/                  # draft + commit (no push)
    │   ├── SKILL.md
    │   └── commit-routine.md        # shared draft + commit (+ optional push) procedure
    ├── wise-commit-push/SKILL.md    # draft + commit + push; reads wise-commit/commit-routine.md
    ├── wise-estimation/SKILL.md     # reference skill (Fibonacci SP scale)
    ├── wise-pr-create/SKILL.md      # create or refresh a PR
    ├── wise-pr-add-reviewers/SKILL.md  # attach Copilot + extras
    ├── wise-pr-watch/SKILL.md       # drive pipelines + comments to green
    ├── wise-pr-create-auto/SKILL.md       # autonomous PR create (no prompts)
    ├── wise-pr-request-review-auto/SKILL.md  # autonomous Copilot attach (no prompts)
    ├── wise-pr-watch-auto/SKILL.md        # autonomous CI watch + fix loop (no prompts)
    ├── wise-implement-plan-auto/          # autonomously implement a PLAN-*.md
    │   ├── SKILL.md
    │   └── agents/executor.md            # fresh-context per-task executor persona
    ├── wise-code-review-auto/SKILL.md     # autonomous high-depth branch code-review (no prompts)
    ├── wise-simplify-auto/SKILL.md        # autonomous simplify + commit (no prompts)
    ├── wise-supervise/SKILL.md            # attach the watchdog loop to a running team of background agents
    ├── wise-revise/                        # proactive planner: audit a scope → executable PLAN-*.md backlog
    │   ├── SKILL.md
    │   └── references/                    # audit-lenses.md (the panel) + plan-format.md (the plan + index schema)
    └── wise-grill/SKILL.md                 # subject-understanding pass (ticket / doc / prompt / question): multi-source research → PLAN, BLUEPRINT-with-questions, or ANSWER
```

No `commands/` directory is present, and one must not be added without
the discussion called for in `CONTRIBUTING.md`
[§2](../../CONTRIBUTING.md#2-conventions-that-apply-to-every-plugin). An
`agents/` directory **IS** present — the plugin-level SDLC role roster
(`AGENTS.md` + `agents/*.md`), auto-discovered by Claude Code and
addressable as `subagent_type: wise:<name>`. It is a deliberate part of
the plugin's design (the workflow engine dispatches `prompt` steps to
it); adding or editing a role follows the procedure in `AGENTS.md`. A
`hooks/` directory IS present, holding **exactly one** sanctioned hook —
the SessionEnd insights-ingest hook (`hooks/session-end-ingest.sh` +
`hooks/hooks.json`). It is the single documented exception to the
no-hooks default; its rationale and hard constraints live in
`CONTRIBUTING.md` [§2.4](../../CONTRIBUTING.md#24-hooks). No other hook
(and no `SessionStart` hook) may be added without that same discussion.
`.mcp.json` IS present — it bundles the MCP servers wise skills depend
on (currently empty; see the bundled-tooling convention in
`CONTRIBUTING.md` [§2.2](../../CONTRIBUTING.md#22-bundled-tooling-convention)).

---

## Invariants

Keep these true. Each one has a rationale in `CONTRIBUTING.md`; the
one-liners below are the rule, not the argument for it.

- **Two skill shapes — standalone and reference.** Every skill's
  frontmatter lands in one of two buckets. Pick the right one when you
  author; mixing fields breaks discovery.
  - **Standalone slash-command skills** (the default shape for every
    action, e.g. `wise-workflow-run`, `wise-commit-message`) —
    user-invocable, shown in the slash menu as `/wise:<skill-name>`
    with a bare `/<skill-name>` alias when unambiguous. Frontmatter
    sets `argument-hint:` (may be an empty string). No `command:` /
    `subcommand:` / `user-invocable: false` / `arguments:` fields —
    those were v1 dispatcher-routing fields with no meaning in v2.
    The skill body reads `$ARGUMENTS` as a raw string and parses its
    own positionals.
  - **Reference / guidance skills** (e.g. `wise-estimation`) —
    description-triggered docs. Frontmatter has NO `argument-hint:`
    and no action-oriented fields. Claude auto-consults them when the
    user's prose matches the `description:`. Body is reference
    content, not action logic.
- **Exactly one `disable-model-invocation: true` skill — `wise`.** It's
  the natural-language helper. Users invoke it explicitly; Claude
  never auto-runs it.
- **The `/wise` helper classifies intent; it does not act directly.**
  Action logic lives in the action skills. The helper's job is to
  (a) print the catalog when called bare, or (b) pick the best
  matching `/wise-*` command and offer to invoke it via the `Skill`
  tool after `AskUserQuestion` confirmation.
- **`scripts/engine.py` is a catalog emitter, nothing more.** Its
  only supported subcommand is `list-skills`, which walks `skills/`
  and emits a JSON document the `/wise` helper consumes. No routing,
  no fuzzy matching, no argument parsing. `engine.sh` stays as the
  bash bootstrap; every skill that calls `engine.sh` grants the bash
  path in its `allowed-tools`.
- **Action skills never invoke other action skills.** The `/wise`
  helper is the only place that calls the `Skill` tool on a
  wise-namespaced action skill (and only after user confirmation).
  Action-to-action work-sharing happens through `scripts/` helpers.
  The exception for `wise-workflow-run` and `wise-workflow-resume`
  (which compose other skills as workflow steps) is below.
- **All persistent state lives in `${CLAUDE_PLUGIN_DATA}`.** Never
  write elsewhere — with TWO narrow exceptions:
  (a) the init registry, see below;
  (b) workflow run state, which is per-workspace by design and lives
  under `~/.local/share/wise/runs/<cwd-slug>/` (off-tree, off
  `.claude/**`, never auto-cleaned); and
  (c) the **insights store** under `~/.local/share/wise/insights/`
  (`ledger.jsonl` + `candidates.json` + `decisions.json` +
  `skill-backups/<ts>/<name>/` + `snapshots/<ts>/{index,skills}/`), the
  self-improvement loop's per-user state. `decisions.json` records
  `promoted` / `dismissed` / `retired` (the last set by
  `/wise-insights-refine` when it merges a skill away; `mine` resurrects
  it if the merged skill is later deleted). `snapshots/` holds
  `/wise-insights-reset` restore points (reversible cleanup); `purge
  --yes` is the only irreversible wipe. New per-user persistent state
  that doesn't fit `${CLAUDE_PLUGIN_DATA}` MUST route through the
  `wise_data_root()` helper in `scripts/workflows.py` — never
  hard-code paths so future relocations are one-function changes.
  (`insights.py` mirrors that helper with a stdlib-only fallback,
  because the SessionEnd hook may run before pyyaml is installed; the
  canonical helper is still used whenever importable.)
- **Init registry — the one file we write inside `${CLAUDE_PLUGIN_ROOT}`.**
  `/wise-init` caches probe results at
  `${CLAUDE_PLUGIN_ROOT}/.wise-init-registry.yaml`. It lives in the
  install dir on purpose: every `/plugin install wise@…` wipes it,
  giving natural invalidation. Workflow engine skills read this as a
  fast-path via `scripts/init-registry.py check` before falling back
  to the full `bootstrap-deps.sh` probe. Two writers: `/wise-init`
  (full payload, interactive) and `bootstrap-deps.sh` itself, which
  auto-populates a successful-probe subset on its way out. `.gitignore`
  keeps the registry out of source control.
- **No persisted project registry.** The `{{project.*}}` template
  variables a workflow run operates on are derived from the current
  context — `wise-workflow-run` §7 resolves them from the current git
  repository / working directory (`project-selection: current`) or by
  asking the user (`project-selection: prompt`). There is no
  `projects.yaml` and no skill that writes one.
- **`allowed-tools` in each skill is narrowly scoped.** Expanding it
  should be a deliberate decision, not an incidental fix-up.
- **`model` / `effort` frontmatter follows the work, not the skill.**
  Lightweight, mechanical, or read-only skills (the commit-drafting
  trio, `wise-workflow-list` / `-status` / `-remove`, `wise-feedback`)
  pin `model: opus` + `effort: low` for snappy turnaround. Skills that
  do real reasoning or orchestration (`wise-pr-watch`, the workflow
  conductor / resume, the wizards, the PRD/TRD architects) omit both
  and inherit the session model — `effort: low` would hurt them. Set
  the knobs to match the skill's cognitive load.
- **The agent roster is plugin-level; workflow agent binding is
  `prompt`-only.** The `agents/*.md` roster files are real Claude Code
  plugin subagents — frontmatter is limited to `name` / `description` /
  `tools` / `model` / `effort` / `color`; plugin subagents **ignore**
  `hooks` / `mcpServers` / `permissionMode`, so never add those. Roster
  `model:` is `inherit` (they follow the session or a step override);
  `effort:` is the role's default reasoning level. In workflows the
  `agent:` / `model:` / `effort:` step fields and the workflow-level
  `agents:` policy bind ONLY to `type: prompt` steps — `interactive`
  steps run inline in the conductor (its own model) and `skill` steps run
  under the invoked skill's frontmatter. `agent:` is **scalar or a list**:
  a list is a **team** (items = bare role or `{role, lead?, model?, effort?}`)
  dispatched as parallel `wise:<role>` subagents, an optional single `lead`
  integrating peers' drafts, then **conductor-synthesized** into one result.
  The conductor normalizes `agent:` via `workflows.py resolve-team` (per-member
  model resolution + role/lead validation); a team step stays **atomic** so a
  mid-team resume re-runs it whole — no new run state. **All step execution is
  in-conversation** (`Task` subagents, subscription-covered — there is NO
  subprocess/headless backend; a headless `claude -p` would bill as
  separate API usage outside the subscription, so it is off-limits for
  step execution). `model:` is a native Task per-call override (the real
  per-step knob); `effort:` is NOT a native per-call knob in-conversation,
  so it is conveyed as a prompt directive only (best-effort, may be
  ignored — forward-looking). The conductor resolves the model through
  `workflows.py resolve-model` first (retired-id substitution + effort
  clamp + tier fallback + user-facing reason). Keep `AGENTS.md`'s catalog
  table in sync with `agents/*.md`, the same way workflow READMEs stay in
  sync with YAML.
- **The roster is canonical; never hand-maintain a divergent copy.**
  `agents/*.md` is the single source. The repo-root `AGENTS.md` and
  `plugins/wise/AGENTS.md` document it as project-*instructions* (not
  loadable registries); their roster tables mirror `agents/*.md` and are
  kept in sync the same way workflow READMEs track their YAML.
- **Cross-skill shared prose lives in `plugins/wise/references/`.** A
  rule or routine read by more than one skill — the Conventional-Commits
  `subject-drafting.md` (read by the commit routine, `wise-commit-message`,
  and `draft-body.md`), the workflow `init-check.md` (read by
  `wise-workflow-run` / `-resume` / `-list` / `-status`), and the two
  quality passes `simplify-pass.md` (read by the commit routine, the
  implement phase, and `wise-simplify-auto`) and `code-review-pass.md`
  (read by `review-branch-auto.md` and `wise-code-review-auto`), the
  watchdog routine `supervise-loop.md` (read by the `supervised-prompt`
  step, the `-auto` implement phase, and `wise-supervise`), the
  `references/grill/` subject-understanding routines
  (`research-sources.md` + `gap-analysis.md` + `blueprint-format.md`,
  read by `/wise-grill` and the `ticket-plan` / `ticket-auto` plan
  phases), and the
  `references/pr/` PR/commit fragments (`draft-body.md`, `ensure-pr.md`,
  `ensure-reviewers.md`, `propose-reviewers.md`, `watch-pipelines.md`,
  the `handle-*.md` queue handlers, `paged-bulk-mode.md`,
  `commit-from-fix.md`, read by the `wise-pr-*` skills and the
  `ticket-auto` workflow) — has a single home there, addressed as
  `${CLAUDE_PLUGIN_ROOT}/references/<file>.md` and read at run time.
  Skill-*local* `references/` (the architects' own templates) stay
  inside the skill dir. Change a shared rule in the reference, never in
  a copy.
- **Action skill directory name IS the slash command.** A skill at
  `skills/wise-workflow-run/SKILL.md` with frontmatter `name:
  wise-workflow-run` is invocable as `/wise-workflow-run` (bare
  alias) or `/wise:wise-workflow-run` (canonical namespaced). There
  is no translation layer. Rename the dir → rename the command; do
  both in the same commit, and flag the change as breaking.
- **Workflow run state lives under `~/.local/share/wise/runs/<cwd-slug>/<run-ulid>/`**
  (honours `XDG_DATA_HOME`). Per-workspace scoping is preserved via
  the `<cwd-slug>` segment. Why off the project tree: Claude Code's
  sensitive-path heuristic flags `.claude/**` paths; putting state in
  the project tree also adds gitignore management and IDE noise. Why
  user-scoped rather than `/tmp`: `/tmp` auto-cleans and breaks the
  resume contract. Never under `${CLAUDE_PLUGIN_DATA}`. Workflow
  *definitions* are the opposite — they live under a `workflows/` root
  (user-authored at `${CLAUDE_PLUGIN_DATA}/workflows/definitions/`,
  shipped at `${CLAUDE_PLUGIN_ROOT}/workflows/`) in one of two layouts:
  - `<root>/<name>/workflow.yaml` — **folder form, preferred**. The
    workflow can ship its own artifacts as siblings (`templates/`,
    `prompts/`) and address them from steps via `{{workflow.dir}}`.
  - `<root>/<name>.yaml` — **legacy flat form**. Still accepted; no
    artifacts dir.

  Folder form wins on same-root collision. User root still wins over
  bundled root on cross-root collision. See
  [`../../docs/wise/workflows.md`](../../docs/wise/workflows.md) for
  the full reference.
- **Exception to "action skills never invoke other action skills" —
  `wise-workflow-run` and `wise-workflow-resume` only.** Those two
  skills are composition-over-skills by design. The exception is
  narrowly scoped: (a) only these two may call `Skill` on a
  wise-namespaced action skill; (b) only as part of a validated
  workflow YAML's `type: skill` steps; (c) never re-entering the
  `wise` helper (no calling `wise:wise`). Every other action skill
  still obeys the blanket rule.
- **Workflow README.md stays in sync with `workflow.yaml` +
  `prompts/`.** When you touch a bundled workflow's YAML or any of
  its `prompts/*.md` fragments, update the workflow's `README.md` in
  the SAME commit — Flow mermaid, Steps table, Inputs/Outputs tables,
  and Related-links section must reflect the new shape.
- **The auto-orchestrators are idempotent on resume.** `ticket-auto`'s
  `process-tickets.md` and `impl-plan-auto`'s `process-plans.md` are one
  implementation (word-for-word identical modulo the unit noun + the §1/§2
  re-plan deltas) and run as a single all-or-nothing `interactive` step, so a
  compaction can orphan them mid-run. Their §1 must *ensure* (create,
  re-attach, or adopt) each unit's worktree from a per-unit ledger under
  `{{run.dir}}/units/` + live `git`/`gh` probes — never reintroduce a
  collide-and-fail `git worktree add -b`. Live state is the source of truth;
  the ledger is a hint; a worktree/branch the run did not claim is skipped,
  never adopted. After ensuring a worktree, §1 carries over the base repo's
  `.worktreeinclude` files via `workflows.py apply-worktree-include`, gated by
  an `includes=done` ledger key so it runs once per worktree (not re-clobbering
  on resume re-attach). Keep the two files mirrored.
- **All workflow YAML + state handling lives in `scripts/workflows.py`.**
  SKILL.md bodies shell out to it; they never parse YAML themselves.
  `scripts/bootstrap-deps.sh` is the single doorway to Python — every
  workflow skill runs it first.
- **External-tool dependencies — bundle the static ones, probe the
  open-ended ones.** When a skill needs a third-party tool, prefer
  declaring it so the install is one step; but when the *set* of
  possible tools is open-ended, probe and propose at run time instead.
  Concretely:
  - Plugin-to-plugin deps, when fixed and known, go in
    `.claude-plugin/plugin.json`'s `dependencies: [...]` array (the
    marketplace's `allowCrossMarketplaceDependenciesOn` permits a
    cross-marketplace entry). `wise` currently declares none.
  - MCP server deps go in `.mcp.json` (currently empty). MCP tool ids
    are derived from the plugin name, so moving an MCP between plugins
    is a breaking rename.
  - CLI / environment deps that neither mechanism can install (Python,
    brew packages) are probed at run time by
    `scripts/bootstrap-deps.sh` and surface a one-shot install prompt.
  - Open-ended deps — where the workflow or skill cannot know up
    front *which* tool the user needs — are probed and proposed
    dynamically. The `ticket-plan` / `ticket-auto` workflows are
    the reference case: they work with any task tracker, so they
    detect the tracker, probe for a matching MCP / CLI, and
    web-search + propose install options when none is found, rather
    than pre-declaring a specific tracker plugin.
  See CONTRIBUTING.md [§2.2](../../CONTRIBUTING.md#22-bundled-tooling-convention) for the full convention.

---

## Pointers for common tasks

For the full procedure on each of these, read the linked section of
`CONTRIBUTING.md`:

| Task | See |
|---|---|
| Add a new `/wise-<action>` command | `CONTRIBUTING.md` [§4](../../CONTRIBUTING.md#4-adding-an-action-to-a-plugin) |
| Install the plugin from this clone | `CONTRIBUTING.md` [§6.1](../../CONTRIBUTING.md#61-install-the-plugin-from-a-clone) |
| Validate JSON / bash / naming before commit | `CONTRIBUTING.md` [§6.2](../../CONTRIBUTING.md#62-syntax-and-structural-checks) |
| Commit and PR style | `CONTRIBUTING.md` [§7](../../CONTRIBUTING.md#7-commit-and-pr-conventions) |
| Versioning rules for this plugin | `CONTRIBUTING.md` [§8](../../CONTRIBUTING.md#8-versioning) |
| Add or modify workflow-subsystem behaviour | `CONTRIBUTING.md` [§9](../../CONTRIBUTING.md#9-workflow-subsystem) |

---

## Why `CLAUDE.md`, `README.md`, and `CONTRIBUTING.md` all exist

Three audiences, three stability contracts:

- `README.md` — users landing on GitHub or running `/plugin info`.
  Describes what the plugin does and how to use it.
- `CLAUDE.md` (this file) — an agent working inside the plugin tree.
  Short list of invariants and a pointer to the full procedures.
- `CONTRIBUTING.md` — the full procedural manual. Single source of
  truth for *how* to change things.

Overlap between the three is limited on purpose. If you find the same
rule documented in two of them and they disagree, the code is the truth
and both files are stale — fix both.
