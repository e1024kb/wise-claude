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

**6b-silent. Pinned-skip profile apply (when `TUNING=skip`):** §6b2 and
§6b3 below are gated on their `prompt` pins and never run for a
workflow that pins them `skip` — so evaluate THIS rule first, outside
both: when the workflow declares a `profiles:` block AND both
`TUNING=skip` and `STEP_SELECT=skip` are pinned, ask nothing and apply
the stored session profile's mapping silently (run §6b2's step 2
persistence — same `record-output "$STATE"` outputs as a level pick).
A workflow that pins `tuning: skip` without a `profiles:` block keeps
today's behavior — nothing recorded, declared defaults stand.

**6b2. Profile & tuning questionary (only if `TUNING=prompt`):**

Fetch — in ONE chained Bash — the workflow's profiles, tuning groups,
step-select block, and the session's stored budget profile:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" get-profiles "$DEF"; \
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" get-tuning "$DEF"; \
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" get-step-select "$DEF"; \
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" profile-get
```

`get-tuning` exit 2 (an `INVALID:` authoring error) → log a `WARN:`
line and skip this subsection entirely; a broken tuning block never
blocks the run. An empty `groups` list is NOT an error — it only means
the workflow declares no tunable groups, and it must not skip the rest
of §6b2: continue to the fork below so `step-preset`/`skip`,
`team-mode`, and `caps` from a `profiles:` block can still apply, just
with nothing to ask in the per-group tuning questions. `get-profiles`
exit 2 → `WARN:` and run the LEGACY flow below (as if the workflow
declared no `profiles:`).
`profile-get` prints the session level (`low|medium|max`; it never
fails — missing store = `medium`).

**Fork on the `profiles` JSON:**

- **`profiles` empty (workflow declares none) → LEGACY flow**: run the
  two-level tuning questionary exactly as before — Level 1
  `Defaults (as tuned) (Recommended)` / `Economy` (sonnet · high
  everywhere) / `Custom`, Level 2 per-group on Custom — and then §6b3
  as its own questionary. (This is the pre-profiles behavior,
  unchanged, for user-authored workflows.)

- **`profiles` present → PROFILE-FIRST flow** (the bundled workflows):

  1. **Q1 — one `AskUserQuestion`** (this single question replaces the
     legacy Level 1 AND §6b3's preset question):
     - Question: `Budget profile for this run?`
     - Header: `Profile`
     - Options (exactly 4). With a stored session profile `<l>`:
       `Session profile: <l> — keep (Recommended)` first, then the
       other two levels, then `Custom (per-step)`. Without one:
       `low` / `medium (Recommended)` / `max` / `Custom (per-step)`.
       Each level's description renders what it changes for THIS
       workflow from the `profiles` JSON (tuning tiers, skipped
       stages via its step-preset/skip, team-mode, caps) — and always
       ends with `Correctness rules unchanged.`
  2. **On a level pick** — expand `profiles[<level>]` and persist in
     ONE chained Bash (the run stub exists since §4, so
     `record-output` works; outputs survive resume). A workflow may
     declare a sparse `profiles:` block (e.g. only `medium:`) — Q1
     still offers all three levels, so if the picked level is absent
     from the `profiles` JSON, treat it as `{}`, the same "declared
     defaults, nothing overridden" convention an explicit empty entry
     gets. Never treat a missing level as an error or skip the
     record-output calls below:
     Every call below takes the state path first — the CLI signature is
     `record-output "$STATE" <name> <value>` (matching §6b3's example);
     omitting `"$STATE"` is an argparse error and nothing persists:
     - `record-output "$STATE" run_profile <level>` — always.
     - per `tuning` entry: `record-output "$STATE" tuning_<gid> "<model> / <effort>"`
       (or the literal `default`). An empty tuning map (the `medium`
       convention) records nothing — declared defaults stand.
     - `step-preset` / `skip` → resolve the covered step ids from the
       step-select JSON and pre-mark them exactly as §6b3's
       mechanics describe (`update-step "$STATE" <step-id> status=skipped` +
       `record-output "$STATE" skipped_stages …`); `step-preset: full` or no
       key → no marks. Then SKIP §6b3 entirely — the profile answered
       it.
     - `team-mode` → `record-output "$STATE" team_mode <solo|full>` (§10d
       passes `--team-mode solo` on every `resolve-team` call when
       recorded solo).
     - per cap: `record-output "$STATE" cap_<name> <int>`.
     - `record-output "$STATE" tuning_summary "profile=<level>; <one line of what changed, or 'defaults'>"`.
  3. **On `Custom (per-step)`** — per-step control, three parts:
     a. One composite `AskUserQuestion`, one question per TUNABLE STEP
        (the union of every step-bound group's `steps`; chunk ≤4 per
        call; for a workflow whose groups are advisory — no `steps:`
        binding, e.g. ticket-auto's phases — fall back to one question
        per GROUP, the phases ARE the steps there). Options:
        `Keep default (<pins>)` / `Opus · high` / `Sonnet · high` /
        `Sonnet · low`; `Other` free-text resolved through
        `resolve-model` BEFORE recording. Record only non-default
        answers: `record-output "$STATE" tuning_step_<step-id> "<model> / <effort>"`
        for step-bound picks (normalise `-` in the step id to `_` in
        the output name), `record-output "$STATE" tuning_<gid> …` for
        advisory groups.
     b. Then run §6b3's multiSelect skip questionary as written.
     c. Then, when any tunable step declares a team (`agent:` list):
        one question — `Panel steps: full team or solo lead?` →
        `record-output "$STATE" team_mode <full|solo>`.
     Caps are NOT asked on Custom — declared defaults stand.
     Finish with `record-output "$STATE" run_profile custom` +
     `record-output "$STATE" tuning_summary …`.

The per-group `tuning_<id>` / per-step `tuning_step_<id>` outputs are
the machine channel (dispatch overrides, `{{tuning_<id>}}` /
`{{cap_<name>}}` templates); `tuning_summary` is display-only. Log the
result (`Pre-flight profile: low — sonnet tiers (opus planning/authoring), minimal research, solo panels.`). Dispatch precedence is §10d's job:
`tuning_step_<sid>` > `tuning_<gid>` > declared pins, and
`team_mode=solo` adds `--team-mode solo` to every `resolve-team` call
(when its JSON returns a `collapsed` key, append to the step prompt:
`Solo mode: also cover these dropped lenses briefly: <dropped roles>.`
and log `team collapsed: <from>→1 (<dropped>)`).

**6b3. Step-selection questionary (only if `STEP_SELECT=prompt`, and
only when §6b2 did not already answer it — a profile-level pick
applies its step-preset/skip and SKIPS this subsection; the legacy
flow and the Custom path still run it):**

Fetch the workflow's optional stages + presets (already fetched in
§6b2's chained call — reuse that JSON):

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
   - Options: one per entry — label = `entry.label`, description
     names the step id(s) it covers (bundled workflows author these
     PER-STEP, so this is per-step granularity; a coupled pair like
     ticket-plan's `gaps` stays one entry by design).

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
     - **Free-text input** (no `options` declared) → Options:
       `Provide input` (description: type the value via the free-text
       `Other` affordance) plus a trailing `Cancel run` option (abort
       the run cleanly when picked, same as a validation abort at the
       attempt cap below). `AskUserQuestion` requires 2-4 declared
       options per question; a single implicit `Other` affordance does
       not satisfy that minimum, so always declare both, not just when
       a harness happens to reject the single-option form.
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
   case above). Keep the raw answer OUT of the generated shell source:
   a user- or ticket-supplied string can contain `$(...)`, backticks,
   or quotes that Bash would parse before `validate-input` ever runs.
   Assign it via a quoted heredoc (which suppresses expansion inside
   the body) and reference it as a plain double-quoted variable, never
   splice it into the command text:

   ```bash
   RAW_ANSWER=$(cat <<'WISE_RAW_<random-6-chars>'
   <the raw answer, verbatim, unescaped>
   WISE_RAW_<random-6-chars>
   )
   CLEAN=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" \
     validate-input "$RAW_ANSWER" "<extract-or-empty>" "<validate-or-empty>")
   ```

   The delimiter must be **freshly random per invocation** (e.g.
   `WISE_RAW_k3x9qp`), never a fixed public string: a hostile answer
   containing the delimiter as its own line would terminate the
   heredoc early and have the rest parsed as shell. If the answer
   text happens to contain your chosen delimiter line, pick another
   random one before running.

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

