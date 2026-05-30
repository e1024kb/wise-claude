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
argument-hint: "[<workflow-name>]"
allowed-tools: Read, Write, Skill, AskUserQuestion, TodoWrite, Task, Bash(${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap-deps.sh:*), Bash(${CLAUDE_PLUGIN_ROOT}/scripts/init-registry.py:*), Bash(${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py:*), Bash(bash:*), Bash(python3:*), Bash(mkdir:*), Bash(git:*), Bash(test:*)
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
`workflow-name`. When `$ARGUMENTS` is empty, the skill prompts
interactively in [§2](#2-resolve-the-workflow-name).

- `workflow-name` — matches the filename without `.yaml`. Resolution
  order: user dir first, bundled second. When absent, [§2](#2-resolve-the-workflow-name)
  prompts the user to pick from the available definitions.

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
re-ordering for).

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

**5b. Capture the current Claude Code session UUID:**

```bash
SESSION_ID=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" current-session-id)
```

Exit 2 means no session jsonl was found — rare, usually when running
outside a Claude Code conversation. Treat `SESSION_ID` as the literal
string `null` in the JSON payload below and flag it to the user in
5f's question ("session untagged — resume won't be able to send you
back to this session"). Do not abort.

**5c. Derive the session label:**

```bash
SESSION_LABEL=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" \
  session-label "$RUN_ID" "<workflow-name>")
```

Format is `<run-ulid>_<first-7-hyphen-tokens-of-workflow-name>` —
short enough to fit in the `/resume` picker, long enough to
distinguish concurrent workflow sessions.

**5d. Check for an existing session claim:**

A session that already hosts a non-terminal run *cannot* cleanly
host a second one — `/resume <session>` would return the user to a
conductor whose loop belongs to whichever run renamed the session
most recently, not whichever the user actually wanted. Probe:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" \
  find-runs-by-session "$SESSION_ID"
```

Each stdout line is `<run-id>\t<workflow-name>\t<status>` for a
non-terminal run in this workspace that claims the same session.
If there are any matches, `AskUserQuestion`:

- Question: `This Claude Code session already has another running
  workflow (<run-id>, <workflow-name>, <status>). Starting a second
  one means /resume won't cleanly return to either. Continue?`
- Header: `Session conflict`
- Options:
  - `Continue anyway — both runs share the session.` — proceed.
  - `Abort this run.` — stop without writing state.

Skip 5d's prompt when `SESSION_ID` is `null` (nothing to conflict
with) or when stdout was empty.

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

**5f. Prune old run directories (cap 25):**

Workflow runs accumulate under `$RUNS_ROOT/<run-ulid>/` (which
resolves to `~/.local/share/wise/runs/<cwd-slug>/<run-ulid>/` in the
default layout)
— each one keeps its state.yaml and step log files on disk forever
unless something reclaims them. Cap the per-workspace total at **25**
so history stays bounded:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" prune-runs
```

- Non-terminal runs (`initializing` / `running` / `paused` / `failed`)
  are **protected** — they're resumable and mustn't be thrown away
  just because the cap fits them out. Even if non-terminal alone
  exceeds 25, all are kept.
- Among terminal runs (`completed` / `cancelled`), the oldest by
  `last_activity_at` are deleted first until the total is back at 25
  (or the non-terminals-alone figure, whichever is higher).
- The cap is overridable via the `WISE_RUN_HISTORY_CAP` env var if the
  user wants more history for a given workspace.
- `prune-runs` prints one `PRUNED:<run-id>` line per deletion on
  stdout; mention the count to the user ("Pruned N old runs.") only
  if any deletions occurred — silent when nothing was over the cap.

This is a file-system cleanup only. Claude Code session transcripts
(`~/.claude/projects/<slug>/<uuid>.jsonl`) are NEVER touched — those
belong to the user's Claude Code history, not the wise plugin.

**5g. Read pre-flight pins from the workflow definition:**

Before any of the three pre-flight prompts (rename_session /
control-mode / worktree), read the workflow's `preflight:` block
— it may pin any or all of those answers, in which case the
corresponding prompt is skipped entirely:

```bash
eval "$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" \
  get-preflight "$DEF")"
```

This sets three shell variables:

- `CONTROL_MODE`   — `prompt` (default; ask §6a) | `wave-sync` | `synchronous`
- `WORKTREE`       — `prompt` (default; ask §6b) | `current` | `new`
- `RENAME_SESSION` — `prompt` (default; ask §5h) | `skip`

Missing keys (or a missing `preflight:` block entirely) resolve to
`prompt` — every pre-flight question runs, matching pre-0.42
behaviour. Invalid values emit a `WARN:` line on stderr and fall
back to `prompt` so the workflow still runs.

**5h. Prompt the user to rename the session (skip if pinned):**

If `RENAME_SESSION=skip`, skip this subsection entirely and just log:
`Pre-flight pin: rename_session=skip (declared by workflow).` The
`/resume` picker will show the raw UUID instead of a friendly label.

Otherwise (`RENAME_SESSION=prompt`), print a short intro with the
copy-pasteable rename command, then `AskUserQuestion`. The
question and options are worded as forward-looking atomic actions
— ("rename AND continue") — rather than past-tense checks ("have
you renamed?"), because the latter misreads when the user hasn't
clicked `Continue` yet:

```
This run is tagged as:
  <SESSION_LABEL>

To make the run findable in /resume's picker later, rename the
Claude Code session to match. Run this in a separate message,
then pick "Rename session and continue" below:

  /rename <SESSION_LABEL>
```

- Question: `Rename this Claude Code session for /resume's picker?`
- Header: `Rename`
- Options:
  - `Rename session and continue` — `I've typed /rename in another message; proceed to pre-flight.`
  - `Skip rename and continue` — `Don't rename. Resume still works via UUID, but the /resume picker shows the raw UUID instead of the friendly label.`
  - `Abort this run` — `Mark the stub state cancelled and stop.`

On `Abort this run`:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" update-run \
  "$STATE" status=cancelled completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

### 6. Pre-flight prompts

Up to two `AskUserQuestion`s in sequence (or batched — your call;
batching is fine since answers are independent). Each is skipped
when the workflow's `preflight:` block pinned the answer in §5g.
Pinned answers are logged (`Pre-flight pin: control-mode=wave-sync
(declared by workflow).`) so the user knows why the prompt didn't
appear.

**6a. Control mode (skip if pinned):**

- **`CONTROL_MODE=prompt`** → run the prompt below.
- **`CONTROL_MODE=wave-sync`** → skip; set the answer to `wave-sync`.
- **`CONTROL_MODE=synchronous`** → skip; set the answer to `synchronous`.

Prompt:

- Question: `How should the workflow run control progress?`
- Options:
  - `Wave-sync (recommended)` — `Run one wave of steps, pause for me between waves. I can ask questions, steer, or abort mid-flight. Approval gates use AskUserQuestion.`
  - `Synchronous` — `Run end-to-end without stopping. Approval gates are auto-approved (picking this IS the approval). Step output goes to per-step log files under the run dir, not the chat — tail state.yaml if you want to watch progress.`

**6b. Worktree (skip if pinned):**

- **`WORKTREE=prompt`** → run the prompt below.
- **`WORKTREE=current`** → skip; set the answer to `Current tree`.
- **`WORKTREE=new`** → skip; set the answer to `Dedicated worktree`
  (and go through the worktree-creation path as usual).

Prompt:

- Question: `Run in a dedicated git worktree or in the current tree?`
- Options:
  - `Current tree` — `Use the cwd/project path as-is (default).`
  - `Dedicated worktree` — `Create a sibling worktree at <project-path>.wise-<run-ulid> on branch wise/<name>-<run-ulid>. Leave cleanup to me — I'll tell you the path at the end.`

Store both answers for step 8; they go into `state.yaml` via
`start-run` and persist across resume.

### 6c. Collect workflow inputs

Some workflows declare an `inputs:` section (top-level in the YAML)
listing variables the user must supply before the DAG launches.
Example:

```yaml
inputs:
  - name: ticket_id
    prompt: "Which Jira ticket? (PROJ-18572 or a browse URL)"
    validate: "^[A-Z]+-\\d+$"
    extract: "([A-Z]+-\\d+)"
```

Enumerate the workflow's declared inputs:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" list-inputs "$DEF"
```

stdout is a JSON array of `{name, prompt, validate?, extract?}`. If
the array is empty, skip this section entirely and continue to
[§7](#7-resolve-the-project).

Otherwise, for each input in order:

1. `AskUserQuestion`:
   - Question: the input's `prompt` text.
   - Header: the input's `name` (truncated to 12 chars).
   - Options: `Other` only — the user types the value via free text.
     (Declaring only `Other` satisfies AskUserQuestion's minimum of
     two options by including the implicit "Other" affordance; if
     the harness rejects single-option calls, add a trailing
     `Cancel run` option and abort cleanly when picked.)

2. Validate + extract via the engine — empty strings for regexes
   the input didn't declare:

   ```bash
   CLEAN=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" \
     validate-input "<raw-answer>" "<extract-or-empty>" "<validate-or-empty>")
   ```

   Exit 0 → `CLEAN` holds the cleaned value; store it as
   `inputs["<name>"]`.

   Exit 2 → stderr carries `INVALID:<reason>`. Re-ask the same
   question with the reason inlined into the prompt. Cap at 3
   attempts total; after the third, abort the run:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" update-run \
     "$STATE" status=cancelled completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
   ```
   and tell the user the input couldn't be validated.

Collect the answers into an `inputs_json` dict (e.g.
`{"ticket_id":"PROJ-18572","note":"small bug"}`). Pass it through
to [§8](#8-finalise-the-run-worktree--start-run)'s `start-run`
payload — the engine merges the dict into `state.outputs` so
`{{<name>}}` templates in step definitions resolve the same way
captured step outputs do.

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
→ "awaiting approval"; ask → `ask: <def.output>`.

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

- Announcement prose (9d), step-outcome prose (9e), skip-report prose
  (9b), and the wave-sync menu summary (9g) all go into the **same
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
  moment of state change (9d and 9e), not between waves.

**9a. Ask what's next.**

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

**9b. Apply skips.**

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

**9c. Handle terminal.**

If `terminal` is set, jump to [§11](#11-finalise).

**9d. Run the wave.**

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
| `prompt`   | first 60 chars of rendered `def.prompt` (one line, ellipsise) |
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

- `type: skill`:
  ```
  Skill({
    skill: "<def.skill>",
    args: <def.payload as key: value per line, rendered>
  })
  ```

- `type: prompt`:
  ```
  Task({
    subagent_type: "general-purpose",
    description: "Workflow step <step.id>",
    prompt: "<def.prompt>" + [if def.until] trailing instruction: "End your last line with a value that matches the regex /<until>/ so the conductor can capture it."
  })
  ```
  If `def.outputs` is set, remember the list so you can extract on
  return.

  **Important — `prompt` steps run in an isolated Task subagent.**
  The subagent has its own tool list (Read / Edit / Write / Bash /
  etc.) but **cannot** call `AskUserQuestion` — that tool only
  works in the main conversation. If the step needs to walk the
  user through a per-item wizard, use `type: interactive` below
  instead. A `prompt` step that tries to AskUserQuestion silently
  degrades (the subagent typically falls back to "list the items
  and return a summary"), which is worse than the step failing
  loudly because it looks like the step worked.

- `type: interactive`:

  Runs inline in the conductor's main conversation instead of
  being spawned as a Task subagent. The conductor — that's you —
  reads the step's `prompt` body and executes it directly, with
  full main-thread tool access (Read, Edit, Write, Bash,
  AskUserQuestion, TodoWrite, etc.).

  Use this for step bodies that drive per-item wizards, iterate
  over a variable-length list with user decisions per iteration,
  or otherwise need to prompt the user mid-step. Pre-0.49
  workflows that used `type: prompt` for this pattern quietly
  lost the user-facing prompts (see the warning above) — switch
  them to `interactive`.

  Execution model:
  1. Render `def.prompt` via the template engine (`{{…}}`
     variables) exactly as for a `prompt` step.
  2. Read the rendered prompt in chat and follow it, emitting
     the final line as the prompt's `until:` contract demands.
  3. Capture outputs from the final line's regex groups via
     `record-output` just like a `prompt` step.

  Trade-offs vs `prompt`:
  - Pro: full tool access, AskUserQuestion works, richer dispatch
    logic across turns (since the conductor stays in context).
  - Con: **sequential only**. An interactive step blocks the
    conductor's main conversation until it completes, so two
    interactive steps in the same wave cannot run in parallel
    (one must finish before the other starts). Non-interactive
    parallel steps in the same wave still run via Task as usual.
  - Con: context budget — the conductor accumulates tool output
    into the main conversation, unlike a Task subagent which
    releases its turn transcript back on return. Use for flows
    that genuinely need user interaction; stay on `prompt` for
    anything that can complete unattended.

  **In synchronous mode** an interactive step still runs inline and
  captures its `until:` / `outputs:` exactly as above — `interactive`
  is chosen for main-thread tool access, not for prompting. It just
  must not call `AskUserQuestion` (sync mode is a blanket approval);
  treat any decision the body would prompt for as auto-approved and
  proceed, the same way `approval` steps do. `ticket-auto`'s
  `process-tickets` step depends on this combination — it is
  `interactive` for tool access while the run is `synchronous`.

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

- `type: approval`:

  **In wave-sync mode** — use `AskUserQuestion`:
  ```
  AskUserQuestion({
    question: "<def.message>",
    header: "Approval — <step.id>",
    options: [
      { label: "Approve", description: "Mark this step completed and continue." },
      { label: "Reject",  description: "Mark this step failed and stop the dependent branch." },
    ]
  })
  ```

  **In synchronous mode** — auto-approve. Picking synchronous at
  pre-flight is itself an implicit blanket approval to run the whole
  DAG through without stopping. The step transitions directly to
  `completed`, and a one-line note `[sync auto-approved]` is written
  to its log file so the decision is auditable after the fact. Do
  NOT emit `AskUserQuestion` in sync mode — it would stall the run
  and defeat the point of picking synchronous. If the workflow
  genuinely needs a human gate, the user should have picked
  wave-sync (or the gate should be upgraded to a `type: prompt`
  step that encodes the check as a programmable condition).

- `type: ask`:

  Interactive step that captures an answer from the user and
  records it as a named output so downstream step templates can
  reference it as `{{<output-name>}}`. Two rendering shapes —
  which one you get depends on whether `confirm_label` is
  declared:

  **Shape A — free-text capture** (no `confirm_label`). The
  natural fit for "give me a comment" / "type a value":

  ```yaml
  - id: user-comments
    type: ask
    question: "<question text>"       # required
    header: "<chip label>"            # optional — ≤12 chars, defaults to step id
    output: user_comments             # required — name under state.outputs
    skip_label: "Skip"                # optional — defaults to "Skip"
  ```

  `AskUserQuestion` options:
  - `<def.skip_label or 'Skip'>` — description: `Record an empty value and continue.`
  - `Provide input` — description: `Type your answer via the free-text Other affordance.`

  Map the result:
  - Picked the skip label → `answer = ""`.
  - Picked "Provide input" → the user's Other-text is the answer.
  - The user picked Other directly with text → that text is the
    answer.

  **Shape B — binary choice** (`confirm_label` is declared). The
  natural fit for "yes/no" / "opt-in to this extra stage":

  ```yaml
  - id: ask-watch
    type: ask
    question: "<question text>"       # required
    header: "<chip label>"            # optional
    output: watch_choice              # required
    skip_label: "No — I'll watch manually"       # optional — defaults to "Skip"
    confirm_label: "Yes — watch pipelines"       # required for Shape B
    confirm_value: "yes"                         # optional — defaults to confirm_label verbatim
  ```

  `AskUserQuestion` options:
  - `<def.skip_label>` — description: `Record an empty value and continue.`
  - `<def.confirm_label>` — description: `Record "<def.confirm_value or confirm_label>" and continue.`

  Map the result:
  - Picked the skip label → `answer = ""`.
  - Picked the confirm label → `answer = def.confirm_value` (or
    `def.confirm_label` if `confirm_value` isn't set).
  - The user picked Other directly with text → **ignore** and
    re-prompt. Shape B is deliberately binary; free-text doesn't
    apply.

  Downstream steps gate with `when: "<output> != ''"` or
  `when: "<output> == '<confirm_value>'"`.

  Pick Shape B whenever the question is yes/no — the "Provide
  input" free-text affordance in Shape A misleads users into
  thinking they need to type `yes`.

  Record the answer and mark the step completed (both shapes):

  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" record-output \
    "$STATE" "<def.output>" "<answer>"
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" update-step \
    "$STATE" "<step.id>" status=completed completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  ```

  **In synchronous mode** — no prompt. The step records an empty
  string for `<def.output>` (both shapes) and transitions straight
  to `completed`, with a one-line `[sync skipped ask]` note in its
  log. If the workflow genuinely needs a user answer, the user
  should pick wave-sync at pre-flight.

**9e. Collect and score.**

After the wave's tool results come back, build ONE message that
reports every step's outcome (prose) AND performs all the
bookkeeping (tool calls). Structure:

1. Prose preamble — one line per step, in the same order 9d
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
   (the log already captured the full subagent output). Only
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

   b. Append the full tool output to the step's log file. Use the
      `workflows.py write-log` subcommand, piping the output to
      stdin — NOT the `Write` tool, which prompts Claude Code's
      per-file permission dialog on first write:

      ```bash
      python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" write-log \
        "$RUN_DIR" "<step.id>" "<step-run-ulid>" <<'WISE_LOG_EOF'
      <the full step output — subagent final message / bash stdout+stderr / etc.>
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

**9f. Persist the run.**

`update-run last_activity_at=<utc>` is handled by every `update-step`
call — you don't need a separate write.

**9g. Yield (wave-sync only).**

If `state.control_mode == "wave-sync"`:

9e already printed each step's outcome in-chat, so don't re-summarise
here. Optionally tack one line on noting what's likely next (the ids
the next `next-wave` call will surface — cheap to predict from
`depends_on`). Then call `AskUserQuestion` in the SAME message:

- Options: `Continue`, `Pause (you'll resume later)`, `Abort run`, `Modify (freeform instruction)`.

- `Continue` → loop to 9a.
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
  update the in-memory definition and continue. Go back to 9a. If
  you cannot interpret the instruction safely, surface what you'd
  do and ask again.

If `state.control_mode == "synchronous"`, skip 9g entirely. Don't
emit a "proceeding" line or duplicate of 9e's results — but DO
bundle your next `next-wave` Bash call into the **same message** as
9e's results and bookkeeping. That single message is the heartbeat
of sync mode: it reports what the wave did AND kicks off the next
wave's probe, all while keeping the turn open.

In other words, sync mode's chat has the same per-step reporting as
wave-sync (thanks to 9d's announcements and 9e's outcome lines);
what it skips is only the between-waves user-control menu (9g's
`AskUserQuestion`). Sync mode never has a moment where prose is the
last thing in the message — the next tool call is always there.

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
- Never swallow step output — every step's full output goes to
  `logs/<id>.<step-run-ulid>.log`. The in-chat outcome line in 9e is
  a summary; the log file is the source of truth, and 9e should
  name it when the summary can't fit.
- Every message in the main loop **must end with a tool call** — the
  full turn-continuity rule is §10. A trailing text-only message stalls
  the run, especially in synchronous mode where there is no
  `AskUserQuestion` to prompt the user back in.
- On `Modify`, changes are ephemeral: apply them in state.yaml if
  persistent, but never rewrite the definition YAML (the user's
  definitions directory is only mutated by `wise-workflow-create` and
  `wise-workflow-remove`).
