# Contributing to wise-claude

This document is the single source of truth for **how** to contribute to the
`wise` plugin in this marketplace. The READMEs describe *what* the plugins do;
[`plugins/wise/CLAUDE.md`](./plugins/wise/CLAUDE.md) records per-plugin
invariants in a short form suitable for AI agents; this file holds the full
procedures.

Read the section that matches your task. Skim the rest — the "Things to
avoid" lists have been written from real mistakes.

---

## 1. Repo layout

```
wise-claude/
├── .claude-plugin/
│   └── marketplace.json          # lists every plugin in this repo
├── CONTRIBUTING.md               # this file
├── README.md                     # marketplace-level user docs
├── docs/
│   └── wise/                      # wise plugin architecture docs
└── plugins/
    └── wise/                      # workflow engine, shared scripts, flat /wise-* commands, natural-language /wise helper
        ├── .claude-plugin/plugin.json
        ├── CLAUDE.md
        ├── README.md
        ├── .mcp.json             # bundled MCP servers (currently empty)
        ├── AGENTS.md             # catalog/index of the agent roster
        ├── agents/               # plugin-level SDLC role roster (wise:<name> subagents)
        ├── scripts/              # engine.*, bootstrap-deps.sh, init*, workflows.py
        ├── workflows/            # bundled workflow definitions
        └── skills/
            ├── wise/SKILL.md     # the natural-language helper
            └── wise-<name>/SKILL.md  # every action skill is a flat /wise-<name> slash command
```

(This marketplace currently hosts a single plugin, `wise` — the PRD/TRD
authors are skills *inside* it (`wise-prd-architect`, `wise-trd-architect`),
not separate plugins. The marketplace-wide conventions in §2–§3 are written
to apply to any plugin added here.)

A plugin is self-contained under `plugins/<name>/`. Nothing above that path
should be plugin-specific.

`wise` is a **standalone plugin**: it owns the workflow engine, shared
scripts, and the natural-language `/wise` helper. Every user-facing action
is a flat `/wise-*` slash command.

---

## 2. Conventions that apply to every plugin

These are marketplace-wide. They apply before any per-plugin rule.

- **Plugin and skill names** use `kebab-case`. A plugin skill is invoked as
  `/<plugin>:<skill>` — plugin skills are *always* namespaced, there is no
  way around that. Claude Code will also expose an unprefixed alias
  (`/<skill>`) when the name is unambiguous across installed plugins, but
  you cannot rely on the unprefixed form being available. Document the
  namespaced form and let the unprefixed form be a convenience.
- **Flat slash commands + a natural-language helper.** Every user-facing
  action is a flat, autocomplete-visible slash command —
  `/wise-workflow-run`, `/wise-pr-create`, etc. No dispatcher-style
  routing. The plugin additionally hosts one skill named the same as the
  plugin (`skills/wise/SKILL.md`, invoked as `/wise`) — but that skill is
  a **natural-language helper**, not a dispatcher: bare `/wise` prints the
  catalog of every `/wise-*` slash command; `/wise <free-form text>`
  classifies intent against the catalog and proposes a matching command
  via `AskUserQuestion` before invoking it through the `Skill` tool. See
  [§2.1](#21-skill-shapes) for the two skill shapes plus the helper's
  narrow remit.
- **State lives in `${CLAUDE_PLUGIN_DATA}`.** Never write to the user's
  home, the workspace, or a `.claude/` dir inside another project. Plugin
  data survives updates and is wiped on uninstall by the runtime; that's
  exactly what we want.
- **`allowed-tools` is narrowly scoped.** Grant the minimum set of tools
  and Bash patterns the skill actually needs. Broad wildcards get rejected
  in review.
- **Validate arguments before any work.** Every skill that accepts
  arguments must validate them at the top of its procedure, reject
  malformed input with a clear error, and stop. A provided-but-invalid
  argument is an error — do **not** silently "helpfully interpret" it as
  absent, do not fall through to a prompt to recover. Only a legitimately
  absent argument may fall through to a prompt or default. See [§4.1](#41-action-skill--the-common-case) for
  the shape each skill's validation step should take.
- **Hooks are opt-in and exceptional.** A plugin that's already behind
  explicit slash commands almost never needs a hook — hooks tax every
  session and make behaviour implicit. No new hook (of any event) ships
  without the discussion and constraints in [§2.4](#24-hooks). `wise`
  currently ships exactly one: the SessionEnd insights-ingest hook.
- **No lifecycle assumptions.** Claude Code has no `PostInstall`,
  `PostUpdate`, or `PreUninstall` hook. Anything that needs to run on
  first-use-after-install must be idempotent and lazy.
- **Don't commit developer-specific absolute paths** (e.g.
  `/Users/<you>/Projects/...`) to any file that ships with the plugin.
  Documentation examples are fine; actual state is not.

### 2.1 Skill shapes

Every skill in the `wise` plugin is one of two shapes. The plugin also
hosts a single third artefact — the natural-language helper — with a
narrow, well-defined remit that is not reused for any other skill.

**Standalone slash-command skills** — *the default shape for every
new action skill.* User-invocable, shown in the slash menu as
`/wise:<skill-name>` with a bare `/<skill-name>` alias when
unambiguous. The directory name on disk equals the slash command:
`plugins/wise/skills/wise-workflow-run/SKILL.md` is invoked as
`/wise-workflow-run` (bare) or `/wise:wise-workflow-run` (canonical).

- Lives at `plugins/wise/skills/<skill-name>/SKILL.md`. The
  directory name matches the frontmatter `name:` field verbatim
  and doubles as the slash command.
- Frontmatter keys: `name:`, `description:`, `argument-hint:`,
  `allowed-tools:`. `user-invocable:` stays at its default (`true`).
  No `command:` / `subcommand:` / `arguments:` — those were v1
  dispatcher-routing fields and have no meaning in v2.
- `argument-hint:` is the compact menu hint users see in the slash
  menu (e.g. `"[<workflow-name>]"`, `"[--check]"`). May be an empty
  string for skills that accept no arguments.
- The skill body self-parses `$ARGUMENTS` — the raw string Claude
  Code passes through when the user types `/<skill-name> …`. By
  convention: first whitespace-separated token is the positional,
  remainder is the tail or ignored. Simple on/off flags can be
  detected by substring; richer grammars should be documented in
  the SKILL.md body.
- If the user invokes the skill with empty `$ARGUMENTS`, the skill
  may fall through to an interactive picker / prompt. A
  provided-but-invalid argument is always an error — never silently
  re-prompt (see [§2](#2-conventions-that-apply-to-every-plugin)).

**Reference / guidance skills** — description-triggered docs. Pick
this shape when the skill is a bundle of knowledge Claude should
consult when the user's prose matches the description (e.g.
`wise-estimation` firing on "estimate this ticket"), *not* an action
the user invokes by name.

- Lives at `plugins/wise/skills/<skill-name>/SKILL.md`, same
  layout as standalone skills.
- Frontmatter keys: `name:`, `description:`, `allowed-tools:`. No
  `argument-hint:` — the absence of that key is what distinguishes
  a reference skill from a standalone action skill at catalog-emit
  time (`scripts/engine.py list-skills` buckets on this).
  `user-invocable:` stays at the default (`true`) so Claude can
  auto-consult the skill.
- Body is reference material — tables, code samples, rules of thumb
  — not numbered procedure steps. No `--copy` / `--check` style
  args because the skill isn't invoked by name.
- `description:` should be "pushy" — explicit about the keywords
  and contexts where Claude should auto-consult. Understated
  descriptions under-trigger.

**The natural-language helper** — exactly one, always named the same
as the plugin (`skills/wise/SKILL.md`, invoked as `/wise`). This is a
third category with a narrow, specific job:

- Discovers the catalog via
  `bash ${CLAUDE_PLUGIN_ROOT}/scripts/engine.sh list-skills`, which
  execs into `scripts/engine.py list-skills` and returns a JSON
  document listing every standalone and reference skill.
- When the user types `/wise` bare, renders the catalog as a
  human-readable listing.
- When the user types `/wise <free-form text>`, classifies the intent
  against the catalog (pure LLM judgement — no fuzzy library),
  proposes a matching `/wise-<skill>` command via `AskUserQuestion`,
  and invokes the pick through the `Skill` tool on confirmation.
  Never runs anything silently.
- Frontmatter: `disable-model-invocation: true`. Users invoke it
  explicitly; Claude never auto-runs it. `allowed-tools` includes
  `Skill` (to invoke the chosen action), `AskUserQuestion` (to
  confirm the pick), and a narrowly-scoped
  `Bash(${CLAUDE_PLUGIN_ROOT}/scripts/engine.sh:*)` for the
  catalog-emit.
- There is exactly one `disable-model-invocation: true` skill in the
  plugin, and it is this helper.
- See `plugins/wise/skills/wise/SKILL.md` for the reference
  implementation.

**Why this design.** The v2 flat-commands + helper split solves the
same problems the old dispatcher solved, without the per-invocation
routing cost:

1. **Autocomplete replaces routed help.** Typing `/wise-` into
   Claude Code's slash menu fans out to every action. Discovery is a
   Tab press, not a routed slash command. The `/wise` helper exists
   for users who'd rather describe what they want than remember
   command names, and for long-form intent classification.
2. **Every action is first-class.** No hidden
   `user-invocable: false` action skills. Every skill is directly
   invokable.
3. **Adding an action is a one-step operation.** Drop a skill
   directory under `skills/wise-<name>/`. The next time the `/wise`
   helper runs, `scripts/engine.py list-skills` picks it up
   automatically. No registration, no dispatcher code change.
4. **Typo recovery lives in the helper.** If a user types
   `/wise-workflow-ru` by accident, Claude Code's own fuzzy match
   over the slash menu handles it. If a user types a free-form
   request the helper can't classify, it falls back to the full
   catalog listing — no silent wrong-command execution.

**Cross-plugin name collisions.** The bare alias `/<skill-name>`
only works when no other installed plugin ships a skill of the same
name. `wise` avoids collisions by prefixing every skill with `wise-`.
The canonical namespaced form `/wise:<skill-name>` always works
regardless.

### 2.2 Bundled-tooling convention

Third-party dependencies that a `wise` skill needs to work — MCP
servers, other plugins, CLI binaries, language runtimes — are bundled
into the plugin, not left to the user. A user installing the plugin
gets everything its skills need in one step; they never paste JSON
into settings or run `brew install` for a tool a skill silently
depends on.

Three mechanisms by dependency kind:

- **Another plugin** → the plugin's
  `.claude-plugin/plugin.json` `"dependencies": [...]` array. Claude
  Code (v2.1.110+) auto-installs listed plugins transitively.
  `wise` declares `figma` and `atlassian` from
  `claude-plugins-official` with the `marketplace:` field set; the
  marketplace's `allowCrossMarketplaceDependenciesOn` key permits
  the cross-marketplace resolution. See
  https://code.claude.com/docs/en/plugin-dependencies.md.
- **MCP server** → the plugin's `.mcp.json` (currently empty).
  Claude Code auto-registers any `mcpServers` the plugin declares
  when it loads. **MCP tool ids are derived from the plugin name**
  (`mcp__plugin_<plugin>_<server>__<tool>`) — both the `.mcp.json`
  entry and the `allowed-tools` list of every consuming skill must
  stay in sync.
- **CLI / runtime / OS package** (Python, brew packages, system
  binaries) → Claude Code has no built-in installer for these. The
  plugin hosts `scripts/bootstrap-deps.sh`, which probes at run time
  and surfaces a one-shot install prompt via `AskUserQuestion` on
  missing deps.

When adding a skill with a new external dependency:

1. Pick the right mechanism above.
2. Add the entry to the plugin's file (`.mcp.json`, or `plugin.json`'s
   `dependencies:`).
3. Update the **Bundled tooling** table in `plugins/wise/README.md`
   with one row: dependency name, kind, registered-in path, and the
   skill(s) that use it.
4. If the dependency has a notable first-run cost (large download,
   permission prompt, network call), document it in the consuming
   skill's SKILL.md so users aren't surprised.
5. Bump the plugin's `plugin.json` `version` — adding bundled
   tooling is user-visible and is a minor bump.

Why this convention exists: the moment we ask a user to copy a JSON
blob into their Claude Code config to make a skill work, many of them
won't, and the skill silently under-performs. Bundling makes skills
actually work on first use.

### 2.3 Cross-marketplace dependencies

`wise` depends on the `figma` and `atlassian` plugins, which live in
the separate `claude-plugins-official` marketplace. Two things make
that resolve:

1. Each entry in `wise`'s `plugin.json` `dependencies:` array carries
   a `marketplace:` field — `{ "name": "figma", "marketplace":
   "claude-plugins-official" }`.
2. The repo's `.claude-plugin/marketplace.json` declares
   `"allowCrossMarketplaceDependenciesOn": ["claude-plugins-official"]`.
   Without that allow-list entry, Claude Code refuses to resolve a
   dependency that points at a different marketplace.

If a future skill needs a plugin from yet another marketplace, add
that marketplace to the `allowCrossMarketplaceDependenciesOn` array in
the same PR that adds the dependency.

### 2.4 Hooks

The default remains **no hooks** — a plugin behind explicit slash commands
should not also run implicit per-session code. Adding a hook is a deliberate
decision that must be argued in the PR, not slipped in. This section IS the
"discussion" the §2 bullet requires; it records the one sanctioned hook and
the bar any future one must clear.

`wise` ships exactly one hook:

- **SessionEnd → `hooks/session-end-ingest.sh`.** On session end it reads the
  `transcript_path` from the hook's stdin payload and hands that single file to
  `scripts/insights.py ingest`, which appends a compact, redacted record to the
  insights ledger (see [§5](#5-plugin-state)). This is what makes the
  `/wise-insights-mine` self-improvement loop work without the user having to
  remember to capture anything.

Hard constraints on this hook — and the bar for any hook that is ever proposed:

- **No LLM, no network.** Pure local parsing.
- **No dependency bootstrap.** `bootstrap-deps.sh` can prompt/install and must
  never run from a hook. The ingest engine is therefore **Python-stdlib-only**,
  so the hook works even before `/wise-init`.
- **Bounded work.** It touches exactly the one transcript that just ended —
  never a full scan. The expensive clustering/drafting stays in the
  `/wise-insights-mine` skill, behind an explicit invocation.
- **Never blocks or fails the session.** The script is `set +e`, swallows all
  errors, and always exits 0. A SessionEnd hook cannot block teardown and must
  not try to.
- **Idempotent.** Re-firing on the same session is a no-op (keyed on
  `session_id` + transcript mtime).
- **Bash 3.2 compatible** (macOS default) and `hooks/hooks.json` is
  auto-discovered — no `plugin.json` `hooks` field, no matcher.

`SessionStart` hooks remain disallowed outright: they tax startup and there is
no ingest-style justification for them here.

---

## 3. Adding a new plugin to the marketplace

1. Create `plugins/<name>/.claude-plugin/plugin.json` with `name`,
   `version` (start at `0.0.1`), `description`, and `author`.
2. Create `plugins/<name>/README.md` (user docs) and
   `plugins/<name>/CLAUDE.md` (agent invariants — keep it short; link here
   for procedures).
3. Add at least one skill under `plugins/<name>/skills/`.
4. Register the plugin in `.claude-plugin/marketplace.json` — add an entry
   to the `plugins` array with `name`, `source` (`./plugins/<name>`), and
   `description`.
5. Add a row to the marketplace `README.md` plugin table.
6. Smoke-test locally (see [§6](#6-local-development)).
7. Open a PR (see [§7](#7-commit-and-pr-conventions)).

---

## 4. Adding an action to a plugin

Adding a new `/wise-<action>` slash command is a single-step
operation: create one new skill directory under `plugins/wise/skills/`.
No registration, no dispatcher code change, no `scripts/engine.py`
edit. The `/wise` natural-language helper discovers the new skill on
its next catalog emit via `scripts/engine.py list-skills`.

The directory name IS the slash command. A skill at
`plugins/wise/skills/wise-workflow-run/SKILL.md` with frontmatter
`name: wise-workflow-run` is invoked as `/wise-workflow-run` (bare
alias) or `/wise:wise-workflow-run` (canonical namespaced). There is
no translation layer.

**Preferred path: the scaffold wizard.** Don't hand-author frontmatter.
Run `/wise-skills-create <skill-name> [<description>]` from a clone of
the marketplace. The wizard scaffolds the skill into
`plugins/wise/skills/<skill-name>/SKILL.md`, then delegates to Claude
Code's `skill-creator`, which asks whether the skill is a standalone
action or a reference/guidance skill and writes the file.

`/wise-skills-edit <name>` is the companion wizard for modifying an
existing skill.

The handwritten procedure below is useful as a reference for what the
wizard produces, or for one-off cases where the wizard's output
needs manual adjustment.

### 4.1 Action skill — the common case

Every action skill in v2 is a **standalone slash command** ([§2.1](#21-skill-shapes)).
The frontmatter is small, the body self-parses `$ARGUMENTS`, and
the skill is immediately visible in the Claude Code slash menu —
no hidden-action intermediate shape.

1. Create `plugins/wise/skills/<skill-name>/SKILL.md`. The
   directory name equals the frontmatter `name:` field and doubles
   as the slash command — pick the name carefully, rename commits
   are breaking.

2. Start from this frontmatter template:

   ```yaml
   ---
   name: <skill-name>                # e.g. wise-workflow-run
   description: >-
     <One user-facing sentence describing what the skill does>.
     Invoked as `/<skill-name>` (bare alias) or `/wise:<skill-name>`
     (canonical). Use when the user says <trigger words>, "<synonym>",
     or types `/<skill-name>`.
   argument-hint: "[<arg>]"          # autocomplete hint; "" when no args
   allowed-tools: Read, Write, AskUserQuestion, Bash(<narrow-pattern>:*)
   ---
   ```

   Rules:

   - **`name:` equals the directory name.** Match verbatim. Renames
     are breaking changes — flag them in the PR.
   - **`description:` first sentence is user-facing.** It shows up
     in the `/wise` helper's catalog listing and in the slash menu's
     preview. Follow the first sentence with a trigger-phrase hint
     ("Use when the user says …") so the LLM-based intent
     classifier and Claude Code's own auto-triggering both have
     something to match.
   - **`argument-hint:` is the autocomplete hint.** Keep it short —
     `"[<workflow-name>]"`, `"[--check]"`, `"[path]"`. The presence
     of this key is what marks the skill as a standalone action (as
     opposed to a reference skill); `scripts/engine.py list-skills`
     buckets on it. Use an empty string `""` for action skills that
     accept no arguments.
   - **`allowed-tools:` is narrowly scoped.** Grant only the tools
     the skill actually uses, and scope `Bash(...)` to specific
     command prefixes. Broad wildcards get rejected in review.
   - **Do NOT add** `command:` / `subcommand:` / `subcommand-aliases:` /
     `arguments:` / `user-invocable: false` — those were v1
     dispatcher-routing fields and have no meaning in v2.
     `disable-model-invocation: true` is reserved for the `/wise`
     helper alone.

3. Body structure that has worked well:
   - `## Why this skill exists` — one paragraph.
   - `## Arguments` — document each positional / flag the skill
     accepts. Names describe the raw token form (`<workflow-name>`,
     `--check`) since the skill parses `$ARGUMENTS` itself.
   - `## Procedure` — numbered, deterministic steps, **in this order**:
     1. **Parse `$ARGUMENTS`.** Read the raw string Claude Code
        passes when the user types `/<skill-name> …`. By
        convention: the first whitespace-separated token is the
        positional, the remainder is the tail (or ignored, per the
        skill's grammar). For simple on/off flags, a substring
        check is fine (`[[ "$ARGUMENTS" == *"--check"* ]]`); for
        richer grammars, document the parser in the SKILL body.
     2. **Semantic validation of provided values.** Reject
        placeholder-looking strings (`<path>`, `$VAR`, `{path}`,
        `TODO`, `FIXME`, `...`, `?`), check filesystem existence,
        verify git repo membership, etc. Provided-but-invalid is an
        **error** — do not prompt to recover. Only a legitimately
        absent argument may fall through to a later prompt step or
        an interactive picker.
     3. **The actual work.** Numbered deterministic steps.
   - `## Guardrails` — explicit "do not X". Never prompt as
     fallback for invalid input. Never invoke another action skill
     directly (the `/wise` helper is the only skill that does); if
     two actions share logic, extract it into `scripts/` (see
     [§2.1](#21-skill-shapes) invariants). The one narrow exception
     is `wise-workflow-run` / `wise-workflow-resume`, which compose
     over validated workflow YAML.

4. That's it. No engine.py or dispatcher edit needed —
   `scripts/engine.py list-skills` globs `skills/*/SKILL.md` and
   picks up the new action the next time the `/wise` helper runs.
   The slash menu fans out to the new command the moment Claude
   Code reloads its plugin cache. Only the first sentence of the
   frontmatter `description` appears in the catalog listing
   (subsequent sentences are operational / trigger hints for the
   LLM), so write the description with that in mind.

5. Update the Commands table in `plugins/wise/README.md` so humans
   reading the repo see the new slash command (the auto-discovery
   helps at runtime but is not a substitute for docs).

### 4.2 Natural-language helper — rarely touched

The plugin has exactly one `disable-model-invocation: true` skill,
`skills/wise/SKILL.md`, invoked as `/wise`. You don't create a
new helper when adding an action — the existing one discovers the
new skill automatically via `scripts/engine.py list-skills`. You
only edit the helper when:

- Changing the catalog rendering (adding a new section, reordering).
- Changing the intent-classification prompt (tightening one-match
  vs ambiguous thresholds).

`scripts/engine.py` is a catalog emitter, nothing more. Its only
supported subcommand is `list-skills`, which walks `skills/` and
emits a JSON document bucketed into `standalone`, `reference`, and
`siblings_installed` (always empty — `wise` is a single plugin).

### 4.3 For any skill

- If an existing skill has equivalent structure, copy its frontmatter
  as the starting point rather than writing from scratch.
- Run [§6.2](#62-syntax-and-structural-checks) checks before opening the PR.

---

## 5. Plugin state

`wise` keeps two kinds of persistent state, both off the project
tree:

- **Init registry** — `${CLAUDE_PLUGIN_ROOT}/.wise-init-registry.yaml`,
  written by `/wise-init` and by `scripts/bootstrap-deps.sh`. It
  caches dependency probe results (Python / Node / gh paths and
  versions) so workflow engine skills can fast-path past the full
  probe. It lives in the install dir on purpose — every
  `/plugin install wise@…` wipes it, giving natural invalidation —
  and is `.gitignore`d.
- **Workflow run state** — `~/.local/share/wise/runs/<cwd-slug>/<run-ulid>/`
  (honours `XDG_DATA_HOME`). Per-workspace by design; see
  [§9](#9-workflow-subsystem).
- **Insights store** — `~/.local/share/wise/insights/` (honours
  `XDG_DATA_HOME`; via `wise_data_root()`). Holds the self-improvement loop's
  `ledger.jsonl` (one redacted record per ingested session), `candidates.json`
  (the derived, frequency-ranked patterns), `decisions.json` (the
  promote/dismiss/retire suppression list), `skill-backups/<ts>/<name>/`
  (copies of skills retired by `/wise-insights-refine`, so a merge is always
  reversible), and `snapshots/<ts>/{index,skills}/` (restore points written by
  `/wise-insights-reset`). Written by the SessionEnd hook ([§2.4](#24-hooks)),
  `/wise-insights-mine`, `/wise-insights-refine`, and `/wise-insights-reset`.
  `/wise-insights-reset` is the **reversible** cleanup (snapshot-then-clear,
  rollback via `restore`); `scripts/insights.py purge --yes` is the separate
  **irreversible** wipe of the whole store (snapshots included).

There is **no persisted project registry**. A workflow run resolves
the project it operates on from the current context — see
`project-selection` in [`docs/wise/workflows.md`](./docs/wise/workflows.md):
`current` auto-detects the project from the current git repository,
`prompt` auto-detects then asks the user to confirm or override, and
`any` skips project resolution. The `{{project.path}}` /
`{{project.name}}` / `{{project.kind}}` template variables are
populated from that resolution, not from a stored file.

---

## 6. Local development

### 6.1 Install the plugin from a clone

```
/plugin marketplace add /Users/<you>/Projects/wise-claude
/plugin install wise@wise-claude
```

Re-running `/plugin install` after an edit picks up changes. You can pass
`--keep-data` on uninstall to preserve your workflow definitions between
installs: `/plugin uninstall wise --keep-data`.

### 6.2 Syntax and structural checks

Before opening a PR:

```bash
# JSON manifests parse
python3 -m json.tool .claude-plugin/marketplace.json > /dev/null
python3 -m json.tool plugins/wise/.claude-plugin/plugin.json > /dev/null

# Bash scripts parse
bash -n plugins/wise/scripts/*.sh

# Python scripts compile
python3 -m py_compile plugins/wise/scripts/*.py

# No stale /wise:* name references (after a rename):
grep -Rn "/wise:old-name" plugins/ docs/ README.md CONTRIBUTING.md || echo "clean"
```

### 6.3 Skill smoke tests

There is no Vitest/Codeception-style harness for skills. After
`/plugin install wise@wise-claude`, verify manually:

- `/wise-init` walks through the Python + Node + `gh` dep probes and
  writes `${CLAUDE_PLUGIN_ROOT}/.wise-init-registry.yaml`.
- **Bare `/wise`** (helper, no args) prints the full catalog of
  `/wise-*` slash commands plus reference skills.
- **`/wise <free-form>`** (helper, with text) — e.g.
  `/wise open a PR for this branch` — proposes `/wise-pr-create`
  via `AskUserQuestion` before invoking it.
- **Helper gating:** ask Claude, outside of a slash command, "can
  you run the wise helper for me?" It should decline because
  `skills/wise/SKILL.md` has `disable-model-invocation: true`.
- **Slash menu:** typing `/wise-` fans out to every action.
- `/wise-workflow-run example-workflow` runs the reference workflow
  end-to-end.

---

## 7. Commit and PR conventions

### 7.1 Commits

Conventional-commit style with plugin scope:

- `feat(wise): add /wise-tickets-create`
- `fix(wise): normalise relative paths in /wise-pr-create`
- `chore(wise): bump to 2.x.y` (patch-only, docs tweak)
- `docs: clarify backup policy in CONTRIBUTING.md`

Scope is the plugin name (`wise`) for anything inside `plugins/wise/`, or
`marketplace` / `docs` / `chore` for cross-cutting changes.

Keep commits focused. If a change has an incidental README tweak, fold it
in; if it has a genuinely unrelated refactor, split.

### 7.2 PRs

- One coherent change per PR. If in doubt, split.
- Describe *why* in the body; the diff already shows *what*.
- Mention any invariant changes (skill removals, frontmatter
  additions). Reviewers grep for these.
- Run [§6.2](#62-syntax-and-structural-checks) before requesting review.

---

## 8. Versioning

Each plugin has its own `version` in `plugin.json`. The marketplace has its
own `version` in `marketplace.json` — they are independent.

Per-plugin semver for the `wise` plugin:

- **Patch (`x.y.z`)** — bug fixes, doc changes, non-behaviour-visible tweaks.
- **Minor (`x.y.0`)** — new skills, new optional behaviour, non-breaking
  improvements. Adding bundled tooling is a minor bump.
- **Major (`x.0.0`)** — breaking changes: removed skills, renamed or
  removed skills, changed CLI invocation form.

Bump the `version` in the same PR as the change it describes. There is
no separate changelog file — the git history is the record. Feature
docs describe the *current* behaviour; don't sprinkle
`wise X.Y.Z introduced …` into the rest of the docs.

---

## 9. Workflow subsystem

The workflow subsystem lets users compose `wise`
actions, third-party skills, shell commands, and approval gates into
named multi-step procedures invoked as `/wise-workflow-run <name>`. The
main Claude Code conversation is the conductor — no backgrounded
subagent, no backend service.

User-facing reference: [`docs/wise/workflows.md`](./docs/wise/workflows.md).
This section is the **contributor** reference: schema details, the
invariant exception, and the procedure for extending or modifying the
subsystem.

### 9.1 Moving parts

```
plugins/wise/
├── AGENTS.md                     # catalog of the agent roster
├── agents/                       # plugin-level SDLC role roster (wise:<name>)
│   └── <role>.md                 # one subagent per file; consumed by `workflows.py list-agents`
├── scripts/
│   ├── bootstrap-deps.sh       # ensures python3 + pyyaml + python-ulid
│   └── workflows.py              # all YAML + state + ULID + dep-probe + roster logic
├── workflows/                    # bundled workflow definitions (shipped)
│   └── <name>/                   # folder form (preferred)
│       ├── workflow.yaml         # the definition
│       ├── templates/            # optional — addressable as {{workflow.dir}}/templates/…
│       └── prompts/              # optional — addressable as {{workflow.dir}}/prompts/…
└── skills/
    ├── wise-workflow-list/       # /wise-workflow-list
    ├── wise-workflow-create/     # /wise-workflow-create <name>    (wizard)
    ├── wise-workflow-run/        # /wise-workflow-run <name>       (conductor)
    ├── wise-workflow-resume/     # /wise-workflow-resume <ulid>
    ├── wise-workflow-status/     # /wise-workflow-status [<ulid>]
    └── wise-workflow-remove/     # /wise-workflow-remove <name>
```

Runtime-created, never committed:

- `${CLAUDE_PLUGIN_DATA}/workflows/definitions/<name>/workflow.yaml`
  — user definitions (wizard output, folder form). Legacy flat
  `<name>.yaml` is still accepted.
- `~/.local/share/wise/runs/<cwd-slug>/<run-ulid>/state.yaml` —
  run state (canonical truth). Honours `XDG_DATA_HOME`; path
  computed by `wise_runs_root_for_cwd()` in `scripts/workflows.py`.
- `~/.local/share/wise/runs/<cwd-slug>/<run-ulid>/logs/<step-id>.<step-run-ulid>.log`
  — per-step-execution log.

### 9.2 Invariants

All of the conventions in [§2](#2-conventions-that-apply-to-every-plugin) still apply. The workflow subsystem adds:

- **Workflow run state lives per-workspace**, never under
  `${CLAUDE_PLUGIN_DATA}`. Definitions go the other way — shipped
  at `${CLAUDE_PLUGIN_ROOT}/workflows/` and user-authored at
  `${CLAUDE_PLUGIN_DATA}/workflows/definitions/`. Under each root,
  a workflow can live in one of two layouts:
  `<name>/workflow.yaml` (folder form, preferred — enables sibling
  `templates/` and `prompts/` artifacts addressable via
  `{{workflow.dir}}`) or `<name>.yaml` (legacy flat form). Folder
  form wins on same-root collision.
- **Narrow exception to "action skills never invoke other action
  skills" for `wise-workflow-run` and `wise-workflow-resume` only.**
  Those two skills compose over other skills by design. Allowed:
  `Skill` calls that map to `type: skill` steps in a validated
  workflow YAML. Disallowed: any `Skill` call to `wise:wise` (the
  natural-language helper), any `Skill` call outside the validated
  DAG. Every other action skill still obeys the blanket rule.
- **All YAML + state handling lives in `scripts/workflows.py`.**
  SKILL.md bodies shell out to its subcommands; they never parse or
  emit YAML themselves. This mirrors how `engine.py` owns catalog
  emit for the `/wise` helper.
- **Python is a hard dep of the workflow subsystem.** The
  recommended install path is the `/wise-init` wizard, which
  walks the user through Python + Node + `gh` + `gh auth` and
  caches the probe results at
  `${CLAUDE_PLUGIN_ROOT}/.wise-init-registry.yaml`. Workflow engine
  skills call `scripts/init-registry.py check` first; on
  `INIT:ok` they proceed silently. On any other result they fall
  back to `scripts/bootstrap-deps.sh`. Non-engine skills (`wise-pr-*`)
  don't run either probe — they just invoke their underlying
  command and fail naturally if deps are missing.

### 9.3 Definition schema (v1)

See [`docs/wise/workflows.md`](./docs/wise/workflows.md) for the complete
user-facing reference. Contributor-side invariants:

- **Top-level `version:` is always a plain integer on its own line.**
  Reserved for a future migration path.
- **`name:` is kebab-case** (`^[a-z][a-z0-9]*(-[a-z0-9]+)*$`) and
  matches the filename. Not one of the reserved verbs
  `list|add|run|resume|remove|status`.
- **`steps[]` is a list; ids are unique.** `depends_on` entries must
  reference earlier or peer step ids — forward references are
  illegal. The script will reject cycles.
- **Templating is literal replacement.** `{{project.path}}`,
  `{{project.name}}`, `{{project.kind}}`, and any named `outputs`
  from earlier steps. No expression language. `when:` supports one
  trivial form: `name == 'literal'` / `name != 'literal'`.
- **`preflight:` is optional and omitted when empty.** The block
  pins any or all of the three pre-flight answers (`control-mode`,
  `worktree`, `rename`) so the runner isn't offered choices that
  don't make sense for the workflow. Default value for each key is
  `prompt` (ask the runner). Invalid values on any key fall back to
  `prompt` with a `WARN:` line from `workflows.py get-preflight`.
  Valid enum values per key are tracked in the `PREFLIGHT_KEYS` map
  in `scripts/workflows.py`.
- **Agent binding is `prompt`-only and passes through untouched.** The
  workflow-level `agents: off|auto` policy and the step-level `agent:` /
  `model:` / `effort:` fields bind only to `type: prompt` steps. The
  engine does not whitelist step keys — `_render_step` renders the whole
  step dict, so these fields reach the conductor with no schema change;
  the dispatch logic lives entirely in the `wise-workflow-run` SKILL body.
  `agent:` is **scalar OR a list**: a scalar is a single role / `auto` /
  `off`; a list is a **team** (each item a bare role or
  `{role, lead?, model?, effort?}`) dispatched together and
  **conductor-synthesized** into one step result, with an optional single
  `lead` integrating peers' drafts first. The conductor normalizes `agent:`
  through `workflows.py resolve-team` (it folds in per-member model
  resolution and validates roles + the at-most-one-lead rule). A team step is
  **atomic** — a resume mid-team re-runs it whole, so no new run state is
  added. All step execution is **in-conversation** (`Task` subagents,
  subscription-covered — no headless subprocess backend, which would bill as
  separate API usage). `model:` is a native Task per-call override (the real
  per-step knob); `effort:` is NOT a native per-call knob, so the conductor
  conveys it as a prompt directive only (best-effort). See
  [§9.10](#910-the-agent-roster).

### 9.4 `workflows.py` subcommand contract

The script is the sole source of truth for YAML parsing, state
mutation, and DAG evaluation. When modifying it, keep the subcommands
stable — SKILL bodies reference them by exact token:

```
locate-def <name>                        # prints abs path; exit 1 if not found
probe-requires <def>                     # OK or MISSING: lines; exit 2 on missing
new-ulid                                 # stdout: one ULID
init-state <def> <run-dir> <run-id> <ctx-json>
                                         # writes state.yaml; stdout: its path
next-wave <def> <state>                  # JSON { runnable, to_skip, terminal? }
update-step <state> <step-id> key=val... # mutate one step
update-run  <state>            key=val...# mutate top-level
record-output <state> <name> <value>     # capture into outputs map
reset-running <state>                    # running → pending (resume preamble)
list-runs <runs-root>                    # summary table
dump-state <state>                       # pretty-print YAML
render <template> <state>                # expand {{…}} literally
list-agents                              # JSON of the agents/ roster (auto-select + wizard)
resolve-model <pinned> [effort]          # JSON {model,effort,fell_back,reason,next_fallback}
resolve-team <def> <step-id>             # JSON {mode,lead,members,errors} — normalize a step's agent: into a model-resolved team
```

Breaking any of these is a major-version event (CLI contract change) — see
[§8](#8-versioning).

### 9.5 Adding a step type

1. Extend the definition schema in `docs/wise/workflows.md` with a
   clear spec: which fields are required, success semantics, failure
   modes, captured output.
2. Teach `workflows.py next-wave` to include the new type's
   type-specific fields in the rendered descriptor.
3. Teach the `wise-workflow-run` SKILL body how to dispatch the new
   type — which tool to invoke, how to collect, how to score success.
4. Add a step of the new type to the `example-workflow` bundled
   workflow (`plugins/wise/workflows/example-workflow/workflow.yaml`)
   so the type is exercised in smoke tests.
5. Bump the plugin's `version` per [§8](#8-versioning). New step types are a minor bump.

### 9.6 Adding or editing a bundled workflow

**For ADDING a workflow:**

1. Create `plugins/wise/workflows/<name>/workflow.yaml` — the folder
   form is the default for all new bundled workflows. Sibling
   `templates/` and `prompts/` directories are optional and
   addressable from steps via `{{workflow.dir}}`.
2. **Ship a `README.md` alongside `workflow.yaml`.** Every
   bundled workflow has one, following the consistent shape the
   existing workflows use: title + summary → When to use → When
   not to use → Prerequisites → Flow (mermaid flowchart) →
   Steps table → Inputs → Outputs → Examples → Related. The
   `/wise-workflow-create` wizard generates a scaffolded README
   automatically; for hand-authored workflows, copy the shape from
   one of the existing bundled workflow READMEs
   (`plugins/wise/workflows/*/README.md`). Link the new workflow
   from the "Bundled workflows" table in `plugins/wise/README.md`
   in the same PR.

**For EDITING an existing workflow** (changing `workflow.yaml`
or any `prompts/*.md`): also update the workflow's `README.md` in the
same PR. The Flow mermaid, Steps table, Inputs/Outputs tables, and
Related-links section must reflect the new shape. A stale README is
worse than none — readers trust it. The invariant is codified in
[`plugins/wise/CLAUDE.md`](./plugins/wise/CLAUDE.md)'s Invariants
section.

After either: verify with `python3 scripts/workflows.py locate-def
<name>`, confirm any declared `requires:` plugins are resolvable, and
take a minor version bump.

### 9.7 Testing a workflow change locally

```bash
# 1. Syntax + compile
bash -n plugins/wise/scripts/bootstrap-deps.sh
python3 -m py_compile plugins/wise/scripts/workflows.py
python3 -m json.tool plugins/wise/.claude-plugin/plugin.json > /dev/null

# 2. Bootstrap (installs deps if missing)
bash plugins/wise/scripts/bootstrap-deps.sh

# 3. Drive the script directly (no Claude Code needed).
# `locate-def` abstracts over both layouts (folder form
# `<name>/workflow.yaml` and legacy flat `<name>.yaml`), so always
# feed its output into `probe-requires` rather than hard-coding a path.
python3 plugins/wise/scripts/workflows.py new-ulid
DEF="$(python3 plugins/wise/scripts/workflows.py locate-def example-workflow)"
python3 plugins/wise/scripts/workflows.py probe-requires "$DEF"

# 4. End-to-end via Claude Code
/plugin uninstall wise --keep-data
/plugin install wise@wise-claude
/reload-plugins
/wise-workflow-run example-workflow
```

### 9.8 Adding a required dep to `/wise-init`

When a `wise` skill acquires a new CLI / runtime dependency that's
worth caching up-front (rare — most new deps belong either in
`.mcp.json` or `plugin.json`'s `dependencies:` array), the
three-step procedure is:

1. **Add a probe to `plugins/wise/scripts/init.sh`.** New subcommand
   `probe-<name>` following the `probe-python` / `probe-node` /
   `probe-gh` pattern. Must emit `STATUS=ok|missing`, `BINARY=`,
   `VERSION=`, plus any dep-specific fields. No Python dependency in
   this script — it runs before Python is confirmed.
2. **Update `plugins/wise/skills/wise-init/SKILL.md`** to walk the
   user through the new dep in its usual order. Include installer
   options with concrete commands to paste; don't run installers
   from the wizard.
3. **If the new dep is hard-required by the workflow engine**, add it
   to `REQUIRED_DEPS_FAST_PATH` in
   `plugins/wise/scripts/init-registry.py`. If it's only needed by
   specific skills or workflow steps, skip this.

Bump the plugin's `version` (minor — new runtime requirement is a
user-visible change).

### 9.9 Explicitly deferred

Listed so proposals land in the right version:

- **`TeamCreate`-based long-lived agents** for step-level streaming
  progress and multi-turn coordination.
- **`Monitor`-based live state tailing** from a secondary subagent.
- **Workflow-definition schema migrations** — the path is designed,
  not yet built.
- **Cross-workflow composition** (workflow as a step type).
- **Worktree cleanup (`/wise-workflow-gc`)** by age or count.
- **Declarative retry / backoff on step failure.** A failed step is
  currently terminal.

### 9.10 The agent roster

`plugins/wise/agents/*.md` is a plugin-level roster of SDLC role
subagents (catalogued in `plugins/wise/AGENTS.md`). They are real Claude
Code plugin subagents — auto-discovered on install, invocable as
`subagent_type: wise:<name>` — that the workflow engine dispatches
`prompt` steps to.

**To add or edit a role:**

1. Add/edit `plugins/wise/agents/<role>.md`. Match the shape of the
   existing files: frontmatter limited to `name` (= filename stem),
   `description` (concrete enough to drive `agent: auto` routing),
   `tools` (scoped to the role), `model: inherit`, `effort` (the role's
   default reasoning level), `color`. Plugin subagents **ignore**
   `hooks` / `mcpServers` / `permissionMode` — never add them. Then the
   role's system prompt as the body.
2. Add/update the role's row in `plugins/wise/AGENTS.md` AND the repo-root
   `AGENTS.md` table — the "When `auto` picks it" cell is the routing hint
   the conductor reads.
3. Verify it parses: `python3 plugins/wise/scripts/workflows.py
   list-agents` should list it with the right `model` / `effort`.
4. Minor version bump (new roles are additive).

`plugins/wise/agents/*.md` is the single canonical source; the repo-root
`AGENTS.md` and `plugins/wise/AGENTS.md` are *project-instructions* docs
(not loadable registries) whose roster tables mirror it — keep them in
sync the same way workflow READMEs track their YAML.

The `agent:` / `model:` / `effort:` step fields and the `agents:` workflow
policy that bind to this roster are documented in
[§9.3](#93-definition-schema-v1) and `docs/wise/workflows.md`.

---

## Things to avoid (cumulative, learned from real mistakes)

- Adding a `SessionStart` hook to "run something automatically on every
  session." Every added hook is a permission-prompt tax and a source of
  "why did Claude do that?" reports.
- Storing plugin state anywhere other than `${CLAUDE_PLUGIN_DATA}` (or
  the two documented exceptions — the init registry and per-workspace
  workflow run state). The rationale is in `plugins/wise/CLAUDE.md` —
  read it before proposing a change here.
- Letting a `/wise:*` skill invoke another `/wise:*` skill directly.
  Skills are user-facing entry points. Extract shared logic into
  `scripts/` and have both skills call it.
- Broadening `allowed-tools` to make a skill "just work." Every permission
  is a prompt the user will see — be precise.
