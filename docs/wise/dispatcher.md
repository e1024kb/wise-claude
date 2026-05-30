# The wise natural-language helper

The `wise` plugin exposes every action as a **flat, autocomplete-visible
slash command** with a `wise-` prefix: `/wise-init`, `/wise-workflow-run`,
`/wise-pr-create`, and so on. There is no routing layer — typing
`/wise-` in the slash menu fans out to every command, and users invoke
the one they want directly.

What survives is the bare `/wise` command, which is **not** a dispatcher.
It is a **natural-language helper**: a thin skill whose only job is to
let users describe what they want in prose and get a proposed flat
command back. This page is the reference for how that helper works and
for the one piece of machinery it depends on — the
`scripts/engine.py list-skills` catalog emitter.

## What `/wise` (bare) does

Typing `/wise` with no arguments prints the full catalog of every
command and reference skill in the plugin, grouped by shape. It exists
so a user who just installed the plugin can see what's on offer without
reading the README — and so an existing user who forgets a command
name can skim the list instead of hunting through their shell history.
The catalog renders live from `engine.py list-skills`, so newly
authored skills show up automatically the next time `/wise` is invoked.

## What `/wise <free-form>` does

Typing `/wise <any prose>` — `/wise open a PR`,
`/wise show me running workflows` — turns the helper into an intent
classifier. It reads the catalog, skims the descriptions, picks the
best matching `/wise-*` command (or a small shortlist if ambiguous), and
offers to run it via `AskUserQuestion`. The helper never invokes the
matched command silently; every classification goes through an
explicit confirmation, with `Show help instead` and `Cancel` options
available on every prompt. On confirmation the helper calls the target
skill through the `Skill` tool and relays the output verbatim — it
does not re-implement the target's logic inline.

## The `engine.py list-skills` protocol

`scripts/engine.py list-skills` is the sole machinery the helper
depends on. It is invoked exactly once per `/wise` invocation (via the
stable `scripts/engine.sh` bootstrap — see `CLAUDE.md` for why the
bash wrapper exists) and emits a JSON document on stdout with three
top-level keys:

```json
{
  "standalone": [
    {
      "name": "wise-workflow-run",
      "plugin": "wise",
      "description": "Start a new run of a registered workflow. ...",
      "argument-hint": "[<workflow-name>]"
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

The three keys:

- **`standalone`** — every user-invocable slash-command skill. Each
  entry carries `name` (kebab-case, also the slash-command name),
  `plugin` (always `wise`), `description` (the full frontmatter
  `description:` verbatim — the helper uses the first sentence for
  display and the full text for intent classification), and
  `argument-hint` (the autocomplete hint string; may be empty).
- **`reference`** — description-triggered guidance skills (e.g.
  `wise-estimation`). Same `name` / `plugin` / `description` shape.
  No `argument-hint` — that field's absence is precisely what marks a
  skill as reference rather than standalone.
- **`siblings_installed`** — always an empty object. `wise` ships as a
  single standalone plugin; the key is kept so the catalog shape stays
  stable.

This JSON is **consumed only by the `/wise` helper**. Nothing else in
the plugin parses it; no workflow step, no CLI caller, no other skill.
The shape can evolve freely as long as the helper is updated in
lock-step.

## Discovery

`engine.py` walks `plugins/wise/skills/` and reads the `SKILL.md`
frontmatter of every skill directory it finds. Each skill is bucketed
by the presence or absence of the `argument-hint:` field:

- `argument-hint:` present → **standalone** (user-invocable slash
  command; the field is the autocomplete hint Claude Code shows).
- `argument-hint:` absent → **reference** (description-triggered;
  Claude auto-consults the skill when the user's prose matches).

The skill named `wise` (the helper itself) is skipped — it would
otherwise self-list.

There is no registration step and no manifest. A new skill is
discoverable the instant its directory exists with a valid
`SKILL.md`.

## The helper's decision model

The helper's logic is intentionally small. It loads the catalog once,
then branches on `$ARGUMENTS`:

- **Empty arguments** → render the catalog grouped by shape. Stop.
- **Non-empty arguments** → classify via LLM judgement against the
  catalog. Three outcomes:
  - **One confident match** — propose the single command via
    `AskUserQuestion`. On approval, invoke via the `Skill` tool,
    forwarding any positional extracted from the user's prose (e.g.
    `/wise run the pr-interactive workflow` → the positional
    `pr-interactive` is forwarded to `/wise-workflow-run`).
  - **2–3 plausible candidates** — present a shortlist via
    `AskUserQuestion`. On a pick, invoke that one. Cap at 3 — more
    than that means the classification is weak; fall through to the
    no-match case instead.
  - **No match** — print a one-line "couldn't map that" note and
    render the catalog. Never fabricate a command that isn't in the
    JSON.

Matching is **pure LLM judgement**. No regex, no fuzzy library, no
keyword table, no edit-distance threshold. The helper reads the
user's request, skims the catalog's `description:` fields, and picks
the best semantic fit.

## Why no routing layer

The v1 plugin wrapped every action in a text-token router: a single
`/wise` command that parsed `$1` and `$2`, looked up the matching
skill's arguments schema, validated positionals and flags, and ran a
fuzzy-match fallback when the user mistyped a command. The router was
deterministic, fast, and solved problems that are no longer problems
in v2:

- **Menu clutter** — v1 presented one slash-menu entry per plugin, not
  per action. In v2, autocomplete groups all `/wise-*` commands under
  the shared prefix: typing `/wise-` fans out and previews descriptions
  inline. The menu is no longer cluttered; it's searchable.
- **Typo recovery** — v1's fuzzy matcher caught `/wise workflw run` and
  suggested `workflow`. In v2, Claude Code's autocomplete prevents
  most typos before submission, and the `/wise` helper picks up the
  rest: typing `/wise workflw run` classifies cleanly to
  `/wise-workflow-run` via the LLM, which is more forgiving than an
  edit-distance matcher (handles `/wise show running workflows` →
  `/wise-workflow-status` just as easily).
- **Arg validation** — v1 parsed each skill's `arguments:` frontmatter
  schema and rejected unknown flags up front. In v2, each skill
  parses its own `$ARGUMENTS` string in its procedure. The
  argument-validation layer moved from a central router into the
  skill bodies, where it was always conceptually owned.

Dropping the router drops ~400 lines of Python and a whole category
of bugs (argument-schema drift between frontmatter and skill body,
fuzzy-match thresholds tuned by hand, level-aware disambiguation
between `$1` and `$2`). What's left is a catalog emitter and a
natural-language classifier — two tools that exist for discovery, not
for plumbing.

## Guardrails

The helper operates under a handful of strict rules:

- **Only invoke `wise` skills.** The catalog is the single source of
  truth for what's invocable. Any skill outside the `wise` plugin is
  out of scope, even if the user asks for it by name.
- **Never route silently.** Every classification goes through
  `AskUserQuestion` before any `Skill` invocation, even when the
  match looks unambiguous. The user explicitly typing
  `/wise-workflow-run` is the opt-in for silent execution; typing
  `/wise something` is not.
- **Never fabricate commands.** If nothing in the catalog fits, print
  the no-match message and render the catalog. Don't propose
  `/wise-something-useful` that doesn't exist.
- **Never chain actions.** One invocation proposes one command. If
  the user wants three things, they issue three `/wise` requests — or
  use the flat commands directly.
- **Never re-implement an action skill inline.** The helper proposes;
  the target skill acts. Mutation (starting a workflow run, opening a
  PR) happens inside the target, not here.

## Backward compatibility with v1 forms

Users who built muscle memory on the v1 dispatcher will sometimes
type the old forms: `/wise workflow run`, `/wise skills create
my-skill`. Those commands no longer exist as routing targets — they're
free-form input from the helper's perspective, same as any other prose.

In practice the LLM classifier handles them trivially: `/wise workflow
run` maps to `/wise-workflow-run`, `/wise skills create my-skill` maps
to `/wise-skills-create my-skill` with the positional forwarded. No
special-case code path. The classifier is strictly **better** at typo
recovery than v1's fuzzy matcher, which was limited to edit-distance
on command tokens: the LLM understands `/wise show me what's running`
→ `/wise-workflow-status` without being taught that "running" maps to
"status".

Users who want to skip the helper entirely just type the flat command
— `/wise-workflow-run`, `/wise-pr-create` — and autocomplete takes over.
The helper exists for discovery, not as a required waypoint.
