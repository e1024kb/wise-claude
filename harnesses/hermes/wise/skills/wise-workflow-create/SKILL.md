---
name: wise-workflow-create
description: >-
  Scaffold a new workflow through a step-by-step wizard — collects name,
  description, project-selection policy, pre-flight pins, inputs, and
  steps (id, type, type-specific fields, dependencies), previews the
  generated YAML + README, and writes on confirmation. Writes to `the wise
  data dir's workflows/definitions/<name>/` by default, or offers the
  bundled `harnesses/claude/wise/workflows/<name>/` path when run inside a
  clone of the marketplace repo. Invoked as `/wise-workflow-create`. Use
  when the user says "create a workflow", "scaffold a workflow", "new
  workflow", "author a workflow", or types `/wise-workflow-create`.
---

## Harness adaptation note

This skill was authored for Claude Code and adapted for Nous Research Hermes Agent. Where the steps below reference Claude-specific tools, substitute:

- **AskUserQuestion** — ask the user the same question in plain chat and wait for their reply.


# /wise-workflow-create — wizard for a new workflow

## Why this skill exists

Writing workflow YAML by hand is error-prone (schema is strict, DAG
edges are easy to mis-draw). This skill walks the user through the
schema step-by-step via `AskUserQuestion`, validates as it goes, and
produces a well-formed file.

Two write destinations are supported:

- **User-authored (default)** — `${WISE_DATA_DIR:-$HOME/.local/share/wise}/workflows/definitions/<name>/workflow.yaml`.
  Private to the user's machine; not tracked in any repo. This is
  the right place for one-off or personal workflows.
- **Bundled (opt-in when in-repo)** — `<repo>/harnesses/claude/wise/workflows/<name>/workflow.yaml`
  inside a clone of the wise plugin's marketplace repo. Ships with
  the plugin, tracked in git, seen by every wise installer. The
  wizard only offers this mode when it detects it's running inside
  such a clone (see [§2.5](#25-detect-write-destination) below).

Both destinations use the folder form — `<name>/workflow.yaml` —
so a workflow can later grow sibling `templates/` / `prompts/`
directories without a layout migration.

The wizard deliberately **does not delegate to `skill-creator`** —
workflows aren't skills; the YAML shape is our own.

## Arguments

Read `$ARGUMENTS`. The first whitespace-separated token is the
workflow `<name>`. When `$ARGUMENTS` is empty, stop with an error
pointing at the expected form (`/wise-workflow-create my-flow`).

- `name` — workflow name; kebab-case; matches the filename. Must not
  be `list|create|run|resume|remove|status` (those are reserved as
  subcommand verbs).

## Procedure

### 1. Bootstrap Python (fast-path via `/wise-init` registry)

Prefer the cached registry `/wise-init` wrote; fall back to the live
probe only when the registry is missing, stale, or Python itself
isn't installed yet:

```bash
STATUS="$(python3 "${WISE_PLUGIN_ROOT}/scripts/init-registry.py" check 2>/dev/null || true)"
case "$STATUS" in
  INIT:ok) : ;;
  *)
    echo "Tip: run /wise-init to cache dep probe results and speed up future runs." >&2
    bash "${WISE_PLUGIN_ROOT}/scripts/bootstrap-deps.sh"
    ;;
esac
```

Stop on `BOOTSTRAP:need-python` / `BOOTSTRAP:pip-failed` (only
produced on fall-through; the `INIT:ok` path emits nothing).

### 2. Validate `name`

- `name` matches `^[a-z][a-z0-9]*(-[a-z0-9]+)*$`. Reject otherwise.
- `name` is not in the reserved list. Reject otherwise.
- None of the following already exist (both layouts are checked —
  see the engine resolution order in `scripts/workflows.py`):
  - `${WISE_PLUGIN_ROOT}/workflows/<name>/workflow.yaml` (bundled, folder)
  - `${WISE_PLUGIN_ROOT}/workflows/<name>.yaml` (bundled, legacy flat)
  - `${WISE_DATA_DIR:-$HOME/.local/share/wise}/workflows/definitions/<name>/workflow.yaml` (user, folder)
  - `${WISE_DATA_DIR:-$HOME/.local/share/wise}/workflows/definitions/<name>.yaml` (user, legacy flat)

  If any exist, stop with a clear message pointing at the existing
  file; the user must `remove` first or pick a new name.

### 2.5. Detect write destination

Walk up from `$(pwd)` looking for a marker pair that identifies a
clone of the wise plugin's marketplace repo: a sibling
`harnesses/claude/wise/.claude-plugin/plugin.json` file AND a
`.claude-plugin/marketplace.json` at the same ancestor. The first
ancestor matching both is the repo root.

```bash
REPO_ROOT=""
d="$(pwd)"
while [ "$d" != "/" ]; do
  if [ -f "$d/harnesses/claude/wise/.claude-plugin/plugin.json" ] \
     && [ -f "$d/.claude-plugin/marketplace.json" ] \
     && [ -d "$d/harnesses/claude/wise/workflows" ]; then
    REPO_ROOT="$d"
    break
  fi
  d="$(dirname "$d")"
done
echo "REPO_ROOT=${REPO_ROOT:-<none>}"
```

- If `REPO_ROOT` is empty → `mode = user-authored` (no question).
- If `REPO_ROOT` is set → `AskUserQuestion`:
  - Question: `You're inside the wise marketplace repo at <REPO_ROOT>. Save this workflow as bundled (shipped with the plugin, tracked in git) or user-authored (private to your machine)?`
  - Header: `Destination`
  - Options:
    - `Bundled (Recommended) — ships with plugin` — `Write to <REPO_ROOT>/harnesses/claude/wise/workflows/<name>/workflow.yaml. Tracked in git; seen by every wise installer after the next plugin release.`
    - `User-authored — private to your machine` — `Write to ${WISE_DATA_DIR:-$HOME/.local/share/wise}/workflows/definitions/<name>/workflow.yaml. Not tracked in any repo.`

Store the choice as `mode`. Compute the target path — always folder
form (`<name>/workflow.yaml`) so the workflow can ship its own
artifacts in sibling `templates/` and `prompts/` directories without
needing a future migration:

- `mode = bundled` → `TARGET = "${REPO_ROOT}/harnesses/claude/wise/workflows/<name>/workflow.yaml"`
- `mode = user-authored` → `TARGET = "${WISE_DATA_DIR:-$HOME/.local/share/wise}/workflows/definitions/<name>/workflow.yaml"`

Re-check the collision rule from [§2](#2-validate-name) against
`TARGET` specifically — in bundled mode the existing-bundled check
is what matters; in user-authored mode the existing-user check is
what matters.

### 3. Collect top-level fields

Build up a Python-style dict **in your own working memory** as you
ask; you will render YAML from it at the end. Start with:

```
workflow = {
  "version": 1,
  "name": "<name>",
  "description": None,
  "author": None,
  "requires": [],
  "project-selection": None,
  "agents": None,                  # set by §3.2 only when policy is `auto`
  "preflight": {},                 # filled by §3.3 if any key is pinned
  "inputs": [],
  "steps": [],
}
```

`AskUserQuestion`:

- **Description.** Question: `One-sentence description of what this
  workflow does.` Header: `Description`. Options: "Other" only (user
  types free text). Save as `workflow["description"]`.
- **Author (optional).** Question: `Your handle, for the author field?
  (Empty = skip.)` Header: `Author`. Options: `Skip`, `Other`. Save
  to `workflow["author"]` or leave None.
- **Project selection policy.** Question: `How should the workflow
  resolve the project it runs against?` Header:
  `Project policy`. Options:
  - `current (recommended)` — `Auto-detect the project from the current git repository / working directory at run start.`
  - `prompt` — `Auto-detect, then ask the user at run start to confirm or override the project path/name/kind.`
  - `any` — `The workflow does not care about projects; no selection made.`
  Save as `workflow["project-selection"]`.

### 3.2. Agents policy (optional)

`wise` ships an SDLC agent roster (`wise:architect`, `wise:software-engineer`,
`wise:code-reviewer`, … — see `${WISE_PLUGIN_ROOT}/AGENTS.md`). A
workflow's `prompt` steps can be dispatched to a roster agent instead of
the generic `general-purpose` subagent. The workflow-level `agents:`
policy sets the default for every `prompt` step that doesn't pin its own
`agent:`. `AskUserQuestion`:

- Question: `Should this workflow's prompt steps default to the wise agent roster?`
- Header: `Agents`
- Options:
  - `Leave off (default)` — `Prompt steps run as a plain general-purpose subagent unless a step sets its own agent:. Matches pre-roster behaviour.`
  - `Auto-select per step` — `Each prompt step with no explicit agent: is routed to the best-fit roster role (the conductor picks). Individual steps can still override or opt out.`

Save `workflow["agents"] = "auto"` only when the user picks auto;
otherwise leave it `None` (the §6 render omits it, matching pre-roster
shape).

### 3.3. Pre-flight pins (optional)

Every workflow goes through three pre-flight questions at run
start: control-mode (wave-sync / synchronous / auto-advance),
worktree (current tree vs new worktree), rename_session (rename the
Claude Code session for /resume). A workflow author can **pin** any or
all of those answers when one of them doesn't make sense for the
workflow — e.g. a workflow with `ask` steps and interactive prompts
needs wave-sync or auto-advance, because synchronous mode would
auto-approve gates and skip the asks; pin `auto-advance` instead of
`wave-sync` when those prompts should fire but the runner should NOT
be asked to start each wave. Pinned answers skip the corresponding
AskUserQuestion at run start and are logged for the user.

For each of the three keys below, default to `Leave it to the
runner (prompt)` — that's the current behaviour and the right
answer when the workflow works fine in either mode.

**3.3a. Control mode.** `AskUserQuestion`:
- Question: `Should the runner pick the control mode at pre-flight, or pin one for this workflow?`
- Header: `Control mode`
- Options:
  - `Leave it to the runner (prompt)` — `Ask the runner at pre-flight (default).`
  - `Pin wave-sync` — `Force wave-sync. Pick this when the workflow needs human input (ask steps, AskUserQuestion inside prompt/interactive steps, approval gates) AND the runner should review progress between waves.`
  - `Pin auto-advance` — `Force auto-advance. Like wave-sync (in-step asks/approvals/interactive prompts still fire) but with no between-wave menu — the run flows wave-to-wave and stops only where a step needs input. Pick this when the workflow has its own questions but the runner should NOT be asked to start each wave.`
  - `Pin synchronous` — `Force synchronous. Pick this when the workflow is end-to-end automated and has no steps that need human input.`

If the user picks pin wave-sync, pin auto-advance, or pin
synchronous, save to `workflow["preflight"]["control-mode"]`.

**3.3b. Worktree.** `AskUserQuestion`:
- Question: `Should the runner be asked to pick a worktree, or pin one choice for this workflow?`
- Header: `Worktree`
- Options:
  - `Leave it to the runner (prompt)` — `Ask the runner at pre-flight (default).`
  - `Pin current tree` — `Force in-place execution. Pick this for workflows that don't modify files (status checks, read-only reports).`
  - `Pin new worktree` — `Force a dedicated worktree on every run. Pick this for workflows that make destructive-ish edits and the user should always be able to throw the tree away.`

Save to `workflow["preflight"]["worktree"]` when pinned.

**3.3c. Session rename.** `AskUserQuestion`:
- Question: `Should the runner be asked to rename the Claude Code session for /resume, or skip that prompt?`
- Header: `Session rename`
- Options:
  - `Leave it to the runner (prompt)` — `Ask the runner at pre-flight (default).`
  - `Pin skip` — `Don't ask. Pick this for short-running workflows where the rename prompt is more friction than the /resume label is worth.`

Save to `workflow["preflight"]["rename_session"]` when pinned.

If all three came back as `prompt`, leave `workflow["preflight"]`
empty — the YAML render in §6 omits empty blocks so the produced
file matches pre-0.42 shape exactly.

### 3.5. Inputs loop (optional)

Inputs are values the user must supply at run start — `workflow-run`
asks for each via `AskUserQuestion` during pre-flight, validates
the answer against the declared regexes, and exposes it to every
step as a `{{<name>}}` template variable. Use inputs whenever a
workflow's first step would otherwise try to extract a value from
invocation prose (ticket ids, branch names, target environments,
etc.) — the TUI prompt is a better UX than a subagent asking in
chat.

**3.5a. Continue?** `AskUserQuestion`:
- Question: `Does this workflow need any user-supplied inputs before the DAG launches? (<N> so far)`
- Header: `Inputs`
- Options: `Add input`, `Done — move to steps`.

On Done, go to [§4](#4-step-loop).

**3.5b. Name.** Ask for a kebab/snake-case identifier matching
`^[a-z][a-z0-9_]*$`. This is the variable name used in `{{…}}`
templates. Re-ask on collision with already-added inputs.

**3.5c. Prompt text.** Free-text question the user will see in the
AskUserQuestion call (e.g. `Which Jira ticket? (PROJ-18572 or a
browse URL)`).

**3.5d. Validate regex (optional).** Full-match regex the cleaned
value must satisfy. If the user skips, accept any non-empty string.

**3.5e. Extract regex (optional).** `re.search` regex applied
*before* validation — useful when the user might paste a full URL
and you want the id out of it. If the regex has a capture group,
group(1) is the result; otherwise group(0). Skip if the value is
already bare.

Store each finished input as
`{"name": …, "prompt": …, "validate": …, "extract": …}` (omitting
the optional keys when the user skipped them) and append to
`workflow["inputs"]`. Loop back to 3.5a.

### 4. Step loop

Repeat until the user chooses Done:

**4a. Continue?** `AskUserQuestion`:
- Question: `Add another step? (<N> so far)`
- Header: `Steps`
- Options: `Add step`, `Done — preview workflow`.

On Done, go to [§5](#5-external-requires).

**4b. Step id.** Ask for a kebab-case id. Validate against the regex
above and against the set of already-added ids. On collision, re-ask.

**4c. Step type.** The valid types are `skill`, `prompt`,
`interactive`, `bash`, `approval`, `ask`. `AskUserQuestion` caps at
four options, so present the four most relevant for this workflow and
let the user name any other via the `Other` free-text field (or ask in
two passes). Type meanings:

- `skill` — invoke a registered skill (`wise:…` or 3rd-party).
- `prompt` — free-form prompt to an isolated subagent.
- `interactive` — free-form prompt run inline in the conductor's main
  thread (full tools + `AskUserQuestion`); use when the step must
  prompt the user mid-flow or drive a per-item wizard.
- `bash` — run a shell command.
- `approval` — pause for user approval.
- `ask` — capture a free-text/binary value into an output.

**4d. Type-specific fields.**

Build `step = {"id": <id>, "type": <type>, "depends_on": []}` and
extend per type:

- **skill:**
  - Ask for the namespaced skill id (`wise:wise-workflow-list`,
    `skill-creator:skill-creator`, …). Validate format
    `^[a-z][a-z0-9-]*:[a-z][a-z0-9-]*$`.
  - Ask whether the skill takes a payload. If yes, ask for the payload
    as multi-line `key: value` pairs via a free-text `AskUserQuestion`
    with "Other" — parse each line at `:`. Store as
    `step["payload"]`. Empty → `{}`.

- **prompt:**
  - Ask for the prompt body (free text, multi-line). Templates
    (`{{project.path}}`, `{{var}}`) allowed. Store as `step["prompt"]`.
  - Ask whether there's an `until:` regex. If yes, ask for the regex
    and for a `max_iterations` integer (default 3). Store both.
  - Ask whether the step captures named outputs. If yes, ask for a
    comma-separated list of names. Store as `step["outputs"]`.
  - **Agent (optional).** `AskUserQuestion`: `Which subagent runs this
    prompt step?` Header `Agent`. Options:
    - `Inherit workflow policy (default)` — omit `step["agent"]`; the
      step follows the workflow-level `agents:` policy (§3.2).
    - `Auto-select a role` — store `step["agent"] = "auto"`.
    - `Force a specific role` — list the roster (run
      `python3 "${WISE_PLUGIN_ROOT}/scripts/workflows.py" list-agents`
      and offer the `name`s) and store the picked role as
      `step["agent"]`.
    - `Build a team (multiple roles)` — the step is worked by several
      roster roles at once and the conductor synthesizes one result. Run
      the **team builder** below and store the resulting list as
      `step["agent"]`.
    - `general-purpose (no persona)` — store `step["agent"] = "off"`.

    **Team builder** (only when `Build a team` is picked). Roster from
    `list-agents`.
    1. `AskUserQuestion` (multiSelect): `Which roles are on this step's
       team?` — offer the roster `name`s; require ≥2.
    2. `AskUserQuestion`: `Which role leads (integrates the others before
       the conductor's synthesis)?` — the chosen roles **plus** `No lead
       (equal peers)`.
    3. `AskUserQuestion`: `Per-member model/effort, or shared step-level?`
       Options: `Shared (set once for the whole step)` → ask the Model and
       Effort questions below **once** and store them as the step-level
       `step["model"]`/`step["effort"]`, and write each team member as a
       bare role-name string; `Per member` → for each role ask its own
       Model + Effort and write that member as
       `{role: <name>, model?: …, effort?: …}` (omit a knob when left at
       default). Either way, mark the lead with `lead: true` (this forces
       the object form for the lead).
    Store the assembled list (bare strings and/or
    `{role, lead?, model?, effort?}` objects) as `step["agent"]`. When the
    team uses shared knobs, **skip** the standalone Model/Effort questions
    below (already collected); otherwise still ask them as the step-level
    fallback for any bare-string member.
  - **Model (optional).** `AskUserQuestion`: `Pin a model for this step?`
    Header `Model`. Options: `Inherit session (default)` (omit
    `step["model"]`), `opus`, `sonnet`, `haiku` — store the alias as
    `step["model"]` when not default. (`fable`/`inherit` accepted too
    via Other.)
  - **Effort (optional).** `AskUserQuestion`: `Pin reasoning effort for
    this step?` Header `Effort`. Options: `Inherit (default)` (omit
    `step["effort"]`), `low`, `high` — and `medium`/`xhigh`/`max` via
    Other. Store as `step["effort"]` when not default. (Conveyed as a
    prompt directive only — best-effort, may be ignored by the model
    today; the real per-step knob is `model:`. See
    `docs/wise/workflows.md`.)

- **interactive:** same `prompt` body + optional `until:` +
  `max_iterations` + `outputs:` fields. The difference is execution — an
  interactive step runs inline in the conductor instead of an isolated
  subagent, so it can call `AskUserQuestion`. Collect those fields, but
  **do not** ask the Agent / Model questions: an interactive step is the
  conductor itself, so it can't become a subagent or switch model
  mid-conversation. (`agent:` / `model:` are `prompt`-only.)

- **bash:**
  - Ask for the command (free text). Templates allowed. Store as
    `step["command"]`.
  - Ask for `cwd`. Default suggestion: `{{project.path}}`. Store.
  - Ask for expected `exit_code` (default 0). Store as
    `step["success"]["exit_code"]`.
  - Ask whether there's an expected `stdout_matches` regex. If yes,
    store under `step["success"]["stdout_matches"]`.
  - Ask for a timeout in seconds (default 120). Store as
    `step["timeout"]`.

- **approval:**
  - Ask for the message text (free text, multi-line). Templates
    allowed. Store as `step["message"]`.

- **ask:**
  - Ask for the question text the user will see (free text).
    Store as `step["question"]`.
  - Ask for the `output` variable name — kebab/snake-case, matches
    `^[a-z][a-z0-9_]*$`. The captured answer is stored in
    `state.outputs[<output>]`, referenceable as `{{<output>}}` in
    later step definitions. Store as `step["output"]`.
  - Ask for an optional header (≤12 chars, chip label). Store as
    `step["header"]` only when provided.
  - `AskUserQuestion`: `How should this step present its options to
    the user?`
    - `Free-text (default)` — `The user picks a skip label or types a value via the Other affordance. Use for "what's your comment?" / "which ticket?" / similar open answers.`
    - `Binary choice (yes/no-ish)` — `Two explicit options — a skip label and a confirm label. Use for yes/no / "opt-in to this extra stage".`
    Store the pick as a working variable `ask_shape`.
  - Ask for an optional skip-option label (default `Skip`). Store
    as `step["skip_label"]` only when non-default.
  - **If `ask_shape == "Binary choice"`:** also ask for
    - `confirm_label` (required) — the label for the "yes" option
      (e.g. `Yes — watch pipelines`). Store as
      `step["confirm_label"]`.
    - `confirm_value` (optional, defaults to the confirm label
      verbatim) — the string recorded to the output when the user
      picks confirm. Shorter values (`yes`, `enabled`) make later
      `when:` clauses like `when: "<output> == 'yes'"` cleaner.
      Store as `step["confirm_value"]` only when different from
      the label.

**4e. Dependencies.** If at least one prior step exists,
`AskUserQuestion` with `multiSelect: true`, options = each prior
step's id (as a label) plus an explicit `None (runs first)` option.
On `None`, `step["depends_on"] = []`. Otherwise the selected ids go
into `step["depends_on"]`. If multiple selected AND ≥2 prior steps
already share these deps, mention that they will run in parallel —
one follow-up question (`OK?` / `Let me change`); on change, loop
4e.

**4f. Trigger rule (only if depends_on has ≥2 entries).**
`AskUserQuestion` for `trigger-rule`: `all-success`, `one-success`,
`all-done`, `none-failed-min-one-success`. Default `all-success`.
Store as `step["trigger-rule"]` only if not default.

Append `step` to `workflow["steps"]` and loop to 4a.

### 5. External requires

`AskUserQuestion`: `Does this workflow depend on plugins or skills
that wise doesn't already provide?` Header: `Requires`. Options: `No`,
`Yes, list them`.

On `Yes`: ask for a comma-separated list of entries of the form
`plugin:<name>` or `skill:<plugin>:<skill-name>`. Parse and validate;
store as:

```
workflow["requires"] = [
  {"plugin": "...",} or {"skill": "plugin:skill"},
  ...
]
```

### 6. Preview and confirm

Render the YAML in your working memory as a string — make sure:

- Top-level keys appear in the order:
  `version, name, description, author, requires, project-selection, agents, preflight, inputs, steps`.
- Keys that are `None` / empty are omitted (don't emit
  `author: null`, `requires: []`, `agents: null`, or `preflight: {}`
  when empty — the `agents:` line is dropped unless §3.2 set `auto`, and
  the `preflight:` block is skipped entirely when no key was pinned in
  §3.3, so the produced YAML matches pre-roster / pre-0.42 shape exactly
  for the common "leave everything to the runner" case).
- Step keys appear in the order:
  `id, type, <type-specific>, agent, model, effort, depends_on, trigger-rule`
  (`agent` / `model` / `effort` appear only on `prompt` steps that set
  them).

Show the rendered YAML to the user in a code block and
`AskUserQuestion`:

- Options: `Save`, `Edit a step (re-enter loop at [§4](#4-step-loop))`,
  `Cancel (discard)`.

On `Edit`, jump back to [§4](#4-step-loop) (re-using the existing list).

### 7. Write

On `Save`, write to the `TARGET` computed in [§2.5](#25-detect-write-destination).
`TARGET` always ends in `<name>/workflow.yaml`, so the immediate
parent of `TARGET` is the workflow folder (which does NOT exist yet
and must be created).

- **User-authored mode.** Ensure the workflow folder exists:

  ```bash
  mkdir -p "${WISE_DATA_DIR:-$HOME/.local/share/wise}/workflows/definitions/<name>"
  ```

  Write the YAML via the `Write` tool to `TARGET` (i.e.
  `${WISE_DATA_DIR:-$HOME/.local/share/wise}/workflows/definitions/<name>/workflow.yaml`).

- **Bundled mode.** `<REPO_ROOT>/harnesses/claude/wise/workflows/` always exists
  in a valid clone, but the `<name>/` subfolder does not. Create it:

  ```bash
  mkdir -p "<REPO_ROOT>/harnesses/claude/wise/workflows/<name>"
  ```

  Write the YAML via the `Write` tool to `TARGET` (i.e.
  `<REPO_ROOT>/harnesses/claude/wise/workflows/<name>/workflow.yaml`).

Mention to the user that they can drop workflow-owned artifacts
(templates, prompt fragments, fixtures) as siblings of the
`workflow.yaml` and reference them from steps with
`{{workflow.dir}}/<subdir>/<file>`. Common layout:

```
<name>/
├── workflow.yaml
├── README.md       # generated alongside — see §7.5
├── templates/      # optional; e.g. pr-template.md
└── prompts/        # optional; e.g. ensure-pr.md, watch-pipelines.md
```

### 7.5. Generate the README.md

Every workflow ships a README.md beside its `workflow.yaml`. Compose it
from the in-memory `workflow` dict and `Write` it to
`<TARGET_FOLDER>/README.md` — the folder already exists from §7, so this
is the wizard's second `Write` (first was the YAML).

Produce these sections (drop whole sections when empty):

- **Title + description** — `# <name>` and the verbatim
  `workflow["description"]`, preceded by an HTML comment noting the
  README must stay in sync with `workflow.yaml` + `prompts/*.md` (the
  CLAUDE.md invariant).
- **When to use / When not to use** — one placeholder bullet each for
  the author to refine.
- **Prerequisites** — `/wise-init` completed; one bullet per
  `workflow["requires"]` entry; a "run from inside the project repo"
  bullet unless `project-selection: any`.
- **Flow** — a mermaid `flowchart TD` (rules below).
- **Steps** — table `Step | Type | Purpose` (purpose derived in one
  line from prompt / command / message / question).
- **Inputs** — table `Name | Required | Description` if the workflow
  has `inputs:`, else `None.`.
- **Outputs** — table `Name | Source | Used for`, from prompt steps'
  `outputs:` and ask/interactive steps' `output:`.
- **Examples** — `/wise-workflow-run <name>`.
- **Related** — link to `./workflow.yaml` (and any templates/prompts
  added later).

**Mermaid rules:** one node per step labelled `<id><br/><type>` (append
`→ <outputs>` when declared); one edge per `depends_on` entry
(`<dep> --> <id>`); a `when:`-gated parent renders as a decision diamond
with edges labelled by the when-value (a plain arrow + edge label is a
fine fallback); sort topologically (entry nodes top, terminal bottom).
Keep it a flat DAG — no nested subgraphs.

Then print:

```
Saved /wise-workflow-<name> to:
  <TARGET>
  <TARGET_FOLDER>/README.md  (overview + mermaid flow diagram)
```

In bundled mode, also print a pointer about the release workflow:

```
Heads up — this is a bundled workflow. Commit the new file plus a
CHANGELOG entry + plugin.json version bump in the same PR per the
wise plugin's versioning rules (see CLAUDE.md / CONTRIBUTING.md §9).
```

### 8. Point at the next command

Do NOT invoke `wise:wise-workflow-run` from this skill — the invariant
that only `wise-workflow-run` and `wise-workflow-resume` may call wise
action skills still holds here. Instead, end with a clear pointer:

```
Ready to run it:
  /wise-workflow-run <name>
```

## Guardrails

- Only two write destinations are valid: the user-authored path
  (`${WISE_DATA_DIR:-$HOME/.local/share/wise}/workflows/definitions/`) and — only when
  the detection in [§2.5](#25-detect-write-destination) matches and
  the user opts in — the bundled path
  (`<REPO_ROOT>/harnesses/claude/wise/workflows/`). Never write anywhere else.
- Never overwrite an existing workflow file (bundled or
  user-authored). Collision = stop with a clear message; the user
  must `remove` first or pick a new name.
- Never invoke another action skill. Per CLAUDE.md, only
  `workflow-run` and `workflow-resume` may call wise action skills via
  the `Skill` tool; `workflow-create` authors definitions and nothing
  more. After saving, point the user at `/wise-workflow-run <name>`.
- If the user cancels at any point, discard the in-memory workflow
  and stop. Do not leave partial files behind.
