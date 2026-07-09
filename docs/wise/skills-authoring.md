# Authoring wise skills

Every skill in the `wise` plugin lands in one of **two shapes**:

- **Standalone slash-command skills** — the default. Every action
  (every `/wise-*`) is this shape. The skill's directory name IS the
  slash command: `skills/wise-workflow-run/SKILL.md` is invocable as
  `/wise-workflow-run` (bare alias) or `/wise:wise-workflow-run`
  (canonical). The frontmatter has an `argument-hint:` field; the
  body parses `$ARGUMENTS` and does the action's work.
- **Reference / guidance skills** — description-triggered docs
  (e.g. `wise-estimation`). No `argument-hint:`. Claude auto-consults
  them when the user's prose matches the frontmatter `description:`.
  The body is reference content, not action logic.

The discriminator is the presence or absence of `argument-hint:` —
the `scripts/engine.py list-skills` catalog emitter buckets skills
on exactly that field when rendering the `/wise` helper's catalog.
Don't mix shapes: a frontmatter with `argument-hint:` implies
user-invocable; without it implies description-triggered. See
[`dispatcher.md`](./dispatcher.md) for the catalog protocol.

`/wise-skills-create <skill-name>` and `/wise-skills-edit <skill-name>`
are meta-commands that let you scaffold or modify a skill from
within Claude Code itself, delegating to Claude's built-in
`skill-creator` wizard with wise conventions injected as guardrails.
Both only run from inside the `wise-claude` marketplace repo —
writing to a plugin tree from an arbitrary workspace is a footgun we
don't want.

## Standalone slash-command skills

This is the default shape for every new action. The directory name
is the slash command — a skill in `harnesses/claude/wise/skills/wise-workflow-run/`
invokes as `/wise-workflow-run`.

### Frontmatter template

```yaml
---
name: wise-my-action
description: >-
  One-paragraph description of what this skill does, who it's for,
  and when the user would want to invoke it. Starts with a verb.
  Mentions the bare-alias and canonical invocation forms
  (`/wise-my-action` / `/wise:wise-my-action`). Ends with trigger
  phrases — "Use when the user says 'do X', 'do Y', or types
  `/wise-my-action`" — so Claude's description-based routing picks it
  up even when the user doesn't type the slash form.
argument-hint: "[<positional>]"
allowed-tools: Read, Bash(bash:*), AskUserQuestion
---
```

Field-by-field:

- **`name:`** — kebab-case. Must match the directory name. Becomes
  the slash command: `name: wise-my-action` → `/wise-my-action`.
- **`description:`** — rendered in the `/wise` helper's catalog and
  consulted by Claude when the user's prose matches. Write it for
  *semantic* match, not lexical: name the action, the trigger
  phrases, the invocation form. First sentence is the display
  summary in the catalog.
- **`argument-hint:`** — the autocomplete hint Claude Code shows
  when the user types `/wise-my-action `. Use `[<optional>]` for
  optional positionals, `<required>` for required ones, empty
  string `""` for skills that take no arguments. **This field's
  presence** is the discriminator — it marks the skill as
  standalone rather than reference.
- **`allowed-tools:`** — narrowly scoped. List exactly the tools the
  skill's procedure needs. Expanding this should be a deliberate
  decision, not an incidental fix-up.

No `command:`, `subcommand:`, `user-invocable: false`, or
`arguments:` fields. Those were v1 dispatcher-routing fields with no
meaning in v2; the `/wise-skills-create` wizard no longer offers them,
and `/wise-skills-edit` refuses to preserve them if found.

### `$ARGUMENTS` parsing idiom

The skill body receives `$ARGUMENTS` as a raw string — whatever the
user typed after the command name, unparsed. The conventional
parsing shape is:

```bash
# In the skill's procedure:
# 1. Strip leading/trailing whitespace.
# 2. First whitespace-separated token is the positional (if any).
# 3. Remainder is the free-form tail, or ignored if the skill
#    doesn't take one.
```

A skill that takes one optional positional and ignores the rest:

```
## Procedure

### 1. Parse the argument

Read `$ARGUMENTS`. The first whitespace-separated token is the
target name. If empty, drop into the interactive picker (step 2);
otherwise skip to step 3 and use the token as-is.
```

A skill that takes no arguments:

```
## Arguments

This skill takes no arguments. Ignore anything the user types beyond
the skill name.
```

Skills that need an interactive picker when args are empty are the
norm — `/wise-workflow-run` with Enter drops into a picker. That
behaviour is unchanged from v1; the difference is only in *how* the
skill receives its input (raw `$ARGUMENTS` string, not a pre-parsed
`key: value` payload from a dispatcher).

### `argument-hint` conventions

- Empty string `""` — skill takes no arguments.
- `"<required>"` — one required positional.
- `"[<optional>]"` — one optional positional.
- `"[<flag>]"` — one optional flag (e.g. `--check`, `--copy`).
- `"[<positional>] [<flag>]"` — combined.

Keep hints short; the slash menu truncates them. The full usage
contract lives in the skill body's Arguments section.

### Example skill structure

A standalone action skill typically has this shape:

```
harnesses/claude/wise/skills/wise-my-action/
└── SKILL.md       # frontmatter + procedure
```

Occasionally a skill ships sibling artifacts (prompt fragments,
templates) addressed via relative paths — but the common case is
one `SKILL.md` per skill. The `SKILL.md` body follows the
convention:

- `# /wise-my-action — one-line purpose` (H1 heading)
- `## Why this skill exists` — a paragraph or two on the design
  rationale.
- `## Arguments` — the input contract.
- `## Procedure` — numbered steps the skill follows.
- `## Guardrails` — explicit rules about what the skill must not
  do (write outside its scope, call other action skills, spawn
  subagents for trivial work).

See any existing standalone skill under `harnesses/claude/wise/skills/` for a
canonical example (`wise-commit-message` is small and focused;
`wise-workflow-run` is large and illustrates the conductor pattern).

## Reference / guidance skills

Reference skills are docs that Claude auto-consults when the user's
prose matches. They don't have a slash-command invocation form
(though Claude Code exposes them internally as `/wise:<skill-name>`
for debugging) — users never type them directly.

### Frontmatter template

```yaml
---
name: wise-my-reference
description: >-
  Reference for <topic>. Covers <subtopics>. Use whenever the user
  says "<trigger phrase>", "<another phrase>", or is doing <task> —
  even when they don't explicitly name this skill.
allowed-tools: Read
---
```

Field-by-field:

- **`name:`** — kebab-case, matches the directory name.
- **`description:`** — the matching surface. Claude reads this
  against the user's prose and auto-invokes the skill when the fit
  is strong. Write it as a long list of trigger phrases + topic
  keywords. The more specific your trigger phrases, the fewer
  false-positives you get in unrelated conversations.
- **NO `argument-hint:`** — that field's absence is what marks this
  as a reference skill in the catalog. A reference skill with
  `argument-hint:` set would be miscategorised as standalone.
- **`allowed-tools:`** — usually just `Read` for pure reference.
  Skills that include small executable checks list those too.

### Body

Reference skills' bodies are **reference content**, not action
logic. They document patterns, list rules, show examples, explain
trade-offs. They don't have a numbered `## Procedure` block because
there's nothing to execute — Claude reads the content and uses it
to inform whatever work is actually happening in the main
conversation.

See `harnesses/claude/wise/skills/wise-estimation/SKILL.md` for a compact
reference skill (the Fibonacci story-point scale).

## `/wise-skills-create <skill-name>`

Scaffolds a new skill under `harnesses/claude/wise/skills/<skill-name>/`. By
convention wise skills are prefixed `wise-`.

The wizard asks for the skill shape (standalone vs reference), the
description (with a nudge toward including trigger phrases), and —
for standalone skills — the `argument-hint` string. Any free-form
tail the user typed after the skill name is forwarded to
`skill-creator` as additional intent.

Only runnable from inside a checkout of the `wise-claude`
marketplace repo. Running it from an arbitrary workspace fails
cleanly — this skill mutates the plugin source tree; it's not meant
to run against someone else's.

After creation the new skill is auto-discoverable. The `/wise`
helper's next catalog refresh picks it up via
`engine.py list-skills`; autocomplete picks it up as soon as the
plugin is reloaded.

## `/wise-skills-edit <skill-name>`

Opens an existing skill for modification through the same wizard.
Locates the skill under `harnesses/claude/wise/skills/`.

Refuses to edit the `wise` helper itself — the helper's shape is
load-bearing for the whole plugin; use a code editor for its
changes. Works for every other skill shape.

Only runnable from inside a checkout of the `wise-claude`
marketplace repo.

## Why delegate to `skill-creator`?

`skill-creator` is already the canonical Claude Code skill-authoring
tool. It knows the `SKILL.md` conventions, the frontmatter rules,
and can be kept up to date centrally. Wrapping it with a wise
wrapper means:

- Skill authors get the full wizard experience.
- wise-specific invariants (two-shape discipline, narrow
  `allowed-tools`) are injected into the wizard's context so the
  generated skill follows our conventions out of the box.
- We don't duplicate the wizard itself — if Claude Code improves
  `skill-creator`, our skills benefit automatically.

## For workflow *definitions*, use `/wise-workflow-create` instead

Workflow definitions are YAML, not skills — `/wise-workflow-create`
doesn't delegate to `skill-creator`. See
[`workflows.md`](./workflows.md) for the workflow author guide.

## Porting a new skill to the other harnesses

A skill authored under `harnesses/claude/wise/skills/` reaches **only**
Claude Code — it does **not** appear on Codex / Cursor / Hermes until
you tell the port generator about it. The port skill trees are
**generated** by `scripts/build_ports.py` from the Claude skill plus
the inputs under `core/ports/` — never hand-edit a generated
`harnesses/{codex,cursor,hermes}/wise/skills/` file. To port a new
skill, decide its tier and register it:

- **Full** — pure prose + `git` / `gh`, no Claude-only tools. Add the
  skill to the full-tier list in each `core/ports/profiles/<harness>.yaml`.
  The generator reduces the frontmatter to the profile's keep-list,
  rewrites paths per the context-dependent rule in `CONTRIBUTING.md`
  §10.3 (executable bash contexts get the defaulted
  `${WISE_PLUGIN_ROOT:-…/harness/<h>}` expansion, prose references the
  short `${WISE_PLUGIN_ROOT}`), and injects the shared-file-resolution
  blockquote under the H1 from `core/ports/notes/_preamble.md`.
- **Adapted** — uses `Task` / `AskUserQuestion` / the `Skill` tool /
  `TodoWrite`. Add it to the adapted-tier list instead, and write a
  *Harness adaptation note* template at `core/ports/notes/<skill>.md`
  (with `{{harness_name}}` / `{{harness_id}}` placeholders; a
  `notes/<skill>.<harness>.md` override wins when one harness genuinely
  diverges). For prose that must differ per port beyond the standard
  pipeline, add find/replace hunks at
  `core/ports/overlays/<harness>/<skill>.md`.
- **Claude-only** — depends on the SessionEnd hook, Claude transcripts, or
  `skill-creator`. Exclude it from the port profiles and record why.

Then run `python3 scripts/build_ports.py` (or `just build`) and commit
the generated port skills together with the source change — CI runs
`build_ports.py --check` and fails on any drift. Record the tier in
[`docs/compatibility.md`](../compatibility.md); the full procedure is in
[`CONTRIBUTING.md` §10](../../CONTRIBUTING.md#10-cross-harness-ports--the-port-generator).
Any shared reference the skill reads belongs in `core/references/` first.

## Full procedure

See [`CONTRIBUTING.md`](../../CONTRIBUTING.md)
[§4](../../CONTRIBUTING.md#4-adding-an-action-to-a-plugin) for the
complete procedure when adding a new action skill, including the
frontmatter template, the two-shape discipline, and the naming rules
the plugin enforces.
