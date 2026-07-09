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

> **On non-Claude harnesses** (Codex / Cursor / Hermes / opencode): `${CLAUDE_PLUGIN_ROOT}`
> becomes `${WISE_PLUGIN_ROOT}`, which **defaults** to the pack's install
> dir `~/.local/share/wise/harness/<harness>` (baked into the port skills —
> no export needed; set `WISE_PLUGIN_ROOT` only to override). User
> definitions resolve under `WISE_DATA_DIR`, default `~/.local/share/wise`.
> The paths above are the Claude spelling of a harness-neutral scheme; see
> [`docs/compatibility.md`](../compatibility.md) and each port's README.

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
(`harnesses/claude/wise/workflows/*/README.md`).

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

agents: auto                     # optional — off (default) | auto. Default agent
                                 # policy for prompt steps (see Agent roster below).

preflight:                       # optional — pin pre-flight answers (see below)
  control-mode:   wave-sync      # prompt (default) | wave-sync | synchronous | auto-advance
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
    agent: architect             # optional (prompt only) — force a roster role,
                                 #   or `auto` / `off`. See Agent roster below.
    model: opus                  # optional (prompt only) — inherit | opus | sonnet | haiku | fable
    effort: high                 # optional (prompt only) — low | medium | high | xhigh | max
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

> **Cross-harness note.** The step types below are harness-neutral, but the
> primitive each maps to varies: on Claude Code, Hermes, and opencode a
> `prompt` step's role/team runs as parallel `Task`/native subagents (on
> opencode the roster is also registered as `wise-<role>` agent cards in
> `~/.config/opencode/agents/`); on Cursor there is no
> subagent primitive, so the conductor adopts the role in-context and runs
> teams sequentially; on Codex it uses subagents where available. `ask` /
> `approval` become plain-chat questions off Claude. Each port's
> `/wise-workflow-run` carries the full mapping in its execution note; see
> [`docs/compatibility.md`](../compatibility.md).

| Type | Success when | Failure when | Captured output |
|---|---|---|---|
| `skill` | The `Skill` tool call returns without raising. | `Skill` errors or the invoked skill emits a fatal line. | Last message of the skill's reply. |
| `prompt` | Subagent's final message matches `until:` regex (or, when `until:` is absent, single-shot success on return). | `max_iterations` hit without a match; subagent errors; timeout. | Named `outputs:` captured from the matching line's regex groups. |
| `supervised-prompt` | As `prompt`, but the worker runs as a watched background teammate; success when its task completes and the final message matches `until:`. | The worker stays hung past the nudge/respawn ladder and the supervisor fails the slot; subagent errors. | Named `outputs:` captured from the worker's final line, identical to `prompt`. |
| `bash` | `success.exit_code` matches the actual exit code AND all `success.stdout_matches` / `success.stderr_matches` regexes pass. | Any condition fails; timeout. | stdout+stderr to the step's log file; last ~1KB in state. |
| `approval` | User picks Approve (wave-sync / auto-advance), OR the run is in synchronous mode (auto-approved). | User picks Reject or cancels (wave-sync / auto-advance only — never auto-rejects). | The selection label, or `auto-approved (sync mode)`. |
| `ask` | User picks the skip or confirm option (wave-sync / auto-advance), OR sync mode (skipped). | — (ask steps don't fail — they always record *some* value, possibly empty). | The chosen value; see "`ask` rendering shapes" below. |
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

### `supervised-prompt` — a watched `prompt`

`supervised-prompt` is a `prompt` step whose worker runs as an
**addressable background teammate** (`Agent(team_name, name,
run_in_background: true)`) instead of a blocking `Task`, so the
conductor stays free to watch it. A leader loop — the routine in
`harnesses/claude/wise/references/supervise-loop.md` — polls the worker's
heartbeat and nudges it if it hangs mid-turn or goes idle without
finishing, escalating (`TaskStop` → respawn → fail the slot) only
if nudging fails. Use it for a single long step where a silent hang
would otherwise stall the run; `Task` has no timeout of its own.
Tune the watchdog with `WISE_WORKER_STALE_SECS` (default 180s),
`WISE_WORKER_POLL_SECS`, `WISE_WORKER_MAX_NUDGES`, and
`WISE_WORKER_MAX_RESPAWNS`. One supervised step is one worker (no
team `agent:` list). The same routine — under `SUPERVISE=yes` —
drives the `-auto` implement phase's executor fan-out, and the
standalone `/wise-supervise [team]` skill attaches it to any
already-running team.


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

## Agents, model and effort

`wise` ships an **SDLC agent roster** — a set of role subagents
(`wise:architect`, `wise:software-engineer`, `wise:security-engineer`,
`wise:code-reviewer`, …) under `harnesses/claude/wise/agents/`, catalogued in
[`harnesses/claude/wise/AGENTS.md`](../../harnesses/claude/wise/AGENTS.md). A `prompt`
step can be dispatched to one of them instead of the generic
`general-purpose` subagent, and can pin a model and a reasoning effort.

**These fields apply to `prompt` steps only.** An `interactive`
step runs inline in the conductor (it *is* the conductor, so it can't
become a subagent or switch model mid-conversation), and a `skill` step
runs under the invoked skill's own frontmatter. The fields are ignored
on every other step type. **A step that pins none of them inherits the
parent session's model + effort** — the harness setup at run time.

### Workflow-level policy: `agents:`

| Value | Effect |
|---|---|
| `off` (default) | `prompt` steps run as a plain `general-purpose` subagent unless the step sets its own `agent:`. Matches pre-roster behaviour. |
| `auto` | every `prompt` step with no explicit `agent:` is routed to the best-fit roster role (the conductor picks). |

### Step-level: `agent:`

Set on a `prompt` step; overrides the workflow policy for that step. It takes
either a **scalar** (one role or a policy keyword) or a **list** (a team of
roles dispatched together).

**Scalar:**

| Value | Effect |
|---|---|
| `<role>` (e.g. `architect`) | force this role → dispatched as `subagent_type: wise:<role>`. |
| `auto` | the conductor reads the roster (`workflows.py list-agents`) and routes to the role whose description best matches the step's intent + tool needs; falls back to `general-purpose` when nothing fits. |
| `off` | force the plain `general-purpose` subagent. (A YAML 1.1 boolean — both `agent: off` and `agent: "off"` work.) |
| *(omitted)* | inherit the workflow's `agents:` policy. |

**List — a team.** When `agent:` is a list, the step is worked by **several
roster roles at once** and the conductor **synthesizes** their outputs into the
step's single result. Each item is a bare role name or an object with
per-member overrides:

| Member field | Effect |
|---|---|
| `role` (required) | the roster role → `wise:<role>`. A bare string item is shorthand for `{role: <string>}`. |
| `lead` | `true` on **at most one** member → it runs *after* the peers, sees their drafts, and proposes an integrated recommendation before the conductor's final synthesis. Zero leads = equal peers, synthesized directly. |
| `model` | per-member model override; omitted → inherits the step-level `model:`. |
| `effort` | per-member effort override; omitted → inherits the step-level `effort:`. |

`auto` / `off` are policy keywords — valid only as a scalar, **not** as team
members. A team runs **in-conversation** (parallel `Task` subagents under the
subscription, then a conductor synthesis on the main thread — no extra API
billing). A member's `until:` is ignored; the contract applies to the
synthesized result. The step is **atomic** — a resume mid-team re-runs it
whole (members are idempotent producers), so no extra run state is kept.

### Step-level: `model:`

`inherit` (default) | `opus` | `sonnet` | `haiku` | `fable`. Passed as
the Task per-call model override — the real, harness-level way to run a
step's subagent on a specific model. It runs **in-conversation** under the
active subscription (no extra API billing; there is no subprocess/headless
backend). Resolution order Claude Code applies: env
`CLAUDE_CODE_SUBAGENT_MODEL` > this per-call `model:` > the roster agent's
own `model:` frontmatter > the session model. This is the primary per-step
knob — see [Model availability and fallback](#model-availability-and-fallback).

### Step-level: `effort:`

`low` | `medium` | `high` | `xhigh` | `max`. Claude Code's in-conversation
`Task` tool has **no per-call effort parameter**, so `effort:` is conveyed
as a **prompt directive only** — the conductor appends a one-line nudge
(*"Reason at high effort — think carefully, weigh alternatives."*), and
the targeted `wise:<role>` agent's frontmatter `effort:` is the standing
baseline. It is **best-effort and may be ignored** by the model/harness
today — the field is forward-looking (Claude-Code-first; a future model
may act on it at a lower level). When the effort knob must be real, pick a
roster agent whose default effort already matches. The directive uses the
**resolved** effort, clamped to what the model's **family** supports
(Opus, Sonnet, and Fable take the full range as of Sonnet 5; Haiku has
no effort control so it is dropped). Clamping is family-level, not
per-version — a pinned pre-Sonnet-5 id (e.g. `claude-sonnet-4-6`) is
treated as the `sonnet` family and so also passes `xhigh` through
unclamped — see
[Model availability and fallback](#model-availability-and-fallback).

### Model availability and fallback

Before dispatch the conductor resolves the pinned model/effort via
`workflows.py resolve-model`:

- A **known-retired / deprecated** full id (e.g.
  `claude-opus-4-1-20250805`) is swapped for its maintained alias
  (`opus`), and the substitution `reason` is shown in the step's outcome
  line + log.
- **Effort is clamped** to the resolved model's ceiling (a model that
  lacks `xhigh`/`max` steps down; a model with no effort control drops it).
- On a **live** "model unavailable" failure, the step retries once down a
  tier chain (`opus → sonnet → haiku`) before failing.

**Prefer aliases** (`opus`/`sonnet`/`haiku`/`fable`) in workflows — they
auto-resolve to a maintained model and rarely retire, so they sidestep
the fallback path entirely. The durable availability check is Anthropic's
`GET /v1/models` (it also reports each model's supported effort levels),
but it needs an API key the subscription-auth conductor may lack, so the
shipped path uses a static retired-id table plus the live error-driven
retry.

```yaml
agents: auto                 # workflow default

steps:
  - id: design
    type: prompt
    agent: architect         # force the role; uses its effort: high baseline
    model: opus              # alias — auto-resolves, rarely retires (the real knob)
    effort: high             # prompt-directive nudge (best-effort)
    prompt: |
      Design the …
  - id: research
    type: prompt
    agent: auto              # conductor picks the best-fit role
    prompt: |                # model+effort inherited from the session
      Investigate …
  - id: raw
    type: prompt
    agent: off               # plain general-purpose subagent
    prompt: |
      …
  - id: review               # a TEAM — three roles at once, conductor-synthesized
    type: prompt
    model: sonnet            # shared default for members that don't override
    effort: high
    agent:
      - role: architect
        lead: true           # integrates the panel before final synthesis
        model: opus          # per-member override
      - role: security-engineer
        effort: high
      - qa-engineer          # bare string → inherits step model/effort
    until: 'VERDICT: (ship|block)'   # governs the synthesized result, not members
    prompt: |
      Review the proposed change for …
```

The roster agents are real Claude Code plugin subagents (the Claude port) —
after install they appear in `/agents` and are directly invocable as
`subagent_type: wise:<name>`. Other ports vendor the neutral role cards
from `core/agents/` and map them to their own subagent primitive (or adopt
the role in-context where there is none). See
[`harnesses/claude/wise/AGENTS.md`](../../harnesses/claude/wise/AGENTS.md) for the full
list, each role's default effort, and how `auto` chooses.

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
3. Resolves the current session id and records it as
   `claude_session_id:` in state.yaml, in this order: the harness's
   exported id (`$CLAUDE_CODE_SESSION_ID` on Claude Code,
   `$WISE_SESSION_ID` on any harness) → else, on Claude, the
   most-recently-modified `.jsonl` in `~/.claude/projects/<cwd-slug>/`
   (reliable because your own session is being appended to as the
   workflow runs) → else a **synthetic per-workspace id**
   (`local-<cwd-slug>`). The synthetic fallback is what non-Claude
   harnesses use: it needs no transcript, so runs are still tagged and
   `/resume`-able off Claude. (The `claude_session_id:` key name is
   kept for state-file compatibility; on other harnesses it holds the
   neutral id.)
4. Derives a human-readable label of the form
   `<run-ulid>_<first-7-hyphen-tokens-of-workflow-name>` and
   records it as `session_label:`.
5. Checks for **session conflicts** — other non-terminal runs in
   the same workspace that have already claimed this session. Only a
   *live* run counts: matches are classified by how recently they
   checked in (`last_activity_at` vs `WISE_SESSION_STALE_SECS`,
   default 30 min). A `fresh` match means the user is interrupting an
   in-flight run, and asks whether to continue (both runs share the
   session — `/resume` will only return to whichever renamed the
   session most recently) or abort. A `stale` match is a run that was
   abandoned mid-flight and whose state froze at non-terminal; it is
   not a real conflict, so the new run proceeds (with a one-line note)
   rather than prompting.
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

The three `workflows.py` session subcommands degrade cleanly off Claude:
`current-session-id` returns the resolved id (synthetic when there is no
transcript, never empty), `session-path` exits 2 when no `.jsonl` exists
(the "no transcript" signal — always the case off Claude), and
`find-runs-by-session` matches on whatever id was stored. So the conflict
check and `/resume` work on every harness; only transcript-derived niceties
(a real UUID, the transcript path) are Claude-specific.

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
     is auditable after the run. In-step prompts are all suppressed:
     `ask` steps record empty, `interactive` steps don't call
     `AskUserQuestion`. Fully unattended.
   - **Auto-advance** — run waves back-to-back with **no between-wave
     menu** (like synchronous), but **still honor every in-step
     prompt** (like wave-sync): `ask` steps render, approval gates use
     `AskUserQuestion`, and `interactive` steps may prompt. The run
     flows wave-to-wave on its own and stops only where a step
     genuinely needs the user's input. Per-step chat output (9d/9e) is
     shown, so it is not silent the way synchronous is — it just never
     asks "continue to the next wave?".

   Pin `wave-sync` on workflows with `ask` steps, AskUserQuestion
   inside prompt steps, or interactive approval gates when the runner
   should also review progress between waves — synchronous mode would
   break those steps by auto-approving and skipping asks. Pin
   `auto-advance` on the same kind of workflow when its in-step
   questions should fire but the runner should NOT be asked to start
   each wave (e.g. `ticket-plan`, whose DAG is mostly one step per
   wave). Pin `synchronous` on end-to-end automated workflows with no
   human decision points.

3. **Worktree (`preflight.worktree`):**
   - **Current tree** — run against the project path as-is.
   - **Dedicated worktree** — create a sibling worktree at
     `<project-path>.wise-<run-ulid>` on branch
     `wise/<name>-<run-ulid>`. All `{{project.path}}` templates and
     bash `cwd` fields resolve to the worktree. Cleanup is manual
     (`git worktree remove` when you're done). On creation, files listed
     in a `.worktreeinclude` at the base repo root (gitignore syntax) are
     copied into the new worktree — `git worktree add` checks out only
     tracked files, so untracked artifacts the tree needs to run (`.env`,
     local config) are carried over automatically. Best-effort: no file,
     a non-git base, or a missing listed path are silent no-ops.

   Pin `current` for read-only workflows (status checks, reports).
   Pin `new` for workflows that make destructive-ish edits and the
   user should always be able to throw the tree away.

### Why pin

Pre-flight prompts are asked unconditionally by default — that's
safest but noisy for workflows where one of the three questions has
a wrong-answer option. Example: `ticket-plan` has
AskUserQuestion-driven prompt steps, so offering Synchronous
at pre-flight is a footgun — picking it breaks the workflow before
it starts. Pinning `control-mode: auto-advance` in the definition
removes the question entirely (and skips the between-wave menu its
mostly-one-step-per-wave DAG would otherwise trigger). When every key is `prompt` (the
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

All modes produce this output — wave-sync adds the
continue/pause/abort/modify menu after each wave; synchronous and
auto-advance chain straight to the next wave. The one-line per step
is a summary; the full output still lives in
`logs/<step-id>.<step-run-ulid>.log` under the run directory.

## The user-control caveat

Claude Code's main conversation can only do one thing at a time, and
subagents can't prompt back into the main session. A workflow run
therefore occupies the conversation while it executes. **Wave-sync**
is the closest practical approximation to "work alongside a running
workflow": between waves you have the full session and can chat,
steer, or abort. Synchronous mode trades that interactivity for
less ceremony. **Auto-advance** sits between the two — it drops the
between-wave menu like synchronous, but still stops at the
workflow's own in-step prompts (asks, approvals, interactive
questions), so the run pauses for real decisions without asking you
to start each wave.

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
