# wise workflows

A **workflow** is a named, reusable, multi-step procedure that
composes wise actions, third-party skills, shell commands, and
approval gates into a single `/wise-workflow-run <name>` invocation.

Workflows let you codify recipes like "build, test, and open a
release PR" once, then run them repeatably and resumably.

## The commands

| Invocation | Purpose |
|---|---|
| `/wise-workflow-list` | List bundled + user workflows. |
| `/wise-workflow-create <name>` | Wizard to scaffold a new user workflow. |
| `/wise-workflow-run <name>` | Start a run. The main conversation becomes the conductor. |
| `/wise-workflow-resume <run-ulid>` | Continue an interrupted or paused run. |
| `/wise-workflow-status [<run-ulid>]` | List runs in cwd; with arg, dump full state. |
| `/wise-workflow-remove <name>` | Delete a user workflow. Bundled ones are immutable. |

## Where things live

Each `workflows/` root accepts one of two layouts:

- **Folder form (preferred)** — `<root>/<name>/workflow.yaml`. The
  workflow can ship sibling artifacts (see [Workflow artifacts](#workflow-artifacts))
  and address them from steps via `{{workflow.dir}}`.
- **Flat form (legacy)** — `<root>/<name>.yaml`. Still accepted so
  existing user-authored files keep working. No artifacts dir.

Folder form wins on same-root collision.

- **Bundled definitions** — `${CLAUDE_PLUGIN_ROOT}/workflows/<name>/workflow.yaml`
  (or legacy flat `*.yaml`). Ship with the plugin; replaced by reinstall.
- **User definitions** — `${CLAUDE_PLUGIN_DATA}/workflows/definitions/<name>/workflow.yaml`
  (or legacy flat `*.yaml`). Written by `/wise-workflow-create`. Survive
  plugin updates.
- **Run state** — `~/.local/share/wise/runs/<cwd-slug>/<run-ulid>/state.yaml` (honours `XDG_DATA_HOME`).
  Per-workspace. Each step execution gets its own ULID and log file
  at `logs/<step-id>.<step-run-ulid>.log`.

If a user definition has the same name as a bundled one, the user
version wins at run time. `/wise-workflow-list` flags this as a
shadow.

### Workflow artifacts

Folder-form workflows can ship their own supporting files beside the
`workflow.yaml`. By convention:

```
<name>/
├── workflow.yaml
├── README.md         # overview + mermaid flow diagram + step table; see below
├── templates/        # long-form text the workflow injects (PR bodies, email templates, …)
└── prompts/          # prompt fragments shared across steps or with standalone skills
```

**Every workflow ships a `README.md`** with a consistent
structure — title + summary → When to use → When not to use →
Prerequisites → Flow (mermaid flowchart derived from the
step DAG) → Steps (table) → Inputs → Outputs → Examples →
Related. The `/wise-workflow-create` wizard generates a scaffolded
README automatically; when hand-authoring, copy the shape from
any of the bundled workflows' READMEs
(`plugins/wise/workflows/*/README.md`).

Steps reference them via the `{{workflow.dir}}` template variable,
which expands to the absolute path of the folder:

```yaml
- id: draft-body
  type: prompt
  prompt: |
    Read the template at {{workflow.dir}}/templates/pr-template.md
    and fill it from {{changes_summary}}.
- id: run-fixer
  type: bash
  command: cat {{workflow.dir}}/prompts/watch-pipelines.md
```

`{{workflow.dir}}` is the empty string for legacy flat-form
workflows, so any reference to it from a flat-form definition is a
bug — migrate to folder form first (just `mkdir <name>` + move the
file to `<name>/workflow.yaml`; no YAML contents change).

## Definition schema

```yaml
version: 1                       # integer on its own line; required
name: release-checklist          # kebab-case; matches filename
description: Run tests, build, and open a release PR.
author: your-name                # optional

requires:                        # optional per-workflow deps; probed at run start
  - plugin: some-other-plugin
  - skill: skill-creator:skill-creator

project-selection: current       # current (default) | prompt | any (see below)

preflight:                       # optional — pin pre-flight answers (see below)
  control-mode:   wave-sync      # prompt (default) | wave-sync | synchronous
  worktree:       prompt         # prompt (default) | current | new
  rename_session: prompt         # prompt (default) | skip

steps:
  - id: list-workflows           # unique per workflow; [a-z0-9-]
    type: skill
    skill: wise:wise-workflow-list
    payload: {}
    depends_on: []

  - id: decide-release-kind
    type: prompt
    prompt: |
      Project: {{project.name}} (kind {{project.kind}}, path {{project.path}}).
      Given `git status`, is this release 'patch', 'minor', or 'major'?
      Reply with exactly one of: patch | minor | major.
    until: "^(patch|minor|major)$"
    max_iterations: 3
    outputs: [release_kind]
    depends_on: [list-workflows]

  - id: run-tests
    type: bash
    command: make codecept unit
    cwd: "{{project.path}}"
    success:
      exit_code: 0
      stdout_matches: ".*OK.*"
    timeout: 600
    depends_on: [decide-release-kind]

  - id: lint
    type: bash
    command: npm run lint
    cwd: "{{project.path}}"
    success: { exit_code: 0 }
    depends_on: [decide-release-kind]
    # Same depends_on as run-tests → they run in parallel.

  - id: approve-merge
    type: approval
    message: |
      Tests passed. Lint passed. Release kind: {{release_kind}}.
      Approve merge?
    depends_on: [run-tests, lint]
    trigger-rule: all-success
```

## Step types

| Type | Success when | Failure when | Captured output |
|---|---|---|---|
| `skill` | The `Skill` tool call returns without raising. | `Skill` errors or the invoked skill emits a fatal line. | Last message of the skill's reply. |
| `prompt` | Subagent's final message matches `until:` regex (or, when `until:` is absent, single-shot success on return). | `max_iterations` hit without a match; subagent errors; timeout. | Named `outputs:` captured from the matching line's regex groups. |
| `bash` | `success.exit_code` matches the actual exit code AND all `success.stdout_matches` / `success.stderr_matches` regexes pass. | Any condition fails; timeout. | stdout+stderr to the step's log file; last ~1KB in state. |
| `approval` | User picks Approve (wave-sync), OR the run is in synchronous mode (auto-approved). | User picks Reject or cancels (wave-sync only — never auto-rejects). | The selection label, or `auto-approved (sync mode)`. |
| `ask` | User picks the skip or confirm option (wave-sync), OR sync mode (skipped). | — (ask steps don't fail — they always record *some* value, possibly empty). | The chosen value; see "`ask` rendering shapes" below. |
| `interactive` | Conductor's main-thread execution of the step body emits a final line matching `until:`. | `max_iterations` doesn't apply — the conductor retries by re-reading the body. Failure surfaces when the conductor explicitly fails the step. | Named `outputs:` captured from the final line's regex groups, identical to `prompt`. |

### `prompt` vs `interactive`

Both types take a free-form `prompt:` body and use `until:` +
`outputs:` to capture a verdict. The difference is **where the
body runs**:

- `prompt` spawns a **Task subagent** — isolated, its own tool
  list, releases the transcript on return. Good for
  self-contained work: research, generation, bulk analysis.
  Cannot call `AskUserQuestion` (subagent-side, not main-thread).
  Parallelisable — multiple `prompt` steps in the same wave run
  concurrently via parallel `Task` calls in one conductor turn.
- `interactive` runs **inline in the conductor** — the main Claude
  Code conversation reads the body and follows it directly.
  Full main-thread tool access including `AskUserQuestion`,
  richer dispatch across turns. NOT parallelisable — blocks the
  conductor until it finishes.

Pick `interactive` when the body needs to walk the user through
per-item decisions (sonar wizards, review-comment wizards,
anything that calls `AskUserQuestion` more than once). Pick
`prompt` for everything else. An `interactive` step in a wave
with other steps forces the others to wait — don't use it as a
drop-in replacement for `prompt`.


### `ask` — two rendering shapes

An `ask` step captures an answer from the user and records it as
a named output. It renders one of two ways depending on the YAML
shape:

- **Free-text** (default) — `ask` with no `confirm_label` key.
  The user gets two options: the skip label (records empty
  string) or `Provide input` (records free-text via the
  AskUserQuestion Other affordance). Use this when the answer is
  an open value: a comment, a ticket id, a branch name.
- **Binary choice** — `ask` with a `confirm_label` key. The user
  gets two explicit options: the skip label (records empty
  string) or the confirm label (records `confirm_value`, or the
  confirm label itself when `confirm_value` isn't set). Use this
  for yes/no opt-ins: "watch the PR?", "run tests?", etc. The
  free-text affordance is dropped — this is deliberately binary.

```yaml
# Free-text: "what's your comment?"
- id: user-comments
  type: ask
  question: "Any comments for the planning step?"
  output: user_comments
  skip_label: "Skip — no extra guidance"

# Binary: "do you want to opt into this extra stage?"
- id: ask-watch
  type: ask
  question: "Watch the PR until it's green?"
  output: watch_choice
  skip_label: "No — I'll watch manually"
  confirm_label: "Yes — watch pipelines"
  confirm_value: "yes"
```

Downstream steps gate with `when:` — `when: "user_comments != ''"`
for free-text (truthy = user provided something), or
`when: "watch_choice == 'yes'"` for binary (exact-match the
confirm value).

Picking the binary shape for yes/no questions matters for UX:
`Provide input` + free-text forces the user to type `yes` by
hand, which is slow and error-prone. Use binary whenever the
answer is enum-like.

### `trigger-rule` — what makes a dependent runnable

Set on the *dependent* step (not the dependencies). Controls whether
a step becomes runnable once its `depends_on` entries are terminal:

- `all-success` (default) — every dep `completed`.
- `one-success` — ≥1 dep `completed`; others may be `failed` or `skipped`.
- `all-done` — every dep terminal (completed/failed/skipped).
- `none-failed-min-one-success` — every dep terminal, none failed, ≥1 completed.

### Surfacing step output to chat

By default a step's full output goes to its log file; only a
one-line verdict appears in chat. When a step produces content the
user needs to *review* (a drafted PR body, a generated report),
add a `surface:` field to the step definition:

```yaml
- id: draft-body
  type: prompt
  prompt: |
    …write the drafted body to a temp file; emit DRAFT: body_path=<path>…
  until: 'DRAFT: body_path=(\S+)'
  outputs: [pr_body_path]
  surface:
    file: pr_body_path           # read file at state.outputs[pr_body_path]
    label: "Drafted PR body"     # optional header shown above the content
    max-lines: 400               # optional cap; default 400
```

The conductor reads the file (if it exists and is readable) and
inlines the content as a fenced block in the wave-results render,
right after the step's one-line outcome. Truncation adds a
`… (<N> more lines)` footer. A missing output / unreadable file
degrades to a `<surface failed: …>` note — never fails the step.

Use this sparingly — only for content the user actually needs to
see inline. Long noisy outputs belong in the log file, not the
main chat.

### Templating

Before a step runs, the conductor renders `{{project.path}}`,
`{{project.name}}`, `{{project.kind}}`, `{{workflow.dir}}` (absolute
path to the workflow folder for folder-form definitions, empty
string for flat-form), `{{run.dir}}` (absolute path to this run's
directory — the parent of `state.yaml`, off the project tree),
`{{run.id}}` (the run ULID), and any named `outputs` from earlier
completed steps. `{{run.dir}}` is where a step writes run-scoped
artifacts that should persist with the run rather than land in the
project tree — e.g. `{{run.dir}}/plans/PLAN-<ref>.md`. No expression
evaluation beyond literal replacement. For conditional execution, use
the step-level `when:` field with a trivial comparison:
`when: "release_kind == 'patch'"`.

## Project selection

`wise` keeps no persisted project registry — the project a run
operates on is derived from the current context. Set at the workflow
level via `project-selection:`:

- `current` (default) — the conductor auto-detects the project from
  the current directory: `path` from `git rev-parse --show-toplevel`
  (falling back to `pwd`), `name` from the repo basename or
  `origin` slug, `kind` inferred from the repo's contents.
- `prompt` — the conductor auto-detects as above, then presents an
  `AskUserQuestion` at run start so the user can confirm or override
  each of `path` / `name` / `kind`.
- `any` — the workflow is workspace-agnostic; `project` stays null,
  and `{{project.*}}` templates resolve to empty strings.

## Session tagging

The very first persistent act of a run is session tagging. Before
pre-flight, the conductor:

1. Allocates the run's ULID.
2. Creates `~/.local/share/wise/runs/<cwd-slug>/<run-ulid>/` and writes a stub
   `state.yaml` with `status: initializing`.
3. Infers the current Claude Code session UUID by picking the
   most-recently-modified `.jsonl` in
   `~/.claude/projects/<cwd-slug>/` and records it as
   `claude_session_id:` in state.yaml. (No env var surfaces the
   UUID into a skill's shell; the newest-jsonl heuristic is
   reliable because your own session is being appended to as the
   workflow runs.)
4. Derives a human-readable label of the form
   `<run-ulid>_<first-7-hyphen-tokens-of-workflow-name>` and
   records it as `session_label:`.
5. Checks for **session conflicts** — other non-terminal runs in
   the same workspace that have already claimed this session. On
   match, asks whether to continue (both runs share the session —
   `/resume` will only return to whichever renamed the session
   most recently) or abort.
6. Prints a copy-pasteable `/rename <session_label>` command and
   asks the user to confirm (rename / skip rename / abort run).
   The rename is cosmetic: resume uses the UUID, not the label,
   so skipping is safe — the `/resume` picker will just show the
   raw UUID rather than a friendly label.

The label exists for two reasons. First, so `/resume`'s picker
shows something descriptive ("01K…_release-checklist" instead of a
raw UUID) when you reach for it manually. Second, so when a run is
re-tagged mid-flight (see [Resume](#resume) below), the info line
can identify the previous host session by label rather than UUID.

Legacy runs (started before this feature) have no
`claude_session_id` field; resume treats that as "no stored session"
and proceeds without any notice.

## Pre-flight prompts

After session tagging, BEFORE the run flips to `status: running`,
the conductor asks up to three questions — rename_session,
control-mode, worktree. Each can be **pinned by the workflow
definition** via the top-level `preflight:` block, in which case the
corresponding AskUserQuestion is skipped and the pinned answer is
logged.

### The three keys

1. **Session rename (`preflight.rename_session`):**
   Asked first. Suggests `/rename <session-label>` so the run is
   findable in `/resume`'s picker. `skip` value pins no-rename.
   Optional — the session UUID is always tracked regardless; the
   only effect is the picker shows the raw UUID instead of a
   friendly label.

2. **Control mode (`preflight.control-mode`):**
   - **Wave-sync (recommended)** — run one wave of steps, then pause
     for the user. Between waves you can chat freely, abort, or
     steer. Approval gates use `AskUserQuestion`. This is the only
     mode that lets you interrupt mid-run.
   - **Synchronous** — run end-to-end without stopping. **Approval
     gates are auto-approved** — picking synchronous is itself the
     blanket approval. Each auto-approved gate writes a
     `[sync auto-approved]` line to its step log, so the decision
     is auditable after the run.

   Pin `wave-sync` on workflows with `ask` steps, AskUserQuestion
   inside prompt steps, or interactive approval gates — synchronous
   mode would break them by auto-approving and skipping asks. Pin
   `synchronous` on end-to-end automated workflows with no human
   decision points.

3. **Worktree (`preflight.worktree`):**
   - **Current tree** — run against the project path as-is.
   - **Dedicated worktree** — create a sibling worktree at
     `<project-path>.wise-<run-ulid>` on branch
     `wise/<name>-<run-ulid>`. All `{{project.path}}` templates and
     bash `cwd` fields resolve to the worktree. Cleanup is manual
     (`git worktree remove` when you're done).

   Pin `current` for read-only workflows (status checks, reports).
   Pin `new` for workflows that make destructive-ish edits and the
   user should always be able to throw the tree away.

### Why pin

Pre-flight prompts are asked unconditionally by default — that's
safest but noisy for workflows where one of the three questions has
a wrong-answer option. Example: `pr-interactive` has `ask` steps
and AskUserQuestion-driven prompt steps, so offering Synchronous
at pre-flight is a footgun — picking it breaks the workflow before
it starts. Pinning `control-mode: wave-sync` in the definition
removes the question entirely. When every key is `prompt` (the
default), omit the block — pre-0.42 workflow files didn't have it
and they still behave the same.

All resolved answers persist in `state.yaml` so resume doesn't
re-ask.

## Dependencies

Two layers:

- **Plugin-level** — the wise `plugin.json` can declare
  `"dependencies": [...]`, which Claude Code auto-installs with wise
  (v2.1.110+; see
  https://code.claude.com/docs/en/plugin-dependencies.md).
  Populated when the *shipped* workflows need a third-party plugin.
- **Workflow-level** — each definition's `requires:` list. Probed at
  run start. If anything is missing the conductor prints the exact
  `/plugin install` commands and asks:
  - `I've installed them, re-check` — re-probe; if still missing,
    re-prompt; if OK, continue.
  - `Abort` — stop without creating a run directory.
  wise never auto-installs plugins.

## Run state

```yaml
version: 1
run_id: 01J9Z2N0S3KHK2H9TMNWQJP6TN     # ULID
workflow_name: release-checklist
workflow_version: 1
workspace: /path/to/your-project
claude_session_id: 684af09c-b0e3-40bc-bebb-6c05e473c563   # the session the run was started in; null on legacy runs
session_label: 01J9Z2N0S3KHK2H9TMNWQJP6TN_release-checklist  # suggested /rename target; advisory only
started_at: 2026-04-19T10:30:00Z
last_activity_at: 2026-04-19T10:32:15Z
completed_at: null
status: running                     # initializing | running | paused | completed | failed | cancelled
control_mode: wave-sync
worktree: null                      # or { path, branch, created_by_ws }
project: { path, name, kind }       # null when project-selection: any
outputs: { release_kind: minor }
steps:
  - id: list-projects
    status: completed
    run_id: 01J9Z2N0SX4ABCDEF123456    # ULID of THIS step execution
    started_at: 2026-04-19T10:30:02Z
    completed_at: 2026-04-19T10:30:05Z
    log: logs/list-projects.01J9Z2N0SX4ABCDEF123456.log
  ...
```

`state.yaml` is the canonical truth; everything else (TodoWrite,
user-facing summaries) is derived.

**Run history cap.** Each new run prunes older runs in the same
workspace so the total stays at **25** (override with
`WISE_RUN_HISTORY_CAP`). Non-terminal runs (`initializing` / `running`
/ `paused` / `failed`) are protected — the cap only reclaims
*terminal* runs (`completed` / `cancelled`), oldest first by
`last_activity_at`. The pruned run's state.yaml and step logs are
deleted from `~/.local/share/wise/runs/<cwd-slug>/<ulid>/`; the user's Claude Code
session transcripts (`~/.claude/projects/…`) are never touched.

## Resume

`/wise-workflow-resume <run-ulid>` loads the state, reconciles Claude
Code sessions (see below), resets any mid-flight `running` step to
`pending` (with a fresh step-run-ulid on re-entry — the old log stays
for debugging), and re-enters the conductor's main loop using the
persisted control mode and worktree. Completed steps are never
re-run.

**Session re-tag (no prompt).** Before re-entering the loop, resume
silently re-tags the run with the session you're resuming from. A
skill can't invoke `/resume` on the user's behalf, so "switch back
to the original session" would mean blocking the run on a command
the user has to type themselves — worse UX than just continuing
here, where the user already is. Instead:

- **Match, stored-null (legacy run), or current-session untagged** —
  no notice; proceed.
- **Mismatch** — overwrite `claude_session_id` with the current
  session UUID and emit one info line:
  `(Previously started in session <stored-label>; continuing here.)`
  (or `…, which is no longer available; continuing here.` when the
  original's `.jsonl` has been wiped). Resume then proceeds
  normally.

The `session_label` is preserved so future `/rename` lookups still
work; only the UUID pointer moves. The original session's
transcript remains on disk under `~/.claude/projects/<slug>/` for
reference — if you really want to go back, `/resume <old-label>`
from your prompt bar at any time.

For genuinely re-run-worthy steps (e.g. "re-run the test step after I
fixed the flaky bit"), use the `Modify` option in wave-sync mode or
manually set `steps[i].status=pending` via editing `state.yaml`.

## Progress reporting

Every state transition is surfaced in-chat. Before a wave dispatches,
the conductor announces each step it's about to run:

```
Wave 2 — 3 step(s):
  - ▶ run-tests (bash): $ make codecept unit (cwd: …/learning-site-spa)
  - ▶ lint (bash): $ npm run lint (cwd: …/learning-site-spa)
  - ▶ a11y-audit (skill): invoke wise:a11y
```

After the wave returns, every step's outcome is reported on its own
line:

```
Wave 2 results:
  - ✓ run-tests: exit 0 in 42s
  - ✓ lint: exit 0 in 8s
  - ✗ a11y-audit: 3 violations found (see logs/a11y-audit.01K….log)
```

Skips (from `trigger-rule` not being met) get their own line with
the reason:

```
Skipping:
  - ⊘ approve-merge (approval) — all-success not met: dep run-tests is failed
```

Both modes produce this output — wave-sync adds the
continue/pause/abort/modify menu after each wave; sync mode chains
straight to the next wave. The one-line per step is a summary; the
full output still lives in `logs/<step-id>.<step-run-ulid>.log`
under the run directory.

## The user-control caveat

Claude Code's main conversation can only do one thing at a time, and
subagents can't prompt back into the main session. A workflow run
therefore occupies the conversation while it executes. **Wave-sync**
is the closest practical approximation to "work alongside a running
workflow": between waves you have the full session and can chat,
steer, or abort. Synchronous mode trades that interactivity for
less ceremony.

**Synchronous mode is silent by design.** Between the "Run <id>
started" line and the final summary, the only user-visible output
comes from approval gates. Step output goes to the per-step log
file under `<run-dir>/logs/`. If a sync-mode run appears to hang
after the first step, tail the active run's state file to confirm
steps are transitioning:

```
watch -n 1 cat "$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" runs-root)/<run-ulid>/state.yaml"
```

If `status: running` and `last_activity_at` keeps advancing, the run
is healthy — just quiet. If `last_activity_at` is stale, that's a
real stall; resume it with `/wise-workflow-resume <run-ulid>` in a
fresh session.

## Authoring workflows

Interactively: `/wise-workflow-create <name>` — the wizard walks you
through the schema step-by-step. Writes to
`${CLAUDE_PLUGIN_DATA}/workflows/definitions/<name>/workflow.yaml`.

By hand: create a YAML file at that path matching the schema above.
Validate with a dry run — `/wise-workflow-run <name>` will reject a
malformed definition cleanly rather than explode mid-run.

See [`CONTRIBUTING.md`](../../CONTRIBUTING.md) [§9](../../CONTRIBUTING.md#9-workflow-subsystem) for the full
schema reference and the author walkthrough in prose.

## Python dependency

The workflow subsystem requires Python 3 with PyYAML and python-ulid.
`scripts/bootstrap-deps.sh` probes on every workflow command and
offers to install via `mise` (recommended) or directly via brew if
Python isn't on PATH. If Python is present but modules are missing it
runs `pip install --user` and retries.
