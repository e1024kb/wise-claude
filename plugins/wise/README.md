# wise — a Claude Code copilot

A Claude Code plugin that ships everyday git / PR / ticket-planning /
workflow automation as a suite of first-class, autocomplete-visible
slash commands. Every command is flat (`/wise-init`, `/wise-workflow-run`,
`/wise-pr-create`, …); the bare `/wise` command is a discovery aid and
natural-language classifier, not a dispatcher.

`wise` ships a workflow engine, shared scripts, and every action skill
(`/wise-init`, `/wise-workflow-*`, `/wise-skills-*`, `/wise-pr-*`,
`/wise-commit-*`, `/wise-feedback`) plus the `wise-estimation`
reference skill and the `/wise` natural-language helper. Turning a
tracker ticket into a plan — or autonomously all the way into a PR —
is handled by the `ticket-plan` / `ticket-auto` bundled workflows
(see Workflows below).

wise also bundles companion tooling (dependent plugins) that its skills
rely on, so `/plugin install wise@wise-claude` sets up
everything a wise skill needs in one step. See
[**Bundled tooling**](#bundled-tooling) below.

## Install

From this marketplace (see the repo-level README for the marketplace
install step), then:

```
/plugin install wise@wise-claude
```

Update in place:

```
/plugin uninstall wise --keep-data
/plugin install wise@wise-claude
/reload-plugins
```

`--keep-data` preserves your workflow definitions across the reinstall.

### First-time setup

Run once after installing:

```
/wise-init
```

This wizard walks you through installing Python 3 + `pyyaml` /
`python-ulid` / `typing_extensions`, Node ≥22, and the `gh` CLI
(plus `gh auth login`). It skips deps that are already present, so
re-runs are cheap — expect to re-run it after every `/plugin install
wise@…` since that wipes the cached dep registry the wizard writes.
Workflow engine skills (`/wise-workflow-run`, etc.) use the cache as
a fast-path on every subsequent invocation instead of re-probing.

## Commands

Every action is its own flat slash command. Typing `/wise-` and
hitting Tab in Claude Code's slash menu fans out to every command
below.

| Invocation | Description |
|---|---|
| `/wise-init` | First-time setup wizard — walks you through installing Python, Node, and the `gh` CLI, then caches the probe results so future workflow runs skip the live check. Re-run any time your environment changes or after `/plugin install wise@…` (which wipes the cache by design). |
| `/wise-skills-create <skill-name>` | Scaffold a new action or reference skill via Claude Code's `skill-creator`. Marketplace-repo only. |
| `/wise-skills-edit <skill-name>` | Modify an existing wise skill. Refuses to edit the `/wise` helper. Marketplace-repo only. |
| `/wise-workflow-list` | List bundled + user workflow definitions. |
| `/wise-workflow-create <name>` | Wizard to scaffold a new user workflow. |
| `/wise-workflow-run [<workflow-name>]` | Start a workflow run. The main conversation is the conductor. |
| `/wise-workflow-resume [<run-ulid>]` | Resume an interrupted or paused run. |
| `/wise-workflow-status [<run-ulid>]` | List runs in cwd, or dump one run's state. |
| `/wise-workflow-remove <name>` | Delete a user workflow (bundled ones are immutable). |
| `/wise-commit-message [--copy]` | Draft a Conventional-Commits subject line from the pending diff. Read-only — drafts and hands back. |
| `/wise-commit` | Stage every working-tree change (`git add -A`), draft the subject, run `git commit`. Local only. |
| `/wise-commit-push` | Same as `/wise-commit`, then `git push`. Refuses `main` / `master`. |
| `/wise-pr-create` | Create or refresh a PR for the current branch. |
| `/wise-pr-add-reviewers` | Attach Copilot code review + optional individual reviewers to the current branch's PR. |
| `/wise-pr-watch` | Watch CI + drive fixes to green. |
| `/wise-pr-create-auto` | Autonomous `/wise-pr-create` — create/refresh a PR with no prompts (base = repo default branch). |
| `/wise-pr-request-review-auto` | Autonomous `/wise-pr-add-reviewers` — attach Copilot code review with no prompts. |
| `/wise-pr-watch-auto [<max-fix-attempts>]` | Autonomous `/wise-pr-watch` — watch CI, auto-fix failures + bot review comments, loop to green; merges the PR when all checks pass (branch protection respected); no prompts. |
| `/wise-implement-plan-auto [<plan-file>]` | Autonomously implement a `PLAN-*.md` — parallel fresh-context executor agents per task wave, one atomic commit per task. |
| `/wise-feedback [<feedback-text>]` | File a feedback / bug / suggestion issue against `e1024kb/wise-claude` via `gh` — drafts Problem / Summary / Proposal from your prompt + current Claude Code session context, auto-attaches OS / Claude Code version / current git project, previews before submit. Tags `feedback`, assigns to `@e1024kb`. |
| `/wise-insights-mine [--here] [--since <N>d] [--min-count <N>]` | The self-improvement loop (harvest). Mines your local Claude Code session history for recurring task patterns and, once one recurs across enough distinct sessions, drafts it into a reusable skill under `~/.claude/skills/` — after you approve each candidate. Fully local; nothing leaves your machine. See [§ Self-improvement loop](#self-improvement-loop). |
| `/wise-insights-refine [--dry-run] [--min-jaccard <X>] [--include-external]` | The self-improvement loop (garden). Finds overlapping learned skills and, with your approval, merges them into one and retires the originals (reversibly). Acts only on wise-managed skills; never deletes hand-written ones. |

### Self-improvement loop

`wise` learns from how you actually use Claude Code. A single SessionEnd
hook (`hooks/session-end-ingest.sh` — local, no LLM, no network, never
blocks exit) quietly records each finished session into a local ledger
under `~/.local/share/wise/insights/`, keeping only **redacted** prompt
text and tool **names** (never tool inputs).

Run `/wise-insights-mine` whenever you like. It clusters those sessions
by a deterministic recurring-vocabulary fingerprint, counts how many
distinct sessions each pattern appears in, hides machine-generated /
headless prompts, and surfaces the strongest recurring patterns over a
frequency threshold (default 3 sessions). For each candidate you choose
**Draft** (writes a starter skill to `~/.claude/skills/<name>/`),
**Dismiss** (suppressed forever), or **Skip**. Promoted and dismissed
patterns never resurface.

Drafted skills are written with `user-invocable: false`, so they stay **out
of your `/` slash-command menu** — Claude auto-invokes them in the background
when their `description` matches, but they never clutter your command list.
(Delete a skill's directory to remove it, or flip that frontmatter field if
you ever want to type it directly.)

Once you've accumulated learned skills, **`/wise-insights-refine`** is the
*garden* pass to mine's *harvest*: it enumerates your skills, finds overlapping
ones (deterministic token overlap, with the merge confirmed by you), and —
per-group — **merges** redundant skills into one aggregated skill and
**retires** the originals. Retirement is reversible: each retired skill is
copied to `~/.local/share/wise/insights/skill-backups/` first, and only
wise-managed skills (those carrying the provenance marker) are ever retired —
hand-written skills are suggestion-only. `--dry-run` shows the plan without
touching anything. Wipe the insights state with
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/insights.py" purge --yes`.

### The `/wise` natural-language helper

The bare `/wise` command is a discovery aid, not a router:

- **`/wise`** (no arguments) — prints the full catalog of every
  `/wise-*` command the plugin exposes, plus the reference skills
  Claude auto-consults. Useful when you know what the plugin can do
  in the abstract but can't remember the command name.
- **`/wise <free-form text>`** — e.g. `/wise open a PR`, `/wise show
  me running workflows`. Classifies the request against the live
  catalog via LLM judgement, proposes the best-matching `/wise-*`
  command through `AskUserQuestion`, and — on your confirmation —
  invokes it. Never routes silently; you always see the proposal
  before anything runs.

The catalog the helper reads is emitted by `scripts/engine.py
list-skills`.

## Workflows

A **workflow** in wise is a named, reusable, multi-step procedure
defined in YAML. You compose wise actions, third-party skills, shell
commands, and approval gates into a single `/wise-workflow-run <name>`
invocation; the main Claude Code conversation becomes the
**conductor** and drives the DAG wave by wave (parallel fan-out
where steps share `depends_on`, serial where they don't), persists
per-run state at `~/.local/share/wise/runs/<cwd-slug>/<run-ulid>/state.yaml` (honours `XDG_DATA_HOME`),
and supports both interactive (wave-sync) and headless
(synchronous) execution modes. Workflows you interrupt can be
resumed from their last-completed step via
`/wise-workflow-resume <run-ulid>`.

Workflows let you codify recipes like "run tests, open a PR, tag a
release" once, then invoke them repeatably. Six step types are
supported — `skill` (invoke another plugin skill), `prompt` (Task
subagent, parallelisable), `interactive` (main-thread, full
AskUserQuestion access), `bash` (deterministic shell), `approval`
(pause for confirmation, auto-approved in synchronous mode), and
`ask` (capture a free-text or binary user answer).

The project a run operates on is resolved from the current context:
`project-selection: current` auto-detects it from the current git
repository, `prompt` asks you to confirm or override, `any` skips
project resolution.

**Full reference** — definition schema, step-type semantics,
run-state format, resume behaviour, worktree support, dependency
probing, and the author-side walkthrough — lives in
[**`docs/wise/workflows.md`**](../../docs/wise/workflows.md). Start
there when writing your first workflow.

### Bundled workflows

Each bundled workflow ships with a README (linked below) that
documents what it does, its flow diagram, inputs, outputs, and
related skills.

| Workflow | Invocation | Summary |
|---|---|---|
| [`example-workflow`](./workflows/example-workflow/README.md) | `/wise-workflow-run example-workflow` | Reference workflow exercising every step type + parallel-wave dispatch. Safe to run. |
| [`ticket-plan`](./workflows/ticket-plan/README.md) | `/wise-workflow-run ticket-plan` | Tracker ticket → detect tracker + probe access → type-routed parallel research (design spec + related items + codebase audit) → guided decisions → SP-estimated implementation plan → approval. |
| [`ticket-auto`](./workflows/ticket-auto/README.md) | `/wise-workflow-run ticket-auto` | Autonomous ticket → PR pipeline — a Lead Architect + 3 Senior Engineers take each ticket through plan → implement (in a worktree) → commit → push → PR → request review → watch + fix CI to green → merge, end to end, no user prompts. One PR per ticket; a PR is merged when its checks pass, else left open for a human. |

## Skills

A **skill** is the unit of work or reference content wise ships.
The plugin uses two shapes (see the Design notes section below for
the longer rationale):

- **Standalone slash-command skills** — the default shape for every
  action. User-invocable, shown in Claude Code's slash menu as
  `/<skill-name>` (e.g. `/wise-workflow-run`, `/wise-commit-message`).
  Frontmatter carries an `argument-hint:`; the skill body self-parses
  `$ARGUMENTS` as a raw string.
- **Reference / guidance skills** — auto-triggered by the user's
  prose (e.g. `wise-estimation` firing on "story points"). Not
  user-invocable; Claude consults them when the `description:` matches.

Two of the auto-triggered skills are full document-authoring
workflows rather than short reference docs:

- **`wise-prd-architect`** — drives a structured five-phase process
  for writing Product Requirements Documents. Auto-triggers on "PRD",
  "product spec", "feature spec", and similar prose.
- **`wise-trd-architect`** — the engineering counterpart, for
  Technical Requirements Documents. Auto-triggers on "TRD",
  "technical design", "architecture document", "system design".

Each ships its own `agents/` and `references/` files inside its
skill directory.

**You don't write skill frontmatter by hand** — that's what
`/wise-skills-create <name> [<...free-form description>]` is for.
It's a wizard that delegates to Claude Code's `skill-creator`, asks
you which of the two shapes fits (defaulting to standalone),
enforces the wise frontmatter conventions (narrow `allowed-tools`,
correct shape-specific fields), and writes the new SKILL.md into
`plugins/wise/skills/<name>/`. You can even **migrate an existing
Claude Code skill** into the plugin by pasting its intent and path
into the free-form tail.

To modify an existing skill, use `/wise-skills-edit <skill-name>`.
Same wizard, target the existing SKILL.md.

**Full author guide** — frontmatter rules per shape, the wizard
walkthrough, how to migrate a third-party skill — lives in
[**`../../docs/wise/skills-authoring.md`**](../../docs/wise/skills-authoring.md)
and [**`../../CONTRIBUTING.md §4`**](../../CONTRIBUTING.md#4-adding-an-action-to-a-plugin).

## Deeper reading

The plugin's architecture, data shapes, and workflow subsystem are
documented under [`../../docs/wise/`](../../docs/wise/):

- [`dispatcher.md`](../../docs/wise/dispatcher.md) — how the `/wise`
  natural-language helper classifies intent and the
  `scripts/engine.py list-skills` catalog protocol.
- [`skills-authoring.md`](../../docs/wise/skills-authoring.md) —
  adding or modifying wise skills via `/wise-skills-create` /
  `/wise-skills-edit`.
- [`workflows.md`](../../docs/wise/workflows.md) — the workflow
  subsystem: definitions, step types, run state, resume, worktrees,
  dependency handling.

## Bundled tooling

wise is opinionated about dependencies: **anything a wise skill needs
to work, wise installs itself.** Users should never have to configure
a separate MCP server or plugin out of band just because a wise skill
wants to use one.

This is a load-bearing convention: every PR that adds a new wise skill
with an external tool dependency must also add that dependency to one
of the following mechanisms, and update the table below.

### What ships with wise today

| Dependency | Kind | Registered in | Used by |
|---|---|---|---|
| Python 3 + PyYAML + python-ulid + typing_extensions | CLI / runtime — workflow engine's YAML + state store | `plugins/wise/scripts/init.sh` + `plugins/wise/scripts/bootstrap-deps.sh` (probes); registry cached by `/wise-init` at `${CLAUDE_PLUGIN_ROOT}/.wise-init-registry.yaml` | every workflow engine skill (`wise-workflow-run`, `wise-workflow-list`, …) |
| Node ≥22 | CLI / runtime — npx-driven MCP servers | `plugins/wise/scripts/init.sh` + `bootstrap-deps.sh` probes; registry cached by `/wise-init` | MCP servers launched via npx |
| [`gh` CLI](https://cli.github.com) + `gh auth login` | CLI binary — authenticated GitHub client | `plugins/wise/scripts/init.sh` + `bootstrap-deps.sh` probes; registry cached by `/wise-init` | the `wise-pr-*` family of skills and the `ticket-auto` workflow |

`wise` declares no plugin `dependencies`. The `ticket-plan` /
`ticket-auto` workflows work with any task tracker, so instead of
pre-declaring a tracker plugin they detect the tracker at run time,
probe for a matching MCP / CLI, and web-search + propose install
options when none is found.

### How each dependency kind is bundled

- **Plugin-to-plugin** (another plugin on the same marketplace or a
  permitted third-party marketplace) → add to `plugin.json`'s
  `"dependencies": [...]` array. Claude Code auto-installs the listed
  plugins when a user installs wise (v2.1.110+; see
  https://code.claude.com/docs/en/plugin-dependencies.md).
- **MCP server** → add to `plugins/wise/.mcp.json`. Claude Code
  auto-registers the server when the plugin loads. Note that MCP tool
  ids are derived from the plugin name
  (`mcp__plugin_<plugin>_<server>__<tool>`); both the `.mcp.json`
  entry AND the consuming skills' `allowed-tools` list must stay in
  sync.
- **CLI binary or language runtime** that neither of the above can
  install (Python, `brew` packages, system tools) → a bootstrap
  script probes at run time and, if missing, surfaces a one-shot
  install prompt via `AskUserQuestion`. The consuming skill runs the
  bootstrap as its first step. See `scripts/bootstrap-deps.sh` for
  the reference implementation.

### Why this convention

Two reasons:

1. **Single install step.** A user who wants to try a wise skill
   shouldn't also need to read an MCP install block, paste a JSON
   blob into their Claude Code config, and restart. The moment we ask
   a user to do manual config, many of them won't, and the skill
   silently under-performs.
2. **Reproducibility and versioning.** If the dependency config is
   bundled with wise, a given wise version pins specific tooling.
   Upgrades are atomic — you `/plugin install` and get the matching
   tooling. No "works on my machine" drift.

## Contributing

See the repo's [`CONTRIBUTING.md`](../../CONTRIBUTING.md) for the full
contributor guide — adding actions, commit and PR conventions, local
testing. Invariants for agents editing the plugin are in
[`CLAUDE.md`](./CLAUDE.md).

## Design notes

- **Autocomplete over routing.** Every action is a first-class
  slash command so Claude Code's completer does the discovery work
  that a dispatcher used to do. No text-token parsing, no fuzzy
  matcher, no hidden action skills — typing `/wise-` fans out the
  full menu inline.
- **Natural-language helper as the fallback.** `/wise` bare + `/wise
  <free-form>` is the answer for users who'd rather describe what
  they want than memorise names. It classifies against the live
  catalog (from `engine.py list-skills`), proposes one match via
  `AskUserQuestion`, and never routes silently.
- **Adding an action** is a single-step operation: drop a skill
  directory into `plugins/wise/skills/<name>/` with `argument-hint:`
  frontmatter. It appears in autocomplete as `/<name>` on the next
  `/reload-plugins`.
- **Workflows are a first-class subsystem, not a bolt-on.** The
  conductor runs in the main Claude Code conversation; state is
  per-workspace YAML; parallelism uses Claude Code's one-message-
  multiple-tool-calls pattern; dependencies are probed (never
  auto-installed).
