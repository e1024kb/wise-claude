---
name: wise-skills-create
description: >-
  Scaffold a new skill inside the wise plugin by delegating to Claude
  Code's skill-creator skill. The new skill lands in
  `plugins/wise/skills/<name>/`. The plugin hosts two skill shapes:
  standalone slash-command skills and reference/guidance skills. Any
  free-form tail the user types after the skill name is forwarded to
  skill-creator as additional intent. Only runnable from inside a
  checkout of the wise-claude marketplace repo. Invoked as
  `/wise-skills-create` (bare alias) or `/wise:wise-skills-create`
  (canonical). Use when the user says "create a skill", "scaffold a
  skill", "new skill", "add a skill", or types `/wise-skills-create`.
argument-hint: "<skill-name> [<...description>]"
allowed-tools: Read, Skill, Bash(test:*), Bash(git:*), Bash(pwd:*), Bash(cat:*), Bash(grep:*)
---

# /wise-skills-create — scaffold a new wise plugin skill

## Why this skill exists

The wise plugin hosts two skill shapes:

1. **Standalone slash-command skills** (the default shape) —
   user-invocable, shown in the slash menu as `/wise:<skill-name>`
   with a bare `/<skill-name>` alias when unambiguous.
   Self-parses its args from the `$ARGUMENTS` string Claude Code
   passes through. Examples: `/wise-commit-message`,
   `/wise-workflow-run`, `/wise-pr-create`.
2. **Reference / guidance skills** — description-triggered docs
   that Claude auto-consults when the user's natural language
   matches the skill's `description:` field. No `argument-hint:`
   in frontmatter; body is reference content, not action logic.
   Example: `wise-estimation`.

Rather than ask every contributor to memorise the frontmatter
conventions for the two shapes, this skill delegates the actual
authoring to Claude Code's `skill-creator` skill, handing it a
briefing pre-scoped to `plugins/wise/`, plus any free-form intent the
user typed on the invocation line.

This skill is only runnable from inside a checkout of the
`wise-claude` marketplace repo — its sole purpose is to add skills
to the wise plugin source tree.

## Arguments

Read `$ARGUMENTS`. The first whitespace-separated token is the
`<skill-name>`; the remaining tokens are the freeform description
tail. When `$ARGUMENTS` is empty, stop with an error pointing at the
expected form (`/wise-skills-create <skill-name> [<...description>]`).

- `skill-name` (string, required) — the kebab-case identifier of the
  new skill. Becomes the `name:` value in frontmatter and the
  directory name under `plugins/wise/skills/`. By convention wise
  skills are prefixed `wise-`.
- `PROMPT` (rest, optional) — everything the user typed after
  `<skill-name>`, joined by single spaces. Forwarded verbatim to
  `skill-creator` so the wizard can skip clarifying questions the user
  has already answered in prose.

### Example invocations

```
/wise-skills-create wise-lint-format
/wise-skills-create wise-release  wrapper around `gh release create` that drafts a release body
  from the branch's commits since the last tag — standalone slash command.
```

## Procedure

### 1. Parse the arguments

Read `$ARGUMENTS` directly. The first whitespace-separated token is
`skill-name`; everything that follows (if anything) is the
free-form `PROMPT` tail. Trim whitespace on both.

### 2. Semantic validation

Reject malformed `skill-name` before doing any other work:

- Placeholder-looking strings: angle-bracketed (`<skill-name>`), shell
  variable (`$X`, `${X}`), curly-bracketed (`{skill-name}`), literal
  `TODO` / `FIXME` / `...` / `?`.
- Not kebab-case: contains spaces, uppercase, leading/trailing
  dashes, or characters outside `[a-z0-9-]`.

On any failure, emit a one-line error and stop:

```
"<value>" is not a valid skill name. Use kebab-case (lowercase
letters, digits, hyphens). Examples:
  /wise-skills-create wise-lint-format
  /wise-skills-create wise-release
```

The `PROMPT` tail is free-form and not validated — it's passed through
as user intent.

Note: we do **not** check for name conflicts here. Conflict resolution
is the wizard's job.

### 3. Marketplace-repo guard

Find the git working-tree root:

```bash
git rev-parse --show-toplevel
```

If the command fails (not inside a git repo) or the resolved root's
`.claude-plugin/marketplace.json` is missing or does not declare
`"name": "wise-claude"`, refuse:

```
/wise-skills-create must be run from inside a checkout of
github.com/e1024kb/wise-claude. Current cwd is not a marketplace
repo (no .claude-plugin/marketplace.json with
name: wise-claude was found in any ancestor directory).

Clone the marketplace repo, cd into it, then re-run:
  git clone git@github.com:e1024kb/wise-claude.git
  cd wise-claude
  /wise-skills-create <skill-name> [<description>]
```

The check uses `grep '"name":[[:space:]]*"wise-claude"'` on
the JSON — a bit coarse but robust for our format. If the format ever
changes, update this skill to match.

Let `REPO_ROOT` = the resolved git toplevel and
`WISE_PLUGIN_ROOT` = `$REPO_ROOT/plugins/wise`.

Confirm the wise plugin directory exists:

```bash
test -f "$WISE_PLUGIN_ROOT/.claude-plugin/plugin.json"
```

If it doesn't, the checkout is broken — stop with a one-line error
naming the missing path.

### 4. Invoke skill-creator with the plugin briefing

Call `skill-creator` via the `Skill` tool, passing a briefing that
tells it exactly what to build, where, and under which conventions.
Splice the user's `PROMPT` value (if any) into the briefing so the
wizard has the user's intent in hand from the start.

```
Skill({
  skill: "skill-creator:skill-creator",
  args: <BRIEFING below>
})
```

**Briefing template** (substitute the bracketed values with the
resolved absolute paths, the user's `skill-name`, and the user's
`{PROMPT}` text):

```
Create a new skill inside the wise plugin of the wise-claude
marketplace.

WISE PLUGIN ROOT (all file creation happens inside this tree):
  [WISE_PLUGIN_ROOT]

TARGET SKILL DIRECTORY:
  [WISE_PLUGIN_ROOT]/skills/[skill-name]/SKILL.md

CONVENTIONS THE NEW SKILL MUST FOLLOW:
  - [REPO_ROOT]/CONTRIBUTING.md §2.1   (standalone + reference-skill shapes)
  - [WISE_PLUGIN_ROOT]/CLAUDE.md       (plugin-specific invariants)
  - [WISE_PLUGIN_ROOT]/README.md       (plugin's user-facing surface)
  - Reference standalone slash-command skills:
      [WISE_PLUGIN_ROOT]/skills/wise-commit-message/SKILL.md     (no-arg)
      [WISE_PLUGIN_ROOT]/skills/wise-workflow-status/SKILL.md    (optional positional + listing fallback)
  - Reference guidance skill:
      [WISE_PLUGIN_ROOT]/skills/wise-estimation/SKILL.md         (description-triggered doc)

USER-PROVIDED SKILL NAME: [skill-name]

USER-PROVIDED INTENT (free-form; may be empty):
  {PROMPT}

The intent above is authoritative where it overlaps with a wizard
question — skip that question and confirm the implied answer
instead of re-asking.

WIZARD — ask the user in order (skip any question the intent already
answers):

  1. Which shape fits this skill? Default: **(a) standalone slash
     command**, unless the user's intent clearly matches (b).

     (a) Standalone slash command — user-invocable, shown in the
         slash menu as `/wise:<skill-name>` (with a bare
         `/<skill-name>` alias when unambiguous). Self-parses its
         args from `$ARGUMENTS`. Use for anything the user types as a
         slash command.
     (b) Reference / guidance skill — auto-triggered by keywords in
         the user's natural language, ships a piece of domain
         knowledge (coding conventions, a design system, a reference
         scale) that Claude pulls into context when relevant. No
         slash-command invocation form — Claude picks it up from
         prose alone.

  2a. If (a) standalone slash command:
      • One-sentence description — include triggering keywords
        (`/<skill-name>`, and plain-language cues a user might
        type). Reference-style "pushy" phrasing helps Claude pick
        the skill up on prose requests too. End with a "Use when the
        user says …" hint listing likely trigger phrases.
      • `argument-hint` — a compact string shown next to the slash
        menu entry (e.g. `[--copy]`, `[<path>]`, `<name> [--flag]`).
      • Body: Why this skill exists / Arguments (how the skill's own
        logic parses its `$ARGUMENTS` string) / Procedure / Guardrails.
      • Frontmatter MUST NOT set `command:` or `subcommand:` (those
        fields are leftovers from v1 and no longer have meaning).
      • Frontmatter MUST NOT set `user-invocable: false` (default
        is true; this shape depends on slash-menu visibility).

  2b. If (b) reference / guidance skill:
      • Trigger description — a pushy sentence naming the keywords
        and contexts where Claude should auto-consult the skill.
        See `wise-estimation` for a reference description.
      • Body: the actual reference content (patterns, code
        examples, tables, etc.). No `## Procedure` requirement —
        this isn't action logic.
      • Frontmatter MUST NOT set `command:`, `subcommand:`, or
        `argument-hint:`.
      • Frontmatter MUST NOT set `user-invocable: false` (default is
        true; reference skills need to be invocable to
        auto-trigger).
      • `allowed-tools` stays narrow — only what the body actually
        uses (often just Read, sometimes none).

FRONTMATTER CHECKLISTS

  Standalone slash command:
    - name: <skill-name>
    - description: >-  <one paragraph, first sentence user-facing
      with triggering keywords + "Use when …" hint>
    - argument-hint: "[args]"
    - allowed-tools: <narrow set>
    - (NO command/subcommand/arguments/user-invocable fields)

  Reference / guidance:
    - name: <skill-name>
    - description: >-  <pushy sentence; lists triggering keywords>
    - allowed-tools: <narrow set or omit>
    - (NO command/subcommand/argument-hint/arguments/user-invocable
       fields)

AFTER WRITING THE FILE, tell the user to reload the wise plugin
so the new skill is picked up. Show them these commands verbatim as a
copy-pasteable block:

    /plugin uninstall wise --keep-data
    /plugin install wise@wise-claude
    /reload-plugins

For a standalone slash command, the user can then invoke
`/wise:<skill-name> [args]` (or `/<skill-name>` when unambiguous).
For a reference skill, it auto-triggers when the user's prose matches
the description.
```

### 5. Relay skill-creator's output

`skill-creator` handles the wizard and the file write. Relay its
output to the user verbatim. Do not add post-processing.

## Guardrails

- Do **not** write or edit files yourself. `skill-creator` owns the
  file I/O.
- Do **not** invoke any wise action skill.
- Do **not** proceed past step 3 if the marketplace-repo guard
  failed. The whole point of that check is to prevent accidental
  skill creation in unrelated cwds.
- Do **not** create or edit anything outside `plugins/wise/` — this
  skill extends the wise plugin only. For unrelated plugins, the user
  should invoke `/skill-creator` directly.
- Do **not** attempt to reload the plugin for the user. Only print
  the reload instructions. Reloading requires slash commands that
  bash cannot invoke.
- Do **not** drop the `{PROMPT}` intent. It's the user's own
  description of what they want — handing it to the wizard avoids
  asking them to repeat themselves.
