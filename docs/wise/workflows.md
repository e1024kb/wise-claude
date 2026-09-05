# wise workflows

A workflow is a YAML v2 definition the wise engine runs: a DAG of steps
(`agent`, `bash`, `approval`, `ask`, `units`) with a pre-flight
questionary (budget profile, per-group tuning, optional steps, inputs).
The engine is TypeScript under `plugins/wise/engine`, run as source on
bun or Node 24 (`engine/engine.sh`). It runs as a per-user daemon
(`wise-engined`) that spawns vendor CLIs headless (`claude -p`,
`codex exec`, `grok -p`, `gemini -p`) and exposes MCP tools to the
Claude Code conversation through the plugin's `.mcp.json` server
`wise-engine`. The conversation is a thin conductor: it renders
questions, forwards context, prints one line per event and answers
gates. It never sees step output.

Source of truth for this page: `plugins/wise/engine/src/*.ts`
(`defs.ts` schema, `scheduler.ts` DAG, `executor.ts` run loop,
`units.ts` and `phases/` pipelines, `adapters/` harnesses,
`migrate.ts` v1 rewrite).

## Commands

| Invocation | Purpose |
|---|---|
| `/wise-workflow-run [<name> [<inputs...>]]` | Pre-flight questions, `wise_run`, event loop, gates, final report. |
| `/wise-workflow-resume [<run-ulid>]` | Resume a `paused` or `failed` run, or answer a `gated` one, then follow it. |
| `/wise-workflow-status [<run-ulid>]` | List runs, or show one run and its open gate. |
| `/wise-workflow-list` | List bundled and user definitions. |
| `/wise-workflow-create <name>` | Wizard that writes a user definition. |
| `/wise-workflow-remove <name>` | Delete a user definition. Bundled ones are immutable. |
| `bash ${CLAUDE_PLUGIN_ROOT}/engine/engine.sh <command>` | The engine CLI (see [CLI](#cli)). |

Setup once: `/wise-init` (bun or Node 24, `claude auth login`, `gh`).
`DAEMON_UNAVAILABLE` from any tool means the daemon could not start:
run `/wise-init`, then retry.

## Where things live

| Thing | Path |
|---|---|
| Bundled definitions | `${CLAUDE_PLUGIN_ROOT}/workflows/<name>/workflow.yaml` |
| User definitions | `<plugin data>/workflows/definitions/<name>/workflow.yaml`, where plugin data is `$CLAUDE_PLUGIN_DATA`, else `$WISE_DATA_DIR`, else the data root |
| Data root | `$XDG_DATA_HOME/wise`, else `~/.local/share/wise` |
| Run directories | `<data root>/runs/<cwd-slug>/<run-ulid>/` (`<cwd-slug>` = realpath of the cwd with `/` replaced by `-`) |
| Daemon socket | `$XDG_RUNTIME_DIR/wise/engined.sock`, else `<data root>/engined.sock` |
| Daemon lock and log | `<data root>/engined.lock`, `<data root>/engined.log` (rotated at 10 MB to `.1`) |
| Engine config | `$XDG_CONFIG_HOME/wise/engine.json`, else `~/.config/wise/engine.json` |
| Session profile | `<data root>/profile/<session-id>` (one word, written by `/wise-profile`) |

Layouts per root: folder form `<name>/workflow.yaml` (preferred, may
ship `README.md`, `prompts/`, `templates/` addressed via
`{{workflow.dir}}`) or flat form `<name>.yaml` (`{{workflow.dir}}` is
empty). Folder form wins over flat in one root. The user root shadows
the bundled root. A `<workflow>` argument to the CLI is a name or a
path to a `.yaml` file.

Every bundled workflow ships a `README.md`: summary, when to use, when
not to, prerequisites, flow (mermaid from the step DAG), steps table,
inputs, outputs, examples, related. Keep it in sync with the YAML.

## Definition

Top-level keys (`defs.ts` `TOP_KEYS`). Unknown keys warn; `agents` is a
v1 error.

| Key | Required | Value |
|---|---|---|
| `version` | yes | `2`. Missing or `1` is an error with a migration hint. |
| `name` | yes | kebab-case, matches the folder or file name. |
| `description` | no | Free text. |
| `author` | no | Free text. |
| `project-selection` | no | `current` (default) \| `ask` \| `none`. |
| `preflight` | no | `{control-mode, worktree}` pins. |
| `requires` | no | `{plugins: [...], tools: [...]}`. |
| `tuning` | no | `{groups: [...]}`. |
| `profiles` | no | Mapping keyed `low` \| `medium` \| `max`. |
| `inputs` | no | List of input definitions. |
| `step-select` | no | `{prompt?, optional?: [step ids]}`. |
| `steps` | yes | List of steps. |

Minimal example:

```yaml
version: 2
name: release-check
description: Classify the release, run the tests, ask for approval.

preflight:
  control-mode: interactive
  worktree: current

tuning:
  groups:
    - id: classify
      label: "Classification"
      default: { harness: claude, model: sonnet, effort: low }
      fallback: [codex]

profiles:
  low: {}
  medium: {}
  max:
    tuning:
      classify: { model: opus, effort: medium }

inputs:
  - name: focus
    prompt: "What should the release notes focus on?"
    optional: true
    from-context: guidance

steps:
  - id: classify
    type: agent
    group: classify
    prompt: |
      Project {{project.name}} ({{project.kind}}) at {{project.path}}.
      Given `git log`, is this release patch, minor or major?
      Return the field directly: release_kind.
    schema:
      type: object
      properties:
        release_kind: { type: string, enum: [patch, minor, major] }
      required: [release_kind]
      additionalProperties: false
    outputs: [release_kind]
    max_turns: 3

  - id: tests
    type: bash
    run: npm test
    timeout: 600
    depends_on: [classify]

  - id: approve
    type: approval
    message: "Release kind {{release_kind}}, tests green. Tag it?"
    depends_on: [tests]
    trigger-rule: all-success
```

### `project-selection`

`current` detects the project from the run `cwd`: `path` = cwd, `name`
= basename, `kind` = `node` (`package.json`) \| `python`
(`pyproject.toml` or `setup.py`) \| `go` (`go.mod`) \| `rust`
(`Cargo.toml`) \| `other`. `ask` and `none` are accepted by the
validator (v1 `prompt` and `any` are errors with rename hints). The
engine detects the project from `cwd` for every value in this build.

### `requires`

```yaml
requires:
  plugins: [some-plugin]      # installed_plugins.json keys `<name>@<marketplace>`,
                              # or a dir holding .claude-plugin/plugin.json
  tools: [gh, codex]          # binaries on PATH
```

The v1 list form (`- plugin: x`) is an error. `wise_preflight` returns
the unmet entries as `requires_missing` (`plugin:<name>`,
`tool:<name>`); `wise_run` refuses with `REQUIRES_MISSING` while any
is unmet, before the auth probes and before a run directory exists.

### `preflight`

| Key | Values | Effect |
|---|---|---|
| `control-mode` | `interactive` (default) \| `synchronous` | `synchronous` auto-approves every `approval` gate (warn plus `step.done` "auto-approved (control-mode synchronous)") and answers child `wise_ask` calls from `context.decisions`, else fails them with `needs-human`. `interactive` parks the run at every gate. |
| `worktree` | `current` (default) \| `new` | Recorded. The engine runs steps in `cwd`; `units` steps make their own worktrees under the run directory. |

v1 keys `rename_session`, `tuning`, `step-select` are errors, as are
`wave-sync`, `auto-advance`, `prompt`. A run answer `control-mode`
overrides the pin when the conductor passes one.

### `tuning`

One question per group at pre-flight. A step binds with `group: <id>`;
`units` steps bind phases through `groups:`.

```yaml
tuning:
  groups:
    - id: plan                       # SLUG_RE ^[a-z][a-z0-9-]*$, unique
      label: "Plan phase"
      description: "Who writes the plan"
      default: { harness: claude, model: opus, effort: high }   # mapping, required
      fallback: [codex, grok]        # harnesses tried after a rate limit
      locked: true                   # no question; default stands
      options:                       # presets offered as choices
        - id: economy                # `default` is reserved
          label: "Economy"
          description: "Opus 4.8 at high"
          value: { model: claude-opus-4-8, effort: high }
```

| Field | Notes |
|---|---|
| `default` | `{harness?, model?, effort?}`. A string (`"opus / high"`) is a v1 error. |
| `fallback` | Harness list. Used when the primary is parked by a rate limit; a fallback runs with `model: inherit`. |
| `locked` | Question emitted with `locked: true`; the conductor skips it. |
| `options[].value` | Full or partial `{harness, model, effort}` merged over the profile value. |
| `steps` | v1 error. Bind from the step with `group:`. |
| `allow-api` | Landing in the same release (plan M6.2): lets the `low` profile run this group's `auth: api-key` steps. |

Resolution per step: preset answer (unlocked group) > profile tuning
for the group > group default. A step's own `harness` / `model` /
`effort` override the group.

### `profiles`

Budget levels the questionary offers. Keys are a subset of `low`,
`medium`, `max`; the profile question lists the declared levels, or all
three when the block is absent.

```yaml
profiles:
  low:
    description: "sonnet evidence, Opus 4.8 authoring"   # replaces the level blurb
    tuning:
      authoring: { model: claude-opus-4-8, effort: high } # group id -> partial default
    caps:                                                 # positive ints, CAP_RE ^[a-z][a-z0-9_]*$
      max_review_cycles: 2
  medium: {}                                              # the declared defaults
  max:
    tuning:
      authoring: { model: opus, effort: high }
    caps: { max_review_cycles: 5 }
```

Default blurbs: `low` "cheapest tiers, fewer retries", `medium` "the
workflow's declared defaults", `max` "highest tiers, widest scope". The
default answer is the session profile when offered, else `medium`,
else the first declared level. v1 keys `step-preset`, `skip`,
`team-mode` and the tuning value `"default"` are errors.

`caps` land in `state.caps`; a `units` step reads the names it lists.
`low` carries one rule the engine applies itself: every Opus-family pin
resolves to `claude-opus-4-8` (see [Model and effort
resolution](#model-and-effort-resolution)). Landing in the same release
(M6.2): `low` refuses `auth: api-key` steps unless `allow-api: true`,
and `caps.tokens` sets a per-run token ceiling that parks the run at a
gate (`Gate.ceiling = {used, limit}`).

### `inputs`

```yaml
inputs:
  - name: ticket_id                  # INPUT_NAME_RE ^[a-z][a-z0-9_]*$
    prompt: "Which ticket?"          # default "Value for <name>?"
    description: "URL or key"
    optional: true                   # else the run refuses to start without it
    default: defaults
    from-context: ticket[].ref       # pre-fill from the run context
    extract: "([A-Z]+-\\d+)"         # first capture group (else whole match) becomes the value
    validate: "^(defaults|ask)$"     # full match after extract
```

`from-context` grammar: `guidance` \| `ticket[].ref` \| `ticket[].title`
\| `ticket[].body` \| `ticket[].url` \| `links[]` \| `decisions.<key>`.
Ticket fields join with `, `, links with newlines. Order: positional
argument from the conductor, else the `input.<name>` answer, else the
context value, else `default`. A non-optional input with no value fails
`wise_run` with `MISSING_ANSWERS`. `validate` failures report
`INVALID:no-match` or `INVALID:validate`. v1 `options:` is an error:
use `validate` with an alternation, or an `ask` step.

Inputs are templating variables (`{{ticket_id}}`) and are also copied
into `state.outputs` at run start, so `when:` can read them by bare
name.

### `step-select`

```yaml
step-select:
  prompt: "Which research stages should run?"
  optional: [analyze-design, research-context]   # step ids; else steps with `optional: true`
```

One `multi` question, label from each step's `description`, all
selected by default. Deselected steps are skipped before the first
wave with verdict `skipped: deselected in pre-flight`. Downstream
consolidation steps use `trigger-rule: none-failed`. v1 `presets` and
object entries are errors.

## Steps

Common fields (`StepBase` and `StepOverrides`):

| Field | Types | Notes |
|---|---|---|
| `id` | all | `^[a-z][a-z0-9_-]*$`, unique. |
| `type` | all | `agent` \| `bash` \| `approval` \| `ask` \| `units`. v1 `prompt`, `skill`, `interactive`, `supervised-prompt` are errors with hints. |
| `description` | all | Shown as the step-select label. |
| `optional` | all | Offered in step-select when `step-select.optional` is absent. |
| `depends_on` | all | Step ids. Self or unknown id is an error. Steps whose deps are all terminal run together. |
| `trigger-rule` | all | See below. Default `all-success`. |
| `when` | all | Expression, see below. A list is a v1 error. |
| `group` | agent, units | Tuning group id. Warns "no effect" on bash / approval / ask. |
| `harness` | agent, units | `claude` \| `codex` \| `gemini` \| `grok`. Overrides the group. |
| `model`, `effort` | agent, units | Override the group. `effort`: `low` \| `medium` \| `high` \| `xhigh` \| `max`. |
| `auth` | agent, units | `subscription` (default) \| `api-key`. |
| `fallback` | agent, units | Harness list, overrides the group's. |
| `mode` | agent, units | `approval-required` \| `auto` (default) \| `full-access`. |
| `resume` | agent, units | `fresh` (default) \| `unit`. `unit` resumes the previous attempt's session cursor. |
| `max_turns` | agent, units | Passed to Claude and grok `--max-turns`. |
| `timeout` | agent, bash, units | Seconds. Default 1800 for agent and bash; per-phase defaults for units. |
| `stale_after` | agent, units | Idle seconds before the stale policy acts. Default 600. |
| `allowed_tools` | agent, units | Claude permission rules (`Bash(git:*)`, `WebFetch`) pre-granted to the child; grok gets them as `--allow`. |
| `allow-api` | agent, units | Landing in the same release (M6.2). |

### `agent`

```yaml
- id: classify
  type: agent
  group: classify
  prompt: |
    ... {{project.name}} ... Return the field directly: release_kind.
  schema:
    type: object
    properties: { release_kind: { type: string, enum: [patch, minor, major] } }
    required: [release_kind]
    additionalProperties: false
  outputs: [release_kind]
  max_turns: 3
  allowed_tools: ["Bash(git:*)"]
```

| Field | Notes |
|---|---|
| `prompt` | Required unless `skill`. Rendered, sent as the child's first user message. |
| `skill` | Sugar: `prompt: "Run /<skill>"`, forces `harness: claude`. Exclusive with `prompt`; a non-claude harness is an error. |
| `schema` | JSON schema for the structured result (`claude --json-schema`, `codex --output-schema`, `grok --json-schema`). Required when `outputs` is set. |
| `outputs` | Names copied from the structured result into run outputs. Each must be a schema property. A missing name fails the step: `schema result lacks <name>`. |
| `until` | Deprecated. Accepted on `agent` for one release with a warning; an error on other types. `wise-engine migrate` turns a plain enum regex into `schema` plus `outputs`. |

Verdict: first non-empty line of the child's text (200 chars), else the
JSON headline, else `ok`. Exit classes: `ok`, `error`, `rate_limited`,
`auth`, `timeout`, `max_turns`, plus `missing_output`. `rate_limited`
parks the harness and returns the step to `pending` (see [Fallback and
rate limits](#fallback-and-rate-limits)); `auth` fails the run.

### `bash`

```yaml
- id: stamp
  type: bash
  run: |
    set -eu
    date -u +%Y-%m-%dT%H:%M:%SZ
  outputs: [stamp]
  timeout: 15
```

`bash -c <run>` in the run cwd under the clean child environment.
Success is exit code 0 without timeout. `outputs`: the first name gets
the whole trimmed stdout (1 MiB cap). Verdict: last non-empty stdout
line, else `ok`; on failure `failed: <last stderr line or exit code>`.
v1 `command` (rename `run`), `cwd` (prefix the script with `cd`),
`success` are errors.

### `approval`

```yaml
- id: approve
  type: approval
  message: "Tests green. Tag {{release_kind}}?"
```

Opens a gate with options `approve` / `reject`. `approve` completes the
step (verdict `approved`), `reject` fails it (verdict `rejected`). Under
`control-mode: synchronous` the gate is auto-approved.

### `ask`

```yaml
- id: pick-next
  type: ask
  message: "Which improvement next?"
  options: [tests, docs, performance]
  allow_text: true           # default: true when `options` is empty, else false
  output: next_focus         # CAP_RE; default: the step id
```

Opens a gate; the answer is recorded as the output and as the verdict
`<output>=<value>`. An empty answer, or a value outside `options` when
`allow_text` is false, is refused (`INVALID_PARAMS`), the gate stays
open. `ask` never fails. v1 `question` (rename `message`), `header`,
`skip_label`, `confirm_label` are errors.

### `units`

The per-unit pipeline (ticket or plan file) as code. See [Unit
pipelines](#unit-pipelines).

```yaml
- id: process
  type: units
  pipeline: ticket                 # ticket | plan
  items: "{{ticket_list}}"         # rendered, then parsed
  groups: { plan: plan, implement: implement, review: review, fix: implement, watch: watch }
  caps: [max_review_cycles, max_fix_attempts, watch_minutes, watch_poll_seconds, watch_stable_passes]
  reviewers: [copilot-pull-request-reviewer]   # default
  parallel: 2                      # units at once, default 1
  resume: unit                     # fixer resumes the reviewer's session
```

| Field | Notes |
|---|---|
| `pipeline` | `ticket` (unit = ticket ref or URL) \| `plan` (unit = `PLAN-*.md` path, relative to cwd). |
| `items` | String. After rendering: a JSON array of strings or `{ref}` objects, else split on `,` `;` newline. Deduplicated. |
| `groups` | Non-empty mapping phase -> tuning group id for `plan`, `implement`, `review`, `fix`, `watch`. Unknown phase warns. `fix` falls back to `implement`'s group. |
| `caps` | Cap names the step reads from `state.caps`. Warns when no profile sets a listed name. |
| `reviewers` | GitHub logins for `gh pr edit --add-reviewer`. |
| `parallel` | Positive int. Git operations are serialised per step. |
| `resume` | `unit` reuses cursors inside a review / fix cycle; `fresh` (default) starts each child clean. |

Outputs: `{units: UnitRow[]}` (`{{units}}` renders the rows as JSON).
Verdict: `units=N merged=N open=N failed=N skipped=N`.

### `trigger-rule`

Set on the dependent step. Evaluated once every `depends_on` entry is
terminal (`completed` \| `failed` \| `skipped` \| `cancelled`).

| Rule | Runs when | Skips when |
|---|---|---|
| `all-success` (default) | every dep `completed` | any dep failed, skipped or cancelled |
| `one-success` | at least one dep `completed` | none completed |
| `all-done` | every dep terminal | never |
| `none-failed` | no dep failed or cancelled (all skipped is fine) | any dep failed or cancelled |
| `none-failed-min-one-success` | `none-failed` and at least one `completed` | as `none-failed`, or all skipped |

Skip verdict: `trigger-rule <rule> not satisfied: <dep>=<status>, ...`.
Prefer `none-failed` behind step-select so a deselected stage does not
skip-propagate.

### `when`

Evaluated after the trigger rule. False skips the step with verdict
`when: <expr> is false`.

```
or      := and ('||' and)*
and     := eq ('&&' eq)*
eq      := unary (('==' | '!=') unary)*
unary   := '!' unary | primary
primary := '(' or ')' | 'text' | "text" | number | true | false | identifier
```

Identifiers are dotted names resolved against `outputs`, `inputs`,
`answers` (a bare name is looked up in that order; `answers.profile`,
`inputs.gap_mode` address one root). An unset identifier is undefined:
`==` against anything is false, `!=` is true. Numbers and booleans
compare with strings by text. Truthy: non-empty string, non-zero
number, `true`, non-empty array. An unparseable expression is treated
as true and emits `warn` `when-unparseable:<step>:...`.

```yaml
when: "readiness == 'gaps' && gap_mode == 'ask'"
```

Order the guarding condition first: an output that was never recorded
is undefined, so `x != ''` alone is true.

## Templating

`render.ts` replaces, in this order and by plain text substitution:

| Placeholder | Value |
|---|---|
| `${CLAUDE_PLUGIN_ROOT}` | The plugin root (children have no such variable). Every agent child also gets the plugin root and the run dir as `--add-dir`, so the `references/` and `agents/` files a prompt cites are readable. |
| `{{workflow.dir}}` | Absolute folder of the definition; empty for flat form. |
| `{{run.dir}}` | The run directory. |
| `{{run.id}}` | The run ULID. |
| `{{project.path}}`, `{{project.name}}`, `{{project.kind}}` | From `state.project`. |
| `{{<name>}}` | `state.outputs` merged over `state.inputs` (an output wins). Non-string values render as JSON. |

No expressions. Unresolved placeholders stay verbatim (the bundled
`report` steps rely on this to detect a step that never ran). Rendering
recurses into every string, list and mapping of the step, including
`schema`, `items`, `message`, `run`. Use `{{run.dir}}` for run-scoped
files (`{{run.dir}}/plans/PLAN-<ref>.md`, `{{run.dir}}/research/*.md`,
`{{run.dir}}/report.md`).

## Harness, model and effort

### Which harness runs a step

`step.harness` > `group.default.harness` > `claude`. Every harness a
run needs is probed before the run directory exists; a missing login
fails `wise_run` with `AUTH_REQUIRED` and `login_cmd`.

| Harness | Binary | Subscription probe | Login command | API-key variable | Config dir variable |
|---|---|---|---|---|---|
| `claude` | `claude` | `claude auth status` (`loggedIn: true`) | `claude auth login` | `ANTHROPIC_API_KEY` | `CLAUDE_CONFIG_DIR` |
| `codex` | `codex` | `codex login status` | `codex login` | `OPENAI_API_KEY` | `CODEX_HOME` |
| `grok` | `grok` | `$GROK_HOME/auth.json` (default `~/.grok/auth.json`) non-empty | `grok login` | `XAI_API_KEY` | `GROK_HOME` |
| `gemini` | `gemini` | adapter landing (plan M5.3) | `gemini` | | |

`auth: api-key` copies the key variable into the child; `subscription`
(default) never does. Usage is folded per pool (`subscription`,
`api-key`), per harness and per step.

### Model and effort resolution

`resolve.ts`, applied at run start per enabled `agent` step and per
`units` phase (`state.resolved[<step>]` and `[<step>.<phase>]`):

1. Retired id swap: a known retired full id (`claude-opus-4-1-20250805`
   and the like) becomes its alias, with `reason`.
2. Low-profile Opus rule: under `low`, every Opus-family pin (`opus`, a
   `claude-opus-5*` id, a swapped retired id, a tuning override)
   resolves to `claude-opus-4-8`. A pin already on Opus 4.8 stands. Not
   env-tunable.
3. Capability clamp (`MODEL_EFFORT_SUPPORT`): `opus`, `fable`, `sonnet`
   take every effort; `haiku` has none, the effort is dropped.
4. Policy ceiling (`MODEL_EFFORT_CEILING`): `opus` and `claude-opus-5`
   (dated snapshots `-YYYYMMDD` included) cap at `high`;
   `claude-opus-4-8` caps at `xhigh`; anything else has no ceiling.
   Override with `WISE_EFFORT_CEILING="opus=xhigh,claude-opus-5=medium"`,
   `"<model>=off"`, or bare `off`. Unparseable pairs are ignored.

`model: inherit` (or no pin) omits `--model`, the child uses its own
default. An empty effort omits the flag.

### Effort per harness

| wise effort | claude `--effort` | codex `model_reasoning_effort` | grok `--reasoning-effort` | gemini |
|---|---|---|---|---|
| `low` | low | low | low | dropped |
| `medium` | medium | medium | medium | dropped |
| `high` | high | high | high | dropped |
| `xhigh` | xhigh | xhigh | xhigh | dropped |
| `max` | max | max | max | dropped |

### Mode per harness (`mode`)

| wise mode | claude `--permission-mode` | codex `-s` sandbox | grok |
|---|---|---|---|
| `approval-required` | `default` | `read-only` | `--permission-mode dontAsk` |
| `auto` (default) | `acceptEdits` | `workspace-write` | `--permission-mode acceptEdits` |
| `full-access` | `bypassPermissions` | `danger-full-access` | `--always-approve` |

Headless children cannot answer permission prompts. Claude children get
`--allowedTools mcp__wise-engine,<allowed_tools>`, `--add-dir <run dir>`
(a unit child also gets its worktree), `--strict-mcp-config` and an
`--mcp-config` holding only the engine server (D18, D19). Codex gets
`--add-dir` and `approval_policy=never`; grok gets `--allow <rule>` per
`allowed_tools`. Permission denials appear in the step log and as a
warning on the result.

### Child environment

Children start from an empty environment plus `HOME`, `PATH`, `LANG`,
`LC_ALL`, `TERM`, `TMPDIR`, `SHELL`, `USER`, every `XDG_*`, the
harness config-dir variable when set, the key variable only under
`api-key`, and the engine channel variables. `CLAUDECODE`,
`CLAUDE_CODE_*` and `CLAUDE_*SESSION*` are never inherited.

### Fallback and rate limits

A child exit classified `rate_limited` parks that harness with
exponential backoff (1, 2, 4, 8 minutes, cap 30), returns the step to
`pending` and emits `warn`. While parked, a step whose `fallback`
(step, else group) names another harness runs there with
`model: inherit`; the fallback is auth-probed on first use and skipped
with `warn` when logged out. An `auth` exit fails the run and kills its
other children.

### Concurrency

Children in flight are capped globally and per harness: global 4,
`claude` 2, `codex` 1, `gemini` 1, `grok` 1. Override in
`~/.config/wise/engine.json`:

```json
{ "concurrency": { "global": 6, "claude": 3, "codex": 2 } }
```

## Pre-flight questionary

`wise_preflight {workflow, cwd, profile?}` returns `{workflow, version,
questions, defaults, requires_missing}`. Question ids double as answer keys.

| Id | Kind | Options | Default |
|---|---|---|---|
| `profile` | `choice` | declared `profiles` levels, else `low`, `medium`, `max` | session profile if offered, else `medium`, else the first |
| `tuning.<group>` | `choice` | `default` ("Keep default (<harness / model / effort>)") plus each `options[].id`; `locked: true` when the group is locked | `default` |
| `harness.<group>` | `choice` | `default` ("Keep default (<harness>)") plus every other harness with an adapter and a subscription login; follows its `tuning.<group>`; absent for locked groups and when no other harness is ready | `default` |
| `step-select` | `multi` | optional step ids, labelled by `description` | all |
| `input.<name>` | `text` | | context value, else `default`, else empty when optional |

The conductor renders them with `AskUserQuestion`, skips locked
questions, the `profile` question when the session profile is known,
and inputs filled positionally, then calls `wise_run {workflow, cwd,
answers, context, inputs, profile?}`. Answers, inputs, context and the
resolved caps are persisted in `state.json`, so resume never re-asks.

A `harness.<group>` answer other than `default` runs the group's steps
on that harness with `model: inherit` (the harness's own default; a
Claude pin means nothing to codex) and the group's effort. Steps that
pin `harness:` themselves (and the `skill:` sugar) are unaffected.

`context` is what the children may not refetch from the transcript:
`ticket[] {ref, title?, body?, url?}`, `guidance`, `decisions
{key: value}`, `links[]`. Children read it with `wise_context`.

## Run lifecycle and gate protocol

### Statuses

| Status | Meaning |
|---|---|
| `initializing` | Run directory being created. |
| `running` | Scheduler active. |
| `gated` | Parked on an `approval` / `ask` gate or a child `wise_ask`. Answer it; not resumable. |
| `paused` | Daemon restarted with the run in flight; `wise_resume` continues it. |
| `completed` | Terminal. |
| `failed` | A step failed, a gate was rejected, or the run errored. Resumable. |
| `cancelled` | Terminal. |

Step statuses: `pending`, `running`, `completed`, `failed`, `skipped`,
`cancelled`. Every step execution gets a fresh step run ULID; a
resumed `running` step goes back to `pending` and keeps its cursor.

### Events

`events.jsonl`, one line per event with a `seq`. Fields: `type`,
`step?`, `verdict?` (200 chars), `outputs?`, `usage?`, `harness?`,
`model?`, `effort?`, `message?`, `kind?`, `data?`.

| Type | When |
|---|---|
| `run.started` | Verdict `<name> profile=<p> control=<mode> steps=<enabled>/<total>`. |
| `step.started` | Carries harness, model, effort for agent steps; `message` is the step's `description` when it has one. |
| `step.progress` | Live child status `turn N, tool X <target>, Nk tokens, <elapsed>: <latest assistant text>`, emitted when the tool changes, when its target changes (at most one per 5 s), else one per 30 s; and child `wise_report` lines (`kind` progress \| blocker \| decision \| finding). |
| `step.done` | Verdict plus clipped primitive outputs. |
| `unit.phase`, `unit.done` | `units` steps: phase start with harness and model; unit verdict and reason. |
| `usage` | Tokens folded into `state.usage` per pool, harness and step. |
| `gate.opened`, `gate.answered` | Gate lifecycle. |
| `warn` | Rate limit, stale child, auto-approval, unparseable `when`, daemon restart. |
| `run.done`, `run.failed` | Terminal (`run.done` also on cancel with verdict `cancelled`). |

### Gates

One gate is open at a time. `wise_wait {run_id, after?, timeout_ms?}`
long-polls (default 110 s, max 600 s, progress notification every 30 s)
and returns `{events, status, gate?, done}`. Gate shape: `{gate_id,
step, kind: approval | ask, message, options?, allow_text?}`. The
conductor asks the user and calls `wise_answer {run_id, gate_id,
value}`; `value` is the option value, free text when `allow_text`, or a
string array for multi. `GATE_STALE`: the gate closed, wait again. A
child `wise_ask` opens an `ask` gate the same way; the answer is
delivered to the child as a tool result (and as a nudge on Claude).

### Stale children

After `stale_after` seconds without output the engine nudges the child
(Claude only: "You have been idle for N minutes. Finish with your
structured result now."), then kills it after another window. Verdict
`failed: stale (no activity, killed)`; the cursor is kept for `resume:
unit`.

### Resume, cancel, daemon

`wise_resume {run_id}`: refused for `completed`, `cancelled` and `gated`
(answer instead); `running` is a no-op; `paused` / `failed` reset
in-flight steps to `pending` and reschedule with `warn` "run resumed".
Completed steps never re-run. `wise_cancel {run_id, reason?}` kills the
child process groups, marks running steps `cancelled`, emits `run.done`.

The daemon starts on demand (MCP server or CLI), one per user (lock
file), exits after 30 idle minutes (gated and paused runs do not keep
it alive), rotates its log at 10 MB. On restart every `running` run
becomes `paused` with `warn` "daemon restarted, run paused" and the
recorded child process group (`<run dir>/daemon.json`) is killed. A
client with another plugin version gets `DAEMON_VERSION_MISMATCH`.

Run history: each run prunes terminal runs in the same cwd beyond
`WISE_RUN_HISTORY_CAP` (default 25), oldest by `last_activity_at`.
Non-terminal runs are never pruned.

## Run directory

```
<data root>/runs/<cwd-slug>/<run-ulid>/
├── state.json                     # the ledger: status, answers, inputs, context, resolved,
│                                  #   caps, usage, steps, outputs, gate (atomic write)
├── events.jsonl                   # one event per line, seq ascending
├── daemon.json                    # daemon pid and child pgid (crash recovery)
├── logs/
│   ├── <step>.<step-run-ulid>.raw.jsonl   # vendor event stream
│   └── <step>.<step-run-ulid>.log         # header (harness, model, effort, mode, exit,
│                                          #   usage, tools) plus text and JSON excerpts
├── units/
│   ├── <branch>.json              # UnitLedger: unit, last_phase, verdict, reason, review,
│   │                              #   watch, cleaned, blueprint, plan_path, cursors, usage
│   └── <branch>.findings.md       # review / CI / bot findings handed to the fixer
├── plans/PLAN-<ref>.md            # engine-written plans (BLUEPRINT-<ref>.md on gaps)
├── worktrees/<branch>/            # one worktree per unit; removed on `merged`
├── checkpoints/<step>.json        # wise_checkpoint payloads
├── research/                      # workflow convention (ticket-plan writes here)
└── report.md                      # workflow convention (the bundled report steps)
```

`state.json` is the truth; `wise_status` and `wise-engine report` are
derived from it. Never under the project tree, never auto-cleaned.

## Child channel

Every child loads one MCP server, `wise-engine` (`bash
engine/engine.sh unit-mcp`), with `WISE_STEP_TOKEN`,
`WISE_ENGINE_SOCKET`, `WISE_DATA_ROOT` in its environment. The token is
scoped to one step of one run; a wrong token is `TOKEN_INVALID`.

| Tool | Params | Result |
|---|---|---|
| `wise_report` | `kind` progress \| blocker \| decision \| finding, `text` (200 chars), `data?` (1 kB) | `{accepted, seq}`; appears as `step.progress`. |
| `wise_ask` | `question`, `options?`, `allow_text?` | `{value}`. Interactive runs: an `ask` gate to the conductor. Synchronous runs: answered from `context.decisions` (exact key, partial key, an option named in a value, else the first option), else error `needs-human` with a `warn`. |
| `wise_context` | `key`: `ticket`, `guidance`, `decisions`, `links`, a dotted path (`ticket.0.body`), an output name, a step id, an input name | `{value}` or `{value: null}`. |
| `wise_checkpoint` | `data` | `{path}` of `checkpoints/<step>.json`. |

Main to child: `wise_nudge {run_id, step, message}` writes a user
message into a Claude child's stdin (`--input-format stream-json`);
codex and grok have no open stdin, `delivered: false`.

## Unit pipelines

`units.ts` and `phases/` run the ticket -> PR and plan -> PR loops the
v1 prose orchestrators used to describe. Phases in order:

| Phase | Kind | Does |
|---|---|---|
| `claim` | code | Ownership gate: our ledger = ours (resume); merged PR = shipped; foreign branch or worktree = skip. Resolves `base`. |
| `worktree` | code | `git worktree add` under `<run dir>/worktrees/`, applies `.worktreeinclude` once (`includes-done`). |
| `plan` | model | Writes `<run dir>/plans/PLAN-<ref>.md` (plan pipeline: re-plans the seed at HEAD). |
| `implement` | model | Task waves, one commit per task, in the worktree. |
| `review` | model | Three-lens panel (correctness, security, tests) writing `units/<branch>.findings.md`. |
| `fix` | model | Applies findings from review, CI or bot comments; commits. |
| `push` | code | `git push -u origin <branch>`. |
| `pr` | code | `gh pr create` with the repo template filled, or reuse. |
| `request-review` | code | `gh pr edit --add-reviewer` per `reviewers`. |
| `watch` | model | One pass: CI state, bot reviews, human comments, merged flag. |
| `cleanup` | code | On `merged`: remove worktree, delete local branch, `cleaned: true`. Runs after a failure too. |

Branch and worktree naming (`phases/common.ts`): a ticket ref with a
project key (`PROJ-777`) is the branch verbatim; a bare number becomes
`abstract-task-<n>`; a URL is reduced to its key. A plan branch is the
file name without `PLAN-` and `.md`, sanitised (`plan-<n>` for digits).
Worktree: `<run dir>/worktrees/<branch>`.

### Model phases

| Phase | Mode | Default timeout | Pinned default when no group | Pre-granted tools |
|---|---|---|---|---|
| `plan` | `auto` | 30 min | claude / opus / high | Read, Glob, Grep, Write, Edit, `Bash(git:*)`, `Bash(gh:*)`, `Bash(ls:*)`, WebFetch, WebSearch |
| `implement` | `full-access` | 90 min | claude / opus / high | edit tools, build tools (`git`, `npm`, `npx`, `pnpm`, `yarn`, `bun`, `make`, `just`, `go`, `cargo`, `python3`, `pytest`, `cd`, `cat`, `ls`), Task, Agent |
| `review` | `auto` | 30 min | claude / opus / medium under `low`, else high | Read, Glob, Grep, Write, Task, read-only `git` subcommands |
| `fix` | `full-access` | 45 min | implement's group | edit and build tools, `Bash(gh:*)` |
| `watch` | `full-access` | 15 min | claude / sonnet | edit tools, `Bash(gh:*)`, `Bash(git:*)`, `Bash(date:*)` |

The step's `timeout` and `max_turns` apply to every phase. Prompts are
templates under `engine/src/prompts/units/` (`ticket/plan.md`,
`plan/plan.md`, `shared/{implement,review,fix,watch}.md`); the
workflow hands them `guidance`, `decisions`, the ticket block or seed
plan, and paths. Structured results:

| Phase | Schema |
|---|---|
| `plan` | `{plan_path, status: ready \| insufficient-context \| no-access, blueprint_path?}`. `no-access` fails with `plan-no-access`; `insufficient-context` fails with `plan-insufficient-context` and records the `BLUEPRINT-<ref>.md`. |
| `implement` | `{waves, tasks, done, failed, commits}`. `done == 0` or no new commits fails the unit. |
| `review` | `{findings, blocking, verdict: approve \| changes-requested}`. |
| `fix` | `{fixed, skipped, commits}`. Commits are counted by git, not trusted. |
| `watch` | `{ci: green \| red \| pending, bot_reviews: resolved \| open \| stuck \| pending, human_comment, merged, verdict: ready \| wait \| fix \| blocked \| needs-human}`. |

### Loops and caps

| Cap | Default (`CAP_DEFAULTS`) | Used by |
|---|---|---|
| `max_review_cycles` | 2 | review -> fix cycles before pushing anyway (`review.converged: false`). |
| `max_fix_attempts` | 3 | fix + push rounds in the watch loop; exhaustion -> `exhausted`. |
| `watch_minutes` | 45 | wall clock of the watch loop; at the cap: `all-green` when the last CI was green, else `exhausted`. |
| `watch_poll_seconds` | 60 | sleep between watch passes. |
| `watch_stable_passes` | 2 | consecutive green-and-covered passes before merging. |

A cap applies only when the step lists it in `caps` and the profile
sets it; otherwise the default. Watch loop per pass: `merged` ->
`merged`; human comment or `needs-human` -> `human-intervention`;
`blocked` -> `blocked`; red CI or open bot reviews -> fix and push (a
fix without a commit -> `partial`); a requested bot silent for 15
minutes on the same head -> one substitute universal review per head;
stable target reached -> `gh pr merge --squash` (then `--merge` when
squash is disallowed) -> `merged`, else `all-green`.

Verdicts: `merged` \| `all-green` \| `blocked` \| `partial` \|
`exhausted` \| `human-intervention` \| `failed` \| `skipped`. Only
`merged` removes the worktree; every other verdict keeps it for a human.
On resume `claim` and `worktree` re-run, other completed phases are
skipped, a unit with a verdict is skipped.

## MCP tools

Server `wise-engine` from `plugins/wise/.mcp.json` (`bash
${CLAUDE_PLUGIN_ROOT}/engine/engine.sh mcp`, tool timeout 660 s). The
descriptions the model reads are in `engine/src/mcp.ts`.

| Tool | Params | Returns |
|---|---|---|
| `wise_preflight` | `workflow`, `cwd`, `profile?` | `{workflow, version, questions, defaults, requires_missing}`. Read-only. |
| `wise_run` | `workflow`, `cwd`, `answers`, `context`, `inputs`, `profile?` | `{run_id, status: running}`. Errors: `WORKFLOW_NOT_FOUND`, `WORKFLOW_INVALID {issues[]}`, `REQUIRES_MISSING {missing[]}`, `MISSING_ANSWERS {missing[], questions[]}`, `PROFILE_REFUSES_API {steps[]}`, `AUTH_REQUIRED {login_cmd}`. |
| `wise_wait` | `run_id`, `after?`, `timeout_ms?` | `{events, status, gate?, done}`. Returns at once for `gated` and `paused`. |
| `wise_answer` | `run_id`, `gate_id`, `value` | `{accepted}`; `GATE_STALE`. |
| `wise_status` | `run_id?` | One `RunSummary` (`run_id, workflow, status, started_at, last_activity_at, completed_at?, cwd, gate?, children?, usage_total?`) or every run, newest activity first. |
| `wise_cancel` | `run_id`, `reason?` | `{status: cancelled}`. |
| `wise_nudge` | `run_id`, `step`, `message` | `{delivered}`. |
| `wise_resume` | `run_id` | `{run_id, status}`. |

Errors come back as `{"error": {code, message, ...}}`. Codes:
`WORKFLOW_NOT_FOUND`, `WORKFLOW_INVALID`, `RUN_NOT_FOUND`, `GATE_STALE`,
`HARNESS_UNAVAILABLE`, `AUTH_REQUIRED`, `BUDGET_EXCEEDED`,
`DAEMON_VERSION_MISMATCH`, `NOT_IMPLEMENTED`, `ALREADY_RUNNING`,
`TOKEN_INVALID`, plus `DAEMON_UNAVAILABLE` from the MCP server itself.

## CLI

`bash ${CLAUDE_PLUGIN_ROOT}/engine/engine.sh <command>` (bun, else Node
24; exit 69 when neither is present).

| Command | Purpose |
|---|---|
| `preflight <workflow> [--profile low\|medium\|max] [--context <json>]` | The questionary spec. |
| `compile-check <workflow>...` | Validate definitions; exit 1 on any error. Issues carry `path`, `level`, `message`, `hint`. |
| `migrate <workflow.yaml> [--write] [--out <path>]` | Rewrite v1 as v2. Dry run by default; `--write` keeps `<file>.v1.bak`; exit 1 when the result still has errors. |
| `list-defs` | Bundled and user definitions (`name`, `source`, `path`). |
| `run <workflow> [--cwd] [--answers <json>] [--context <json>] [--input k=v] [--profile] [--follow] [--timeout-ms]` | Start a run through the daemon. `--follow` streams events and answers gates from stdin. |
| `wait <run_id> [--after] [--timeout-ms]`, `status [run_id]`, `answer <run_id> <gate_id> <value>`, `cancel <run_id> [--reason]`, `resume <run_id>`, `report <run_id>` | Daemon client commands. `report` prints verdicts, units and usage per pool. |
| `daemon serve\|start\|stop [--now]\|status` | The background daemon. |
| `mcp [--no-start]` | The stdio MCP server used by `.mcp.json`. |
| `unit-mcp [--token <t>]` | The child-side MCP server. |
| `auth [harness...] [--json]` | Per harness: binary on PATH, subscription login, login command. Exit 1 when `claude` is missing or logged out. Read by `/wise-init`. |
| `version`, `help` | |

Options: `--json` (default) \| `--text`, `--user-root <dir>`,
`--bundled-root <dir>`, `--data-root`, `--socket`, `--no-start`. Exit
codes: 0 ok, 1 error or run failed / cancelled, 2 not found, 64 usage,
69 daemon unavailable, 70 internal, 75 daemon already running.

## Environment

| Variable | Effect |
|---|---|
| `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, `XDG_RUNTIME_DIR` | Data root, engine config, socket location. |
| `CLAUDE_PLUGIN_DATA`, `WISE_DATA_DIR` | User definitions root (see [Where things live](#where-things-live)). |
| `WISE_EFFORT_CEILING` | Policy ceiling overrides. |
| `WISE_RUN_HISTORY_CAP` | Terminal runs kept per cwd, default 25. |
| `WISE_SESSION_STALE_SECS` | Seconds since `last_activity_at` after which a non-terminal run tagged with the same harness session counts as abandoned rather than a conflict, default 1800. |
| `CLAUDE_CODE_SESSION_ID`, `WISE_SESSION_ID` | Session id for the profile store (else newest transcript, else `local-<cwd-slug>`). |
| `WISE_STEP_TOKEN`, `WISE_ENGINE_SOCKET`, `WISE_DATA_ROOT` | Set by the engine in every child for `unit-mcp`. |

## Authoring

1. `mkdir <user root>/<name>` and write `workflow.yaml` (or run
   `/wise-workflow-create <name>`).
2. `bash ${CLAUDE_PLUGIN_ROOT}/engine/engine.sh compile-check <path>`
   until it prints no errors. Warnings (`until`, `group` on a bash
   step, unknown keys) are allowed but mean something is ignored.
3. `bash ${CLAUDE_PLUGIN_ROOT}/engine/engine.sh preflight <name>` to
   see the questionary a conductor will render.
4. Run it with `/wise-workflow-run <name>`.

Rules that keep runs cheap and resumable: children see only their
prompt, so hand results between steps through `schema` outputs and
files under `{{run.dir}}`; ask every user decision at pre-flight
(inputs with `validate`, an `ask` escape value, `when:` guards on the
mid-run `ask` step); pin `control-mode: synchronous` only when the
workflow has no `approval` step that needs a human. The repo validator
(`python3 scripts/validate_repo.py`) runs `compile-check` on every
bundled `version: 2` definition. Bundled workflows:
`example-workflow` (every step type), `ticket-plan`, `ticket-auto`,
`impl-plan-auto` (see their READMEs).

## Migration from v1

```
bash ${CLAUDE_PLUGIN_ROOT}/engine/engine.sh migrate <workflow.yaml>            # dry run: prints the v2 YAML and the notes
bash ${CLAUDE_PLUGIN_ROOT}/engine/engine.sh migrate <workflow.yaml> --write    # in place, original kept as <file>.v1.bak
```

Rules (`migrate.ts`):

| v1 | v2 |
|---|---|
| `version: 1` or missing | `version: 2` |
| `project-selection: prompt` / `any` | `ask` / `none` |
| `agents:` | dropped |
| `requires: [{plugin: x}]` | `requires: {plugins: [x]}` |
| `preflight.rename_session`, `.tuning`, `.step-select` | dropped |
| `control-mode: wave-sync` / `auto-advance` / `prompt` | `interactive` |
| `preflight.worktree: prompt` | `current` (warning) |
| tuning group `default: "opus / high"` | `default: {harness: claude, model: opus, effort: high}` |
| tuning group `steps: [a, b]` | dropped; `group: <id>` set on steps `a`, `b`; a missing default is derived from a bound step's pins, else `{harness: claude}` with a MANUAL note |
| profile tuning `"default"`, `step-preset`, `skip`, `team-mode` | dropped (warning) |
| input `options: [a, b]` | `validate: ^(a\|b)$`, labels folded into `prompt` |
| input named `ticket`, `ticket_id`, `tickets` / `guidance`, `config_prompt` | `from-context: ticket[].ref` / `guidance` |
| `step-select.optional` objects, `presets` | ids only, `label` to the step's `description`; presets dropped; a multi-step entry is MANUAL |
| `type: prompt` / `interactive` / `supervised-prompt` / `skill` | `type: agent` |
| `agent: <role>` | prompt prefix "Act as the wise `<role>` agent (see `${CLAUDE_PLUGIN_ROOT}/agents/<role>.md`)"; a team is folded to the lead plus lenses with a MANUAL note; `auto` / `off` dropped |
| `until: "^(a\|b)$"` | `schema {properties: {<output>: {enum: [a, b]}}}` plus `outputs`, prompt instruction appended; a non-enum regex keeps `until` with a warning |
| `max_iterations: n` (1..10) | `max_turns: n`, else dropped |
| `command` | `run` |
| `cwd` other than `{{project.path}}` | `cd "<cwd>" \|\| exit 1` prefixed to `run` |
| `success` | dropped (exit code 0) |
| `question` | `message` |
| `header` | dropped |
| `skip_label`, `confirm_label`, `confirm_value` | `options` (plus `allow_text: true` when no confirm label) with a MANUAL note about `when:` guards |
| `when: [a, b]` | `when: "a && b"` |
| step `model` / `effort` / `harness` equal to the group's | dropped; `model: inherit` dropped |
| `surface` | dropped |
| `skill` with `payload` | prompt "Run /<skill> with: <payload>", `harness: claude`; without payload the `skill:` sugar stays |

Manual after migration: `interactive` bodies that asked the user
mid-run (use `wise_ask` from the child, or pre-flight inputs plus
`when:`); `supervised-prompt` watchdog settings (use `timeout` and
`stale_after`); teams (one lead child, or several `agent` steps in one
wave); `surface` output review (write a file under `{{run.dir}}` and
mention it in the verdict); anything the notes mark MANUAL. Legacy v1
runs (`state.yaml`) are followed by the prose conductor under
`references/legacy-conductor/` until plan M3.4 removes it together
with `scripts/workflows.py`.
