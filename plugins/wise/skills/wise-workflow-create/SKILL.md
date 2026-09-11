---
name: wise-workflow-create
description: >-
  Scaffold a new workflow through a step-by-step wizard — collects name,
  description, control mode, inputs, provider tuning, and
  steps (id, type, type-specific fields, dependencies), previews the
  generated YAML + README, and writes on confirmation. Writes to
  `the engine-selected user definition directory<name>/` by default, or
  offers the bundled `plugins/wise/workflows/<name>/` path when run
  inside a clone of the marketplace repo. Invoked as
  `/wise-workflow-create` (bare alias) or `/wise:wise-workflow-create`
  (canonical). Use when the user says "create a workflow", "scaffold a
  workflow", "new workflow", "author a workflow", or types
  `/wise-workflow-create`.
argument-hint: "<name>"
allowed-tools: Read, Write, AskUserQuestion, Bash(mkdir:*), Bash(test:*), Bash(bash:*), Bash(pwd:*), Bash(dirname:*), Bash(python3:*), Bash(${WISE_PLUGIN_ROOT}/scripts/init-registry.py:*)
---

# /wise-workflow-create - author a v2 workflow

Before asking any user question, read and follow the
[question lifecycle](../../references/workflow-host-control.md#keep-asynchronous-questions-open).
Keep asynchronous prompts open until answered; this rule does not authorize
questions in autonomous or otherwise prompt-free procedures.

First read [host control](../../references/workflow-host-control.md). Resolve the
loaded installation, set `WISE_HOST` to this conductor and `WISE_PLUGIN_ROOT`
to that installation. Use its managed launcher for shell commands. Follow the
reference's diagnostics and explicit-answer fallback when MCP or a native picker
is unavailable. Conductor host and child provider are independent.


Create `version: 2` definitions for the Python engine. Use the host's
structured picker for choices and text inputs. Keep accepted answers
across stages; never create v1 `prompt`, `loop`, or `interactive` steps.

## 1. Resolve the destination

Run the init check in `${WISE_PLUGIN_ROOT}/references/init-check.md`,
then read canonical roots and existing definitions:

```bash
"$HOME/.local/share/wise/bin/wise-engine" --wise-host "$WISE_HOST" definition-roots
"$HOME/.local/share/wise/bin/wise-engine" --wise-host "$WISE_HOST" list-defs
```

The first argument is the workflow name. Ask for it if absent; require
`^[a-z][a-z0-9]*(-[a-z0-9]+)*$`. Reject an existing name in either root,
including flat `<name>.yaml` and folder `<name>/workflow.yaml` forms.
Never overwrite an existing definition.

Default destination: `<user_root>/<name>/workflow.yaml`. When cwd is
inside a marketplace clone containing both `.claude-plugin/marketplace.json`
and `plugins/wise/.claude-plugin/plugin.json`, offer User (private) or
Bundled (tracked in that clone). Use `<repo>/plugins/wise/workflows/<name>/`
only after the user chooses Bundled. A plugin cache is not a repository
authoring destination. Recheck the chosen target before writing.

## 2. Collect workflow settings

Ask for a short description, then control mode: leave the runner to
choose, pin `interactive`, or pin `synchronous`. Interactive workflows
pause at approval/question gates. Synchronous workflows auto-approve
approval steps; child questions use supplied decisions or report that
human input is needed. Do not describe synchronous mode as permission
bypass. Provider permissions are separate preflight answers.

Ask whether the workflow needs inputs. For each input collect `name`,
`prompt`, optional `default`, optional `from-context`, and optional
`validate`/`extract` regexes. Read the canonical schema reference in
`docs/wise/workflows.md` before using additional fields. Inputs become
`{{name}}` template values. A missing required answer must remain a
preflight question, not an invented value.

## 3. Collect steps and dependencies

Repeat until the user selects Done. Every step has a unique lowercase
`id`, a description when useful, and one supported type:

- `agent`: prompt or a referenced prompt file, provider/model/effort or
  a tuning group, optional timeout and resume policy. For outputs,
  collect a JSON schema and an explicit `outputs` list of properties.
  `skill:` is a Claude-specific convenience and must not be offered as
  portable execution on other providers. Use a plain prompt for a
  workflow intended to run across providers.
- `bash`: command, timeout, optional outputs. The first output receives
  trimmed stdout. Explain the exact command in the final preview.
- `approval`: message and dependencies. Runtime answers approve/reject.
- `ask`: message, choices, whether free text is allowed, and output name.
- `units`: choose the supported `ticket` or `plan` pipeline, items input,
  parallelism, model-phase tuning groups and caps. Read the current
  bundled `ticket-auto` or `impl-plan-auto` definition for the exact
  pipeline shape; do not invent phase fields.

Offer prior step IDs as `depends_on` choices. Keep independent work
parallel by leaving unrelated dependencies absent. Choose a trigger
rule only where needed: `all-success` (default), `one-success`,
`all-done`, `none-failed`, or `none-failed-min-one-success`. `when` is
an expression or supported list of expressions evaluated by the engine.
Never write a cycle or a dependency on an unknown step.

For model steps, ask whether shared tuning groups are useful. Read
`"$HOME/.local/share/wise/bin/wise-engine" --wise-host "$WISE_HOST" models` for catalogs.
Groups use mapping defaults such as
`{harness: codex, model: inherit, effort: medium}`. Keep harness/model
choices available at run preflight unless the author explicitly pins
them. Provider permission choices remain explicit runtime questions.
Read `engine/engine.sh list-agents` for role names when composing role
instructions; do not copy the retired v1 `agents` roster syntax.

Optional steps use the canonical `step-select` block. Resource limits
use `profiles.<profile>.caps` and referenced cap names. Inspect bundled
examples and compile rather than copying v1 profile or tuning syntax.

## 4. Preview and validate

Render complete YAML beginning with `version: 2`, plus a README with
the purpose, inputs, step table, provider requirements and run command.
A small Mermaid DAG may accompany the preview. Omit empty optional
blocks. Do not make unrelated files or invoke skill-creator.

Compile a temporary candidate through the same public engine:

```bash
"$HOME/.local/share/wise/bin/wise-engine" --wise-host "$WISE_HOST" compile-check <candidate.yaml>
```

Fix every error, relay relevant warnings, and remove the temporary
candidate after the preview. Unsupported Unicode case-insensitive
backreferences must be reported as validation errors; do not silently
rewrite the input rule.

Show the exact destination and files, then ask Create or Keep editing.
On Create, recheck collisions, make the folder, write `workflow.yaml`
and `README.md`, and compile the final path. If validation fails, fix
only the generated files and rerun it. Do not report success while the
final definition is invalid.

## 5. Finish

Show the saved paths and `/wise-workflow-run <name>`. A bundled workflow
requires a repository version bump when committed, per CONTRIBUTING;
do not commit or publish it unless the user requested that action.
Never launch the workflow merely because authoring succeeded.
