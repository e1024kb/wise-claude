---
name: wise-workflow-run
description: >-
  Start a new run of a registered workflow. The main Claude Code
  conversation becomes the conductor — runs pre-flight (control mode +
  worktree), resolves the target project, and executes the workflow's
  DAG wave by wave via the Skill / Task / Bash tools, tracking per-step
  state under a ULID run directory. Invoked as `/wise-workflow-run`
  (bare alias) or `/wise:wise-workflow-run` (canonical). Use when the
  user says "run the workflow", "start a workflow", "kick off
  <workflow-name>", "run the ticket-plan workflow", or types
  `/wise-workflow-run`.
argument-hint: "[<workflow-name> [<input1> <free-form remainder…>]]"
allowed-tools: Read, Write, Skill, AskUserQuestion, TodoWrite, Task, Agent, TeamCreate, TeamDelete, SendMessage, Monitor, TaskCreate, TaskList, TaskGet, TaskUpdate, TaskOutput, TaskStop, Bash(${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap-deps.sh:*), Bash(${CLAUDE_PLUGIN_ROOT}/scripts/init-registry.py:*), Bash(${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py:*), Bash(bash:*), Bash(python3:*), Bash(mkdir:*), Bash(git:*), Bash(test:*)
---

# /wise-workflow-run — the conductor

## Why this skill exists

Running a workflow is an orchestration task: parsing a DAG, tracking
per-step state, driving parallel executions, asking for approvals,
and surfacing progress. This skill owns that loop. Together with
`wise-workflow-resume`, it is one of only two skills in the plugin
allowed to invoke other wise action skills (as part of `type: skill`
steps from a validated workflow definition) — see the invariant
documented in `CLAUDE.md`.

## Arguments

Read `$ARGUMENTS`. The first whitespace-separated token is the
`workflow-name`; everything after it is **positional input values**
for the workflow's declared `inputs:` (see [§2](#2-resolve-the-workflow-name)
and [§6c](#6c-collect-workflow-inputs)). When `$ARGUMENTS` is empty,
the skill prompts interactively in [§2](#2-resolve-the-workflow-name).

- `workflow-name` — matches the filename without `.yaml`. Resolution
  order: user dir first, bundled second. When absent, [§2](#2-resolve-the-workflow-name)
  prompts the user to pick from the available definitions.
- positional inputs — optional. Tokens after the workflow name are
  assigned to the declared inputs **in order**, and the **last
  declared input absorbs the entire remainder of the line** (verbatim,
  spaces preserved) so a trailing free-form prompt works. An input
  satisfied positionally is **not** prompted for in §6c. Example:
  `/wise-workflow-run ticket-auto PROJ-1 prefer the wise-estimation
  skill; never touch infra/*` → `workflow-name=ticket-auto`, first
  input `ticket_ids=PROJ-1`, last input `config_prompt=prefer the
  wise-estimation skill; never touch infra/*`, no questions asked.
  Only the **last** input may contain spaces; every earlier positional
  is a single whitespace-delimited token, so a value that would
  otherwise contain spaces (e.g. a multi-ticket list) must avoid them
  when passed positionally — use `PROJ-1,PROJ-2`, or supply it via the
  interactive prompt, which still accepts spaces.

The workflow YAML *schema* and step-type *semantics* are documented in
`docs/wise/workflows.md`; this skill is the imperative conductor
procedure. Three companion files under
`skills/wise-workflow-run/references/` carry the low-frequency parts —
`preflight.md` (session tagging + questionaries + inputs, read once per
run), `step-types.md` (team / supervised / interactive / approval / ask
dispatch, read only when a wave contains one), and
`roster-resolution.md` (tuning override + model/effort binding, read at
first prompt-step dispatch).

## Procedure

### 1. Init-check + list + picker — in ONE message

Run the init-check per `${CLAUDE_PLUGIN_ROOT}/references/init-check.md`,
firing in the SAME message: `init-registry.py check`, the data call
`workflows.py list-defs`, and a `select:AskUserQuestion` ToolSearch (so
the §2 picker is ready). On `INIT:ok`, use the `list-defs` output and
proceed to §2. Otherwise follow the reference's fallback — this skill
mutates state, so it **drives the need-python install loop** and
proceeds to §2 only once Python is `READY`.

### 2. Resolve the workflow name

If `$ARGUMENTS` is non-empty, use its first whitespace-separated token
as the workflow-name and skip the rest of this step (the `list-defs`
output from §1 is unused in this case — a cheap fork, not worth
re-ordering for). **Keep the text after that first token** — it is the
positional-input remainder consumed in [§6c](#6c-collect-workflow-inputs).
Call it `ARG_REST` (everything in `$ARGUMENTS` after the first token,
with the single separating run of whitespace stripped; preserve all
inner spacing). When the user typed a bare workflow name, `ARG_REST`
is empty.

Otherwise (user typed bare `/wise-workflow-run`), use the `list-defs`
JSON already captured in §1. If the array is empty, stop with:

```
No workflows are registered yet. Create one with
  /wise-workflow-create <name>
or install a marketplace plugin that ships one.
```

Otherwise `AskUserQuestion`:

- Question: `Which workflow do you want to run?`
- Header: `Workflow`
- One option per entry. Label: `[bundled] <name>` for entries with
  `source: bundled`, `[user] <name>` for `source: user` — the
  bracketed tag is part of the label so the source is visible at a
  glance in the picker, not buried in the description. Description:
  `<description first sentence, truncated to ~80 chars>`. Mark
  shadowed bundled entries with a trailing ` (shadowed by user
  definition)` on the description so the user knows which one will
  actually run. Add a final `Abort` option.

On pick, strip the `[bundled] ` / `[user] ` prefix from the chosen
label to recover the bare workflow name (or, simpler, look up the
chosen label's entry in the `list-defs` JSON array by label match
and read its `name` field). Set `workflow-name` to that bare name
and continue. On `Abort`, stop cleanly (no state written, no
"error" framing — the user chose to back out).

### 3. Locate the workflow definition

```bash
DEF=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" locate-def "<workflow-name>")
```

Non-zero exit → relay stderr and stop.

### 4. Probe requires (with install-retry loop)

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" probe-requires "$DEF"
```

Exit 0 (stdout `OK`) → proceed.

Exit 2 → stdout has one or more `MISSING:` lines. Relay them to the
user with the exact install commands:

```
This workflow requires the following that are not installed:
  - <missing item 1>
  - <missing item 2>

Install them (out of band):
  /plugin install <plugin>@<marketplace>
  /plugin install <plugin>@<marketplace>
```

Then `AskUserQuestion`:

- Options: `I've installed them, re-check`, `Abort`.

On `re-check`, re-run the probe. Loop until OK or user aborts. No
auto-install.

### 5. Generate the run ID, tag the session, write stub state

The very first persistent act of a run is to allocate its ULID and
write a stub `state.yaml` that records the Claude Code session the
run was started in. Doing this before pre-flight (rather than after)
is deliberate: if the user later interrupts the conductor and
invokes `/wise-workflow-resume <run-ulid>` from a different session,
resume can compare the stored session UUID against the current one
and offer to send them back to the original session (where the
conductor's TodoWrite list and partial step logs are meaningful).

**5a. Allocate the run ID and run directory:**

```bash
RUN_ID=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" new-ulid)
RUNS_ROOT="$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" runs-root)"
RUN_DIR="$RUNS_ROOT/$RUN_ID"
mkdir -p "$RUN_DIR/logs"
```

**5b–5d. Session capture, label, and claim check:** Read
`${CLAUDE_PLUGIN_ROOT}/skills/wise-workflow-run/references/preflight.md` NOW — once for the whole run; it also carries
§5f, §5h, §6b–§6c below — and follow its §5b (capture the session
UUID), §5c (derive the session label), and §5d (existing-claim
check).

**5e. Write the stub state:**

Build `CTX` as a JSON object with two keys — `claude_session_id`
(JSON-encode `SESSION_ID`, or `null` when it's empty) and
`session_label` (JSON-encode `SESSION_LABEL`), e.g.
`{"claude_session_id":"<uuid>","session_label":"<label>"}`. Then:

```bash
STATE=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" \
  init-state "$DEF" "$RUN_DIR" "$RUN_ID" "$CTX")
```

This writes `state.yaml` with the session fields, the steps list
(all `pending`), and `status: initializing`. Pre-flight answers get
folded in by `start-run` once they're collected.

**5f. Prune old run directories (cap 25):** follow
`${CLAUDE_PLUGIN_ROOT}/skills/wise-workflow-run/references/preflight.md` §5f.

**5g. Read pre-flight pins from the workflow definition:**

Before any of the three pre-flight prompts (rename_session /
control-mode / worktree), read the workflow's `preflight:` block
— it may pin any or all of those answers, in which case the
corresponding prompt is skipped entirely:

```bash
eval "$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" \
  get-preflight "$DEF")"
```

This sets five shell variables:

- `CONTROL_MODE`   — `prompt` (default; ask §6a) | `wave-sync` | `synchronous` | `auto-advance`
- `WORKTREE`       — `prompt` (default; ask §6b) | `current` | `new`
- `RENAME_SESSION` — `prompt` (default; ask §5h) | `skip`
- `TUNING`         — `skip` (default) | `prompt` (ask §6b2)
- `STEP_SELECT`    — `skip` (default) | `prompt` (ask §6b3)

Missing keys (or a missing `preflight:` block entirely) resolve to
each key's default — `prompt` for the three original questions
(matching pre-0.42 behaviour), `skip` for the two opt-in
questionaries, so a workflow only gets a tuning / step-selection
prompt by declaring it. Invalid values emit a `WARN:` line on
stderr and fall back to the key's default so the workflow still
runs.

**5h. Prompt the user to rename the session (skip if pinned):**
follow `${CLAUDE_PLUGIN_ROOT}/skills/wise-workflow-run/references/preflight.md` §5h.

### 6. Pre-flight prompts

The two base-control questions below (plus, when the workflow opts
in, the §6b2 tuning and §6b3 step-selection flows — those are
multi-question flows, not single prompts). For the base controls: up
to two `AskUserQuestion`s in sequence (or batched — your call;
batching is fine since answers are independent). Each is skipped
when the workflow's `preflight:` block pinned the answer in §5g.
Pinned answers are logged (`Pre-flight pin: control-mode=wave-sync
(declared by workflow).`) so the user knows why the prompt didn't
appear.

**6a. Control mode (skip if pinned):**

- **`CONTROL_MODE=prompt`** → run the prompt below.
- **`CONTROL_MODE=wave-sync`** → skip; set the answer to `wave-sync`.
- **`CONTROL_MODE=synchronous`** → skip; set the answer to `synchronous`.
- **`CONTROL_MODE=auto-advance`** → skip; set the answer to `auto-advance`.

Prompt:

- Question: `How should the workflow run control progress?`
- Options:
  - `Wave-sync (recommended)` — `Run one wave of steps, pause for me between waves. I can ask questions, steer, or abort mid-flight. Approval gates use AskUserQuestion.`
  - `Synchronous` — `Run end-to-end without stopping. Approval gates are auto-approved (picking this IS the approval). Step output goes to per-step log files under the run dir, not the chat — tail state.yaml if you want to watch progress.`
  - `Auto-advance` — `Run waves back-to-back without a between-wave menu, but still prompt at steps that need input (asks, approvals, AskUserQuestion inside interactive steps). Best when the workflow's only stops should be its own questions.`

**6b/6b2/6b3. Worktree + the two opt-in questionaries, and 6c. Collect
workflow inputs.** Follow `${CLAUDE_PLUGIN_ROOT}/skills/wise-workflow-run/references/preflight.md` §6b–§6c (already read
in 5b). 6b prompts for the worktree unless pinned; 6b2/6b3 run the
model-tuning and step-selection questionaries when the pre-flight pins
ask for them; 6c collects and validates the workflow's declared
inputs (positional split, choice-input batching, `record-output`
persistence).

### 7. Resolve the project

`wise` keeps no persisted project registry — the project a run
operates on is derived from the current context. Read the workflow
definition's `project-selection`:

- `any` → `project = null`.
- `current` (default) → **auto-detect from the current directory**:
  - `path` = `git rev-parse --show-toplevel` (fall back to `$(pwd)`
    when not inside a git repo);
  - `name` = the basename of `path` (or the `owner/repo` slug from
    `git remote get-url origin` when one is set);
  - `kind` = inferred from the repo's contents — `go.mod` → `backend`,
    a `package.json` with a React/React-Native dependency → `frontend`,
    both backend and frontend markers → `fullstack`, otherwise `other`.
- `prompt` → **ask the user** with `AskUserQuestion`: confirm the
  auto-detected `path`/`name`/`kind` above, or let them override each
  field (the "Other" free-text option accepts a path/name/kind the
  detection missed).

Let `project_json` be the resolved `{path, name, kind}` or the string
`null`.

### 8. Finalise the run (worktree + start-run)

If the user picked a worktree at [§6](#6-pre-flight-prompts)b, create it now:

```bash
WT_DIR="<project.path>.wise-$RUN_ID"
git -C "<project.path>" worktree add "$WT_DIR" -b "wise/<workflow-name>-$RUN_ID"
```

(`<project.path>` is the resolved project path from §7, not a shell
variable — substitute the literal path. `${project.path}` is not valid
shell expansion.)

On git failure, fall back to the original `project.path` and pass
`worktree: null` below; tell the user the worktree couldn't be
created and why. If the worktree WAS created, override
`project.path` to `WT_DIR` in the payload below.

If the worktree was created, carry over any `.worktreeinclude` files
from the original base repo into it — `git worktree add` checks out
only tracked files, so untracked artifacts a tree needs to run
(`.env`, local config) would otherwise be missing:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" \
  apply-worktree-include "<project.path>" "$WT_DIR" || true
```

(`<project.path>` is the ORIGINAL base path from §7 — the source of the
`.worktreeinclude` file and the files it lists — not `$WT_DIR`. Run
this BEFORE overriding `project.path` below. The helper is best-effort:
no `.worktreeinclude`, a non-git base, or a missing listed path are all
silent no-ops and never abort the run, so the `|| true` is belt-and-braces.)

Fold the pre-flight answers into `state.yaml` (flipping
`status: initializing` → `status: running`) via `start-run`:

```bash
CTX='{"control_mode":"<mode>","worktree":<wt-json-or-null>,"project":<project-json-or-null>,"inputs":<inputs-json-or-empty-object>}'
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" start-run "$STATE" "$CTX"
```

(`jq` isn't in `allowed-tools`; construct the JSON yourself as a
literal string from the values you already have. Omit `inputs` or
pass `{}` when the workflow declared none — the engine tolerates
either. Example:
`'{"control_mode":"wave-sync","worktree":null,"project":{"path":"/path/to/project","name":"project","kind":"frontend"},"inputs":{"ticket_id":"TICKET-123"}}'`.)

### 9. Initial TodoWrite

Read the definition's `steps` list. `TodoWrite` one todo per step:

```
{ subject: "<step.id>: <short summary>", activeForm: "Running <step.id>", status: "pending" }
```

Summary hint per type: skill → the skill name; prompt → first 40
chars of the prompt; bash → first 40 chars of the command; approval
→ "awaiting approval"; ask → `ask: <def.output>`. A step §6b3
pre-marked `skipped` gets its todo created as `cancelled` (suffix
the subject with `— deselected at pre-flight`).

Tell the user: `Run <RUN_ID> started. state.yaml: <path>.`

### 10. Main loop

Repeat until a terminal state is reached in [§11](#11-finalise).

**IMPORTANT — turn continuity.** The main loop runs inside a single
Claude Code conversational turn. A turn ends as soon as you emit a
message that does **not** contain a tool call. That will **stall the
run** — the user will see the last output and nothing else, with no
prompt to continue.

The rule is simple and applies equally in wave-sync and synchronous
mode: **every message in the main loop must end with a tool call**.
Prose is fine — encouraged, even, because it's how the user sees
progress — as long as the message it lives in also contains the tool
call that moves the run forward.

Concretely:

- Announcement prose (10d), step-outcome prose (10e), skip-report prose
  (10b), and the wave-sync menu summary (10g) all go into the **same
  message** as the tool calls that follow them. Never emit a bare
  text-only message mid-loop.
- After every `update-step` bookkeeping batch, your very next tool
  call is either (a) the next-wave Bash probe, or (b) the finalise
  sequence in [§11](#11-finalise) if the wave just completed pushed
  the run terminal. The bookkeeping + the following tool call can
  live in one message; they don't have to be split.
- If you find yourself about to write "Step X complete. Running step
  Y…" as prose and realise no tool call is following in the same
  message, either bundle the next `next-wave` call into the same
  message or drop the prose. The run's progress is reported *at* the
  moment of state change (10d and 10e), not between waves.

**10a. Ask what's next.**

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" next-wave "$DEF" "$STATE"
```

Parse the JSON:

- `runnable: []` — list of step descriptors (each has `id`, `type`,
  `definition` — the def merged with templates expanded).
- `to_skip: []` — step ids that should be marked `skipped` per their
  `trigger-rule` (e.g. a dep failed and the rule is `all-success`).
- Optional `terminal: "completed" | "failed"` — when runnable and
  to_skip are both empty AND no steps are `running` in state.

**10b. Apply skips.**

When `to_skip` is non-empty, emit a prose preamble naming each
skipped step and why, then perform the bookkeeping in the SAME
message:

```
Skipping:
  - ⊘ <step.id> (<type>) — <trigger-rule> not met: dep <dep-id>
    is <dep-status>
  - …
```

In the same message, for each id in `to_skip`:

```bash
python3 .../workflows.py update-step "$STATE" <id> status=skipped
```

And `TodoWrite` to mark the matching todos `cancelled`. Do **not**
emit the skip prose as a standalone message — it must share a
message with the tool calls that follow.

**10c. Handle terminal.**

If `terminal` is set, jump to [§11](#11-finalise).

**10d. Run the wave.**

Build ONE message with:

1. A prose announcement naming each step about to run.
2. The per-step `update-step` bookkeeping that transitions each to
   `status: running`.
3. The per-step execution tool calls (dispatched concurrently).

The announcement format:

```
Wave <N> — <count> step(s):
  - ▶ <step.id> (<type>): <one-line description>
  - ▶ <step.id> (<type>): <one-line description>
```

`<N>` is a simple counter the conductor increments per wave (1 for
the first wave, 2 for the second, …). Don't persist it in state; it's
display-only and doesn't survive resume.

The one-line description is generated from the step definition:

| Type | Description |
|---|---|
| `skill`    | `invoke <def.skill>` + first non-empty payload key if any |
| `prompt`   | first 60 chars of rendered `def.prompt` (one line, ellipsise); append ` [→ wise:<role>]` when an agent resolves |
| `supervised-prompt` | as `prompt`, plus ` [supervised]` (runs as a watched background worker) |
| `bash`     | `$ ` + first 60 chars of rendered `def.command` + `(cwd: …)` if `def.cwd` is set |
| `approval` | `approval: ` + first 60 chars of rendered `def.message` |

Then, in the same message, for each runnable step allocate a
step-run-ulid and transition its state:

```bash
SID_RUN=$(python3 .../workflows.py new-ulid)
python3 .../workflows.py update-step "$STATE" <step.id> \
  status=running run_id=$SID_RUN started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  log="logs/<step.id>.$SID_RUN.log"
```

And dispatch each step's executor — **all in the same message**
(multiple tool calls in one message execute concurrently per Claude
Code's tool-use docs). Per step type:

**Resolving agent / model / effort (prompt steps only).** Before
dispatching a `type: prompt` step, resolve its roster binding in one call:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" resolve-team "$DEF" "<step.id>"
```

**Tuning override (only when the run recorded one).** Binding a
recorded `tuning_<group>` choice at dispatch is defined in
`${CLAUDE_PLUGIN_ROOT}/skills/wise-workflow-run/references/roster-resolution.md` — read it (with the model/effort rules
below) the first time a wave dispatches a `type: prompt` step.


Branch on `mode`:

- **`errors` non-empty** → do NOT dispatch. Fail the step loudly with the
  error text on its 10e line + log. An unknown role, a policy keyword
  (`auto`/`off`) used as a team member, or two leads is an authoring bug —
  silently dispatching the wrong agent is worse than failing.
- **`mode: unset`** → the step pinned no `agent:`; apply the workflow's
  top-level `agents:` policy (read once from `$DEF`; default `off`). `off` →
  one `general-purpose` subagent; `auto` → roster-match (next).
- **`mode: off`** → one `general-purpose` subagent.
- **`mode: auto`** → pick the best-fit roster role. Cache the roster **once
  per run**: on the first `auto`/policy-auto step, shell
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" list-agents` once,
  hold the JSON (`{name, description, tools, model, effort}`) in working
  memory, reuse it for every later wave/step — never re-shell per wave.
  Match the step's rendered prompt intent against the `description`s AND
  check `tools` cover what the step does: **writes/edits files** needs
  `Write`/`Edit`; **runs shell or git** needs `Bash`; a step needing a tool
  **no role has** (e.g. `TeamCreate`, a tracker MCP) fits none. **When no
  role both matches the intent and covers the required tools, fall back to
  `general-purpose`.** Resolve its model with `resolve-model
  "<step.model or 'inherit'>" "<step.effort or ''>"`.
- **`mode: single`** → dispatch `members[0]` as one `wise:<role>` Task.
- **`mode: team`** → dispatch every member and synthesize (team flow below).

**Model + availability, and effort (per member).** Follow
`${CLAUDE_PLUGIN_ROOT}/skills/wise-workflow-run/references/roster-resolution.md` — model is a native Task param (omit on
`inherit`, retry once on `next_fallback`); effort is a prompt
directive only, appended from the RESOLVED value.

- `type: skill`:
  ```
  Skill({
    skill: "<def.skill>",
    args: <def.payload as key: value per line, rendered>
  })
  ```

- `type: prompt` — **single agent** (`mode` off / auto / single → one member):
  ```
  Task({
    subagent_type: <"wise:<role>" | "general-purpose">,
    description: "Workflow step <step.id>",
    model: <member.model — include ONLY if not "inherit">,
    prompt: "<rendered def.prompt>"
            + [if member.effort] "\n\nReason at <EFFORT> effort — <gloss>."
            + [if def.until]  "\n\nEnd your last line with a value that matches the regex /<until>/ so the conductor can capture it."
  })
  ```
  If `def.outputs` is set, remember the list so you can extract on
  return. Remember the resolved `agent` / `model` / `effort` too — 10e
  reports them.

- `type: prompt` — **team** (`mode: team`), `type: supervised-prompt`,
  and `type: interactive`: dispatch per
  `${CLAUDE_PLUGIN_ROOT}/skills/wise-workflow-run/references/step-types.md` — read that file the FIRST time a wave
  contains any of these types (a run that never uses them never loads
  it), then follow its recipe for the type.

- `type: bash`:
  ```
  Bash({
    command: "<def.command>",
    description: "Workflow step <step.id>",
    run_in_background: <true if timeout > 30>,
    timeout: <def.timeout * 1000>,
    ...with explicit working-directory handled by prefixing `cd <def.cwd> && <command>`
    (the Bash tool has no cwd field; the engine.sh pattern is to use
    `cd <path> && ...` which is already in the allowed-tools grant).
  })
  ```

- `type: approval` / `type: ask`: dispatch per
  `${CLAUDE_PLUGIN_ROOT}/skills/wise-workflow-run/references/step-types.md` (read it the first time a wave contains one of
  these types).

**10e. Collect and score.**

After the wave's tool results come back, build ONE message that
reports every step's outcome (prose) AND performs all the
bookkeeping (tool calls). Structure:

1. Prose preamble — one line per step, in the same order 10d
   announced them:

   ```
   Wave <N> results:
     - ✓ <step.id>: <one-line outcome>
     - ✗ <step.id>: <one-line failure reason>
     - ⊘ <step.id>: <skip reason>
   ```

   Outcome wording per type:

   | Type | On success | On failure |
   |---|---|---|
   | `skill`    | `<last message first sentence, truncated to ~80 chars>` | `error: <error text, truncated>` |
   | `prompt`   | `captured <name>=<value>` for each `def.outputs`; else `ok (<duration>s)` | `no match for until:/<regex>/ after <iterations>` or subagent error |
   | `bash`     | `exit 0 in <duration>s` + first matched group of stdout if `stdout_matches` captures | `exit <code>: <first line of stderr, truncated>` |
   | `approval` | `approved` (wave-sync) or `auto-approved` (sync) | `rejected by user` |

   For a `prompt` step dispatched to a roster agent or with a `model:` /
   `effort:` override, append the resolved knobs to its outcome line —
   `[agent=wise:<role> model=<m> effort=<e>]`, omitting any that were not
   set — and write the same to the step log so the routing is auditable.
   For a **team** step, list each member and mark the lead —
   `[team=wise:<lead>*, wise:<role2>, wise:<role3> | synthesized]` (the `*`
   flags the lead; `synthesized` notes the conductor merged them) — and write
   each member's `model`/`effort` to the step log. When a member's
   `resolve-team`/`resolve-model` `reason` is non-null (a retired-id swap or an
   effort clamp), append `; <role>: <reason>` so the substitution is visible in
   chat, not just the log.

   Keep each step's line under ~100 chars — if the "one-line" needs
   more room, point at the log file instead (`see logs/<id>.<run-id>.log`).

   **Surface the step's output when declared.** If the step def
   includes a `surface:` field, render the requested content
   inline *immediately after* the step's one-line outcome, so the
   user can review it without opening a file. Shapes:

   ```yaml
   surface:
     file: <output-name>        # read the file at state.outputs[<output-name>]
     label: "Drafted PR body"   # optional prefix shown above the content
     max-lines: 400             # optional cap; default 400
   ```

   Rendering:

   ```
     - ✓ <step.id>: <one-line outcome>

     <label> (<N> lines from <path>):

     ```markdown
     <file contents, truncated to max-lines; add "… (<X> more lines)" line if truncated>
     ```
   ```

   If `state.outputs[<output-name>]` is unset or the file is
   missing/unreadable, render `<surface failed: <reason>>` in
   place of the block — don't fail the step over a surface miss.
   The content goes to the main chat message, not the step log
   (the log already captured its bounded excerpt of the output). Only
   declare `surface:` when the content is genuinely worth reading
   without extra clicks (drafted text the user has to approve,
   small generated reports, etc.) — long binary or noisy outputs
   should stay in the log file.

2. For each step in the wave, in the same message:

   a. Determine success per type:
      - `skill`: tool result returned without an error. On error,
        capture the error text.
      - `prompt`: extract the final assistant message. If
        `def.until` is set, run the regex against the final message;
        success only if a group matches. If `def.outputs` is
        non-empty, capture the group(s) and `record-output` each.
      - `bash`: interpret the tool result's exit code and output.
        Apply `def.success.exit_code` (must match) and any
        `stdout_matches` regex.
      - `approval`: the user's pick (`Approve` → completed, `Reject`
        → failed).

   b. Append a bounded excerpt of the tool output to the step's log
      file. Use the `workflows.py write-log` subcommand, piping the
      output to stdin — NOT the `Write` tool, which prompts Claude
      Code's per-file permission dialog on first write.

      Bound the excerpt: when the output is 120 lines or fewer, log
      it whole; otherwise log the FIRST 60 lines, then a marker line
      `[... truncated <N> lines — full output was in the conductor
      transcript]`, then the LAST 60 lines (verdict/final lines
      always live at the tail, so they are always captured).
      Re-emitting the full output would double every step's token
      cost in this conversation for no gain — the log is a debugging
      aid, not the artifact channel (steps persist real artifacts to
      files themselves).

      ```bash
      python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" write-log \
        "$RUN_DIR" "<step.id>" "<step-run-ulid>" <<'WISE_LOG_EOF'
      <the bounded excerpt — first 60 / marker / last 60 lines>
      WISE_LOG_EOF
      ```

      The `Bash(${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py:*)` grant
      already in `allowed-tools` covers this invocation, so every
      subsequent log write runs silently. If the output content
      might contain the literal token `WISE_LOG_EOF`, pick a
      different heredoc delimiter (e.g. `WISE_LOG_<random 6 chars>`).

   c. `update-step status=<completed|failed|skipped> completed_at=<utc>` —
      plus `error=<short text>` if failed.

   d. `TodoWrite` to update the matching todo (`completed` or
      `cancelled`).

**10f. Persist the run.**

`update-run last_activity_at=<utc>` is handled by every `update-step`
call — you don't need a separate write.

**10g. Yield (wave-sync only).**

The between-wave user-control menu fires in **`wave-sync` only**.
`synchronous` and `auto-advance` both skip 10g and chain straight to
the next wave (see the skip branch below).

If `state.control_mode == "wave-sync"`:

10e already printed each step's outcome in-chat, so don't re-summarise
here. Optionally tack one line on noting what's likely next (the ids
the next `next-wave` call will surface — cheap to predict from
`depends_on`). Then call `AskUserQuestion` in the SAME message:

- Options: `Continue`, `Pause (you'll resume later)`, `Abort run`, `Modify (freeform instruction)`.

- `Continue` → loop to 10a.
- `Pause` → `update-run status=paused`; print resume command:
  ```
  Resume later with:
    /wise-workflow-resume <RUN_ID>
  ```
  Stop.
- `Abort` → `update-run status=cancelled completed_at=<utc>`; jump to [§11](#11-finalise).
- `Modify` → accept free-form instruction ("skip step X", "edit
  prompt of Y and re-run", …). Interpret it against the current
  state: for a skip, `update-step <id> status=skipped`; for an edit,
  update the in-memory definition and continue. Go back to 10a. If
  you cannot interpret the instruction safely, surface what you'd
  do and ask again.

If `state.control_mode == "synchronous"` **or
`state.control_mode == "auto-advance"`**, skip 10g entirely. Don't
emit a "proceeding" line or duplicate of 10e's results — but DO
bundle your next `next-wave` Bash call into the **same message** as
10e's results and bookkeeping. That single message is the heartbeat
of these modes: it reports what the wave did AND kicks off the next
wave's probe, all while keeping the turn open.

In other words, both these modes have the same per-step reporting as
wave-sync (thanks to 10d's announcements and 10e's outcome lines);
what they skip is only the between-waves user-control menu (10g's
`AskUserQuestion`). They never have a moment where prose is the
last thing in the message — the next tool call is always there.

The difference between the two: `synchronous` also suppresses every
in-step prompt (approvals auto-approve, asks record empty,
interactive steps don't call `AskUserQuestion`), so it runs fully
unattended. `auto-advance` keeps all those in-step prompts — it only
drops the between-wave menu, so the run still stops wherever a step
genuinely needs the user's input.

### 11. Finalise

Terminal branch:

```bash
python3 .../workflows.py update-run "$STATE" status=<completed|failed|cancelled> completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

Print a summary:

```
Run <RUN_ID> — <status>.
Workflow: <name>.
Duration: <mm:ss>.
State: <path to state.yaml>.
Logs:   <path to logs dir>.
[if worktree] Worktree at <path> — still there; prune with `git worktree remove` when ready.
```

## Guardrails

- Never invoke the `wise:wise` natural-language helper or another
  `wise-workflow-*` skill. The only Skill invocations this skill is
  allowed to make are the `type: skill` steps of a validated
  workflow.
- Never write outside the run directory and (when requested) the
  worktree path.
- Never skip the `next-wave` round-trip — do not compute runnable
  steps yourself in-conversation. The script is the source of truth
  for readiness.
- Never reorder or re-rank a wave — dispatch all runnable steps
  together, in one message.
- Never swallow step output silently — every step's output is written
  to `logs/<id>.<step-run-ulid>.log` as a bounded excerpt (first 60 /
  truncation marker / last 60 lines, per §10e(b)); the full output
  stays visible in the conductor's own transcript for this turn, it is
  just not all persisted to the log file. The in-chat outcome line in
  10e is a summary; the log file's excerpt is the durable record after
  the turn ends, and 10e should name the log when the summary can't fit.
- Every message in the main loop **must end with a tool call** — the
  full turn-continuity rule is §10. A trailing text-only message stalls
  the run, especially across a between-wave transition in synchronous
  or auto-advance mode, where 10g is skipped so there is no
  `AskUserQuestion` to prompt the user back in.
- On `Modify`, changes are ephemeral: apply them in state.yaml if
  persistent, but never rewrite the definition YAML (the user's
  definitions directory is only mutated by `wise-workflow-create` and
  `wise-workflow-remove`).
