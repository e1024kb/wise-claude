---
name: wise
description: >-
  wise copilot — natural-language entry point for the whole wise plugin.
  With no argument, prints the catalog of available `/wise-*` commands
  plus the reference skills; with free-text (e.g. `/wise open a PR`,
  `/wise draft a commit message`), classifies the request against the
  catalog and offers to run the matching command. Use when the user types
  `/wise` bare (for the help listing) or `/wise <free-form description>`
  (to find the right command).
---

## Harness adaptation note

This skill was authored for Claude Code and adapted for OpenAI Codex CLI. Where the steps below reference Claude-specific tools, substitute:

- **AskUserQuestion** — ask the user the same question in plain chat and wait for their reply.
- **Skill tool (`/wise-*`)** — open and follow the named skill's `SKILL.md` directly.
- **Shared files (`${WISE_PLUGIN_ROOT}`)** — defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/codex`, where `./install.sh codex` puts this pack; export `WISE_PLUGIN_ROOT` only to override.


# /wise — wise copilot

## Why this skill exists

The wise plugin ships 15+ slash commands plus reference skills that
Claude auto-consults. No one remembers every name. `/wise` is the
human-friendly entry point:

- **Bare `/wise`** — print the catalog so the user can see everything
  the plugin adds to their Claude Code install.
- **`/wise <free-form text>`** — "I want to X" style requests.
  Classify the intent against the catalog, propose the best-matching
  slash command, offer to run it on the user's behalf. Fall back to
  the help listing if nothing matches clearly.

This is not a router — autocomplete already does that job, and
every action skill is invokable directly (`/wise-workflow-run`,
`/wise-pr-create`, `/wise-commit-message`, …). `/wise` exists for
discovery and for users who prefer to describe what they want instead
of remembering exact command names.

## Procedure

### 1. Load the catalog

Run once per invocation:

```bash
bash "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/codex}/scripts/engine.sh" list-skills
```

On success, the script emits a JSON document with every skill in
the plugin, bucketed by shape:

```json
{
  "standalone": [
    {
      "name": "wise-workflow-run",
      "plugin": "wise",
      "argument-hint": "[<workflow-name>]",
      "description": "Start a new run of a registered workflow. ..."
    },
    ...
  ],
  "reference": [
    {
      "name": "wise-estimation",
      "plugin": "wise",
      "description": "Story-point estimation reference. ..."
    },
    ...
  ],
  "siblings_installed": {}
}
```

Parse the JSON. Read the `standalone` and `reference` keys.
`siblings_installed` is always empty — `wise` ships as a single
plugin — and can be ignored.

#### 1a. Bootstrap-failure handling

This branch keys on the **`BOOTSTRAP:` protocol**, not a generic
non-zero exit. If `list-skills` stdout contains a `BOOTSTRAP:*` token
instead of valid JSON (`need-python` / `need-node` / `need-gh` /
`need-gh-auth` / `pip-failed`), the runtime isn't set up. **Stop. Do
NOT fall back to an in-context skill list or guess a command.** Relay
the `BOOTSTRAP:` line and any `OPTION:` / `NOTE:` lines verbatim (for
`pip-failed`, also relay the pip stderr — that's where the "why pip
refused" detail lives), then route the user to `/wise-init`. It's the
single remediation path — idempotent, prompts only for gaps; don't
invent install instructions inline (the PEP-668 / mise nuance is
`/wise-init`'s to explain). Re-run `/wise` once it reports OK.

**Non-`BOOTSTRAP:` failures.** If `list-skills` exits non-zero with NO
`BOOTSTRAP:` token (malformed frontmatter, missing `skills/` dir, an
unsupported subcommand), `/wise-init` won't help — the runtime is fine;
the catalog itself is broken. Surface stderr verbatim, suggest the user
reinstall or update the plugin (`/plugin install
wise@wise-claude`) or file a bug, and stop. Do NOT route
to `/wise-init` in this branch.

### 2. Decide the mode

Look at `$ARGUMENTS`:

- **Empty** — print the catalog (see [§3](#3-print-the-catalog))
  and stop.
- **Non-empty** — classify the intent against the catalog (see
  [§4](#4-classify-intent)).

Exactly one of the two modes runs per invocation. Never both.

### 3. Print the catalog

Print the discovered skills grouped by shape. Use this structure:

```
## wise plugin

### Standalone commands

  - `/wise-init` — First-time dep-install wizard.
  - `/wise-workflow-run [<workflow-name>]` — Start a workflow.
  - `/wise-workflow-resume [<run-ulid>]` — Resume a paused run.
  - `/wise-workflow-status [<run-ulid>]` — Show runs or dump one.
  - `/wise-workflow-list` — List available workflows.
  - `/wise-workflow-create <name>` — Scaffold a new workflow.
  - `/wise-workflow-remove <name>` — Delete a user workflow.
  - `/wise-skills-create <skill-name>` — Scaffold a new skill.
  - `/wise-skills-edit <skill-name>` — Edit an existing skill.
  - `/wise-commit-message` — Draft a Conventional-Commits subject.
  - `/wise-commit` — Draft + commit (no push).
  - `/wise-commit-push` — Draft + commit + push.
  - `/wise-pr-create` — Create or refresh a PR.
  - `/wise-pr-add-reviewers` — Attach reviewers.
  - `/wise-pr-watch` — Watch CI + drive fixes to green.
  - ... (read from the JSON)

### Reference skills (auto-triggered by prose)

  - `wise-estimation` — Fibonacci story-point scale.
  - ... (read from the JSON)

---

Tip: type `/wise-` and hit Tab to see every command inline in
Claude Code's slash menu. Or describe what you want in prose —
`/wise open a PR for this branch` — and I'll propose the matching
command.
```

Render from the JSON — don't hard-code the command list in this
skill's prose. Standalone skills' descriptions come from the
`description:` field (first sentence only). Reference skills show
just the name + first-sentence description.

### 4. Classify intent

The user typed `/wise <free-form>`. Your job is to map the request to
one of the standalone commands in the catalog.

**Matching is pure LLM judgement.** No regex, no fuzzy library,
no keyword table. Read the user's `$ARGUMENTS`, skim the catalog's
descriptions, and pick the best match.

Three possible outcomes:

#### 4a. Confident one-match

You see a clear winner — the user's intent maps unambiguously to
one command. Propose it via `AskUserQuestion`:

- Question: `I think you want to run \`/<command>\`. Continue?`
- Header: `Suggest`
- Options:
  - `Run /<command> now (Recommended)` — description: `Invokes the skill with the args I extracted from your request.`
  - `Show help instead` — description: `Print the full catalog without running anything.`
  - `Cancel` — description: `Do nothing.`

If the user's prose contains a positional that the command takes
(e.g. "`/wise run the ticket-plan workflow`" → `workflow-name =
ticket-plan`), extract it and include it in the option
description so the user sees what will actually run: `Run
/wise-workflow-run ticket-plan now (Recommended)`.

On "Run now", invoke:

```
Skill({
  skill: "<plugin>:<skill-name>",    // from the catalog entry
  args: "<extracted args or empty>"
})
```

`<plugin>` comes from the catalog entry's `plugin:` field.
Example: invoking `wise-workflow-run` is
`Skill({ skill: "wise:wise-workflow-run", args: "ticket-plan" })`.
Relay the child skill's output verbatim.

On "Show help instead", jump to [§3](#3-print-the-catalog).

On "Cancel", stop.

#### 4b. Ambiguous — 2–3 plausible candidates

You see multiple candidates that fit the request roughly equally.
Present all of them via `AskUserQuestion`:

- Question: `Did you mean one of these?`
- Header: `Suggest`
- Options: one per candidate, labelled `Run /<command>` with
  description being the candidate's first-sentence description from
  the catalog. Plus `Show help instead` and `Cancel`.

On a pick, invoke that skill. Otherwise behave as [§4a](#4a-confident-one-match).

Cap at 3 candidates total — if more than 3 look equally plausible,
your classification is too weak; jump to [§4c](#4c-no-match) instead.

#### 4c. No match

Nothing in the catalog fits. Be explicit:

```
I couldn't map "<user's request>" to any wise command.

Here's the full catalog:
```

Then render the catalog ([§3](#3-print-the-catalog)). Do not
invent commands that don't exist in the JSON. Do not apologise at
length — one line naming the gap, then the catalog.

## Guardrails

- Never invoke a skill outside the `wise` namespace. The catalog
  from `engine.sh list-skills` is the single source of truth for
  what's invocable.
- Never run a suggested command without user approval. Every
  classification goes through `AskUserQuestion` first, even when
  the match looks unambiguous. The user explicitly typing
  `/wise-workflow-run` is the opt-in for silent execution; typing
  `/wise something` is not.
- Never re-implement an action skill's logic inline. When the user
  approves, invoke the target skill via the `Skill` tool and relay
  its output verbatim.
- Never invent a command that isn't in the JSON. If you're tempted
  to propose `/wise-something-useful` that doesn't appear, the
  answer is to fall back to the help listing, not fabricate.
- Never route to another wise skill on implicit heuristics (like
  "this word appears in the skill name"). The classifier relies on
  semantic fit against the description, not lexical overlap.
- Never chain actions. `/wise` proposes one command per invocation.
  If the user wants to run three commands, they can issue three
  `/wise` requests — or just use the flat commands directly.
- Never write to any file. State mutation happens inside the
  target action skill, not here.
- Never degrade to an in-context skill list when `engine.sh
  list-skills` fails. Follow
  [§1a](#1a-bootstrap-failure-handling) only when the catalog
  load returns a `BOOTSTRAP:*` token in its output — that's the
  case `/wise-init` was built for. If `list-skills` fails without
  any `BOOTSTRAP:` output (malformed frontmatter, missing skills
  dir, unsupported subcommand), surface stderr verbatim and stop;
  do not point at `/wise-init`. The model's training-time memory of
  what `/wise-*` commands exist is not a substitute for the live
  catalog, and pretending otherwise hides a broken install.
