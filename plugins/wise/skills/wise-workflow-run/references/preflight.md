# wise-workflow-run/references/preflight — session tagging, questionaries, inputs

Companion to the conductor SKILL.md, read ONCE per run — right before
§5's sub-steps need it and never inside the main loop. It carries the
pre-flight machinery the loop itself never revisits: session
capture/label/claim (§5b–§5d), run-dir pruning (§5f), the session
rename prompt (§5h), the worktree prompt and the two opt-in
questionaries (§6b/§6b2/§6b3), and workflow-input collection (§6c).
Schema/semantics live in `docs/wise/workflows.md`; this file is the
imperative procedure.

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

A session that already hosts a *live* non-terminal run cannot cleanly
host a second one — `/resume <session>` would return the user to a
conductor whose loop belongs to whichever run renamed the session
most recently, not whichever the user actually wanted. But a run that
was abandoned mid-flight stays non-terminal (`running`/`paused`/
`failed`) forever; it is not a live conflict and must not block a new
run. Probe:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" \
  find-runs-by-session "$SESSION_ID"
```

Each stdout line is
`<run-id>\t<workflow-name>\t<status>\t<last-activity>\t<fresh|stale>`
for a non-terminal run in this workspace claiming the same session.
The 5th field is the genuine-conflict signal:

- **`fresh`** — the run checked in recently; the user is interrupting
  an in-flight conductor. This is the real conflict.
- **`stale`** — the run's activity froze long ago (abandoned). Not a
  conflict. The classification (idle threshold, `WISE_SESSION_STALE_SECS`,
  default 30 min) is owned by `workflows.py`; do not second-guess it.

Only prompt when **at least one match is `fresh`**. Then `AskUserQuestion`:

- Question: `This Claude Code session already has another running
  workflow (<run-id>, <workflow-name>, <status>). Starting a second
  one means /resume won't cleanly return to either. Continue?`
- Header: `Session conflict`
- Options:
  - `Continue anyway — both runs share the session.` — proceed.
  - `Abort this run.` — stop without writing state.

When every match is `stale` (or there are none), do **not** prompt —
proceed straight to 5e. If any stale matches were present, drop a
single informational line first (not a question), e.g. `Note: a prior
run in this session (<run-id>) looks abandoned (idle since
<last-activity>); proceeding.` so the reclaim is visible.

Skip 5d entirely when `SESSION_ID` is `null` (nothing to conflict
with) or when stdout was empty.


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

**6b2. Model/effort tuning questionary (only if `TUNING=prompt`):**

Fetch the workflow's tuning groups:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" get-tuning "$DEF"
```

→ JSON `{groups: [{id, label, steps: [{id, model, effort}], default?}]}`.
Exit 2 (an `INVALID:` authoring error) or an empty `groups` list →
log a `WARN:` line and skip this subsection; a broken tuning block
never blocks the run.

The flow is **two-level** so the common case stays one click:

1. **Level 1 — profile.** One `AskUserQuestion`:
   - Question: `Model/effort profile for this run?`
   - Header: `Tuning`
   - Options:
     - `Defaults (as tuned) (Recommended)` — `Run every step group on
       the workflow's declared model/effort defaults:` + one line per
       group rendered from the JSON it already carries: the `default`
       string for an advisory group, else the per-step pins (collapse
       when uniform — `opus/high`; list the outliers when mixed —
       `opus/high (codebase-audit: sonnet/high)`).
     - `Economy` — `Run every tunable group on sonnet at high effort —
       cheaper and faster, less planning depth.`
     - `Custom` — `Pick model + effort per step group in a follow-up
       question.`
2. **Level 2 — per group (only on `Custom`).** One composite
   `AskUserQuestion` call, one question per group (workflows keep
   these ≤4 by design). Per question:
   - Question: `Model/effort for: <group.label>?`
   - Header: the group id (truncated to 12 chars)
   - Options: `Keep default (<current model/effort summary>)` /
     `Opus · high` / `Sonnet · high` / `Sonnet · low`. `Other`
     accepts a free-text `<model> <effort>` pair — resolve it through
     `resolve-model "<model>" "<effort>"` BEFORE recording, so a
     typo'd model or unsupported effort is caught (and clamped) here
     rather than flowing into dispatch as binding config.

Resolve each group to either `default` or a concrete
`<model> / <effort>` pair, then persist the choices into run state —
one `record-output` per group plus the summary, **chained in a single
Bash invocation** (each call rewrites state.yaml; N separate tool
calls would be N round-trips for one logical mutation). The run stub
exists since §4, so `record-output` works; outputs survive resume and
render into `{{…}}` templates:

```bash
python3 .../workflows.py record-output "$STATE" tuning_<group.id> "<default | model / effort>" \
  && python3 .../workflows.py record-output "$STATE" tuning_<group2.id> "…" \
  && python3 .../workflows.py record-output "$STATE" tuning_summary "<one line: '<id>: <choice>; …' or 'all defaults'>"
```

The per-group `tuning_<id>` outputs are the machine channel (dispatch
overrides, `{{tuning_<id>}}` templates); `tuning_summary` is
display-only.

Log the result (`Pre-flight tuning: authoring=sonnet/high, rest
default.`). When `TUNING=skip`, record nothing — templates that
reference `{{tuning_summary}}` render as a raw placeholder and the
workflow's prompts treat that as "defaults stand". How the choice is
applied at dispatch is §9's job (the `resolve-team --model/--effort`
override for step-bound groups; the `{{tuning_<id>}}` /
`{{tuning_summary}}` outputs for advisory groups with no `steps:`).

**6b3. Step-selection questionary (only if `STEP_SELECT=prompt`):**

Fetch the workflow's optional stages + presets:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" get-step-select "$DEF"
```

→ JSON `{optional: [{id, label, steps, ask-group?}], presets: [{id,
label, description?, skip}]}`. Exit 2 or an empty `optional` list →
`WARN:` and skip this subsection.

1. **Level 1 — preset.** One `AskUserQuestion`:
   - Question: `Which stages should this run include?`
   - Header: `Stages`
   - Options: `Full — run everything (Recommended)` first, then one
     option per declared preset (its `description`, plus the skipped
     stage labels), then `Custom` — `Pick the optional stages to skip
     in a follow-up question.`
2. **Level 2 — per stage (only on `Custom`).** One composite
   `AskUserQuestion` call with `multiSelect: true` questions — one
   question per distinct `ask-group` (entries without one share a
   question, chunked ≤4 options), listing that group's stages:
   - Question: `<ask-group>: which optional stages should be SKIPPED?
     (select none to run them all)`
   - Options: one per stage — label = `entry.label`, description
     names the step ids it covers.

Resolve the final set of deselected stage ids, then **pre-mark every
covered step as skipped** in run state so the scheduler never
launches them (the workflow's `trigger-rule`s — typically
`none-failed` on the consolidation steps — are authored to flow past
user-skipped dependencies):

```bash
python3 .../workflows.py update-step "$STATE" <step-id-1> status=skipped \
  && python3 .../workflows.py update-step "$STATE" <step-id-2> status=skipped \
  && python3 .../workflows.py record-output "$STATE" skipped_stages "<comma-joined stage ids, or 'none'>"
```

(One chained Bash invocation for all the marks — not one tool call
per step.)

Log it (`Pre-flight step-select: preset=standard — skipping
deep-dive (research-context).`). The pre-marked statuses live in
`state.yaml`, so a resumed run keeps the selection with no extra
bookkeeping. When `STEP_SELECT=skip`, every step runs as authored.

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

stdout is a JSON array of `{name, prompt, validate?, extract?,
optional?, options?, default?}`. If the array is empty, skip this
section entirely and continue to [§7](#7-resolve-the-project).

**First, split the positional remainder.** Map `ARG_REST` (from §2)
onto the declared inputs in order: each input consumes one
whitespace-separated token, EXCEPT the **last declared input, which
absorbs the entire remaining substring verbatim** (inner spaces
preserved) — that is what lets a trailing free-form prompt arrive
intact. Build a `positional[<name>]` map of the values you extracted; an
input with no token (including every input when `ARG_REST` is empty)
stays absent from it.

Then, for each input in order:

1. **Resolve the value.** Three cases:
   - **Supplied positionally** (`<name>` is in `positional`) → take
     that as the raw answer and **do not** `AskUserQuestion`; go
     straight to validation in step 2.
   - **Absent and `optional: true`** → default to the empty string,
     store it as `inputs["<name>"]`, and skip both the prompt and
     validation (an optional input the operator didn't supply is just
     blank — its step templates resolve `{{<name>}}` to empty).
   - **Absent and required** → `AskUserQuestion`:
     - Question: the input's `prompt` text.
     - Header: the input's `name` (truncated to 12 chars).
     - **Free-text input** (no `options` declared) → Options: `Other`
       only — the user types the value via free text. (Declaring only
       `Other` satisfies AskUserQuestion's minimum of two options by
       including the implicit "Other" affordance; if the harness
       rejects single-option calls, add a trailing `Cancel run` option
       and abort cleanly when picked.)
     - **Choice input** (`options` declared) → one option per entry:
       label = the entry's `label` (or its `value`), description = the
       entry's `description`. List the `default` value's option first.
       The recorded answer is the chosen option's **`value`** (not its
       label); `Other` free text is allowed and goes through the same
       validation as any typed value.
     - **Batching:** a RUN of consecutive choice inputs is asked as
       ONE composite `AskUserQuestion` call (up to 4 questions per
       call; overflow continues in a next call) — that is the
       "configure everything up front" questionary UX. Free-text
       inputs stay one-per-call as before.

2. Validate + extract via the engine — empty strings for regexes
   the input didn't declare (run this for positionally-supplied and
   prompted values alike; skip it only for the optional-default-empty
   case above):

   ```bash
   CLEAN=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" \
     validate-input "<raw-answer>" "<extract-or-empty>" "<validate-or-empty>")
   ```

   Exit 0 → `CLEAN` holds the cleaned value; store it as
   `inputs["<name>"]`.

   Exit 2 → stderr carries `INVALID:<reason>`. Re-ask the input with
   the reason inlined into the prompt — via `AskUserQuestion` even if
   the rejected value came in positionally (a bad CLI value falls back
   to an interactive fix rather than aborting outright). Cap at 3
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

