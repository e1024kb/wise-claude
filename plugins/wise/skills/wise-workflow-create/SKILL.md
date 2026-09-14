---
name: wise-workflow-create
description: >-
  Create a workflow from a free-form prompt. Automatically infer its name,
  settings, steps and dependencies, then ask for harness, model and effort
  for each model-executing step in sequence. Validate and save YAML + README
  without additional authoring questions. Use when the user says "create a
  workflow", "scaffold a workflow", "new workflow", "author a workflow", or
  invokes `/wise-workflow-create` or `/wise:wise-workflow-create`.
argument-hint: "[<workflow prompt> | --name <name> <workflow prompt>]"
allowed-tools: Read, Write, AskUserQuestion, Bash(mkdir:*), Bash(test:*), Bash(bash:*), Bash(pwd:*), Bash(dirname:*), Bash(python3:*), Bash(${WISE_PLUGIN_ROOT}/scripts/init-registry.py:*)
---

# /wise-workflow-create - author a v2 workflow

Before executing, follow [model fallback](../../references/workflow-host-control.md#model-fallback)
for unavailable models or delegation routes, including in autonomous procedures.

At every skill start, identify your main/child role and the current client
and GUI/TUI question tools, then read and follow the
[question lifecycle](../../references/workflow-host-control.md#keep-asynchronous-questions-open).
Keep asynchronous prompts open until answered; this rule does not authorize
questions in autonomous or otherwise prompt-free procedures.

First read [host control](../../references/workflow-host-control.md). Resolve the
loaded installation, set `WISE_HOST` to this conductor and `WISE_PLUGIN_ROOT`
to that installation. Use its managed launcher for shell commands. Follow the
reference's diagnostics and explicit-answer fallback when MCP or a native picker
is unavailable. Conductor host and child provider are independent.


Create `version: 2` definitions for the Python engine. Use the host's
structured picker for choices and text inputs, with the shared main-harness text
fallback when no permitted native control or rendered MCP form is usable. Keep accepted answers
across stages; never create v1 `prompt`, `loop`, or `interactive` steps.

## 1. Read the prompt and resolve the destination

Treat the entire `$ARGUMENTS` string as the workflow's free-form description,
not the first word as a name. Support an optional leading `--name <name>`;
validate that explicit name against `^[a-z][a-z0-9]*(-[a-z0-9]+)*$` before
any work, and reject a missing or invalid flag value without re-prompting.
Reject engine-reserved explicit names (`list`, `create`, `run`, `resume`,
`remove`, `status`), including legacy lone names, without re-prompting.
For compatibility, a lone valid slug is a name and uses the workflow description
already given in the conversation. With empty arguments, use the workflow request
in the conversation. Only if no workflow intent is available, ask once for a
free-form description. Do not ask the user to split it into steps.

Run the init check in `${WISE_PLUGIN_ROOT}/references/init-check.md`,
then read canonical roots and existing definitions:

```bash
"$HOME/.local/share/wise/bin/wise-engine" --wise-host "$WISE_HOST" definition-roots
"$HOME/.local/share/wise/bin/wise-engine" --wise-host "$WISE_HOST" list-defs
```

Derive a short valid name from the prompt unless explicitly supplied. If a derived
name is reserved or collides, append the first available numeric suffix starting
at `-2`, skipping reserved and existing names.
Reject an explicitly supplied existing name. Check both roots, including flat
`<name>.yaml` and folder `<name>/workflow.yaml` forms. Never overwrite.

Default destination: `<user_root>/<name>/workflow.yaml`, including inside a
marketplace clone. Do not ask for a destination. Use
`<repo>/plugins/wise/workflows/<name>/` only when the user explicitly requests a
bundled workflow and the clone contains `.claude-plugin/marketplace.json` and
`plugins/wise/.claude-plugin/plugin.json`. A plugin cache is not a repository
authoring destination. Recheck the chosen target before writing.

## 2. Automatically draft the workflow

Read the canonical v2 schema in `docs/wise/workflows.md` when available in the
checkout, otherwise the loaded plugin's `engine/wise_engine/defs.py` and bundled
workflow examples. Infer the description, inputs, step IDs, types, prompts,
outputs and dependencies from the user's intent. This is the default behavior;
no opt-in flag or separate planning request is needed.

Do not ask for a name, description, control mode, input schema, step count,
step types, dependencies, tuning groups, or approval of the draft. Preserve
explicit constraints and choose routine details yourself. Leave control mode
unset unless requested. Do not infer synchronous auto-approval or permission
bypass. Required runtime values not supplied by the prompt become workflow
inputs with clear preflight questions, not invented answers or authoring prompts.
For literal enums use anchored alternation validation such as `^(auto|ask)$`.

Split the intent into concrete steps with unique lowercase IDs and sufficient
prompts to run independently of this authoring conversation. Preserve requested
order and data flow with `depends_on`; leave unrelated work independent. Use
`agent` for model work, `bash` for known deterministic commands, `approval` or
`ask` only for gates required by the requested workflow, and `units` only for a
supported ticket or plan pipeline. Do not add execution, publishing or other
side effects beyond the described workflow. Authoring never executes these steps.

For agent outputs, define the JSON schema and explicit `outputs` properties
needed downstream. Prefer portable prompts; `skill:` forces Claude and must not
be combined with another harness. Read `list-agents` through the managed engine
when composing role instructions. For `units`, inspect bundled `ticket-auto` or
`impl-plan-auto` for phase bindings and caps instead of inventing fields.
Use canonical `step-select`, trigger rules and profiles only when needed.

Show a concise ordered step table with purpose, type and dependencies, then
proceed directly to tuning. This is an informational preview, not a question.

## 3. Select harness, model and effort for each step

Read the current catalog; never hardcode provider, model or effort options:

```bash
"$HOME/.local/share/wise/bin/wise-engine" --wise-host "$WISE_HOST" models
```

Visit model-executing steps in the displayed order. For each step, complete these
questions before moving to the next step. Include the step ID and purpose in
every question:

1. Ask which harness to use, using catalog harnesses.
2. After that answer arrives, ask which model from that harness's catalog to use.
   Show model labels and descriptions, and retain the exact model ID.
3. After that answer arrives, ask which of that model's supported efforts to use.
   If there are no supported efforts, show "Effort: not supported by this model"
   and omit `effort`; do not invent an effort option. If only one value exists,
   still present it for explicit selection.

**MUST: ask every harness, model and effort question explicitly.** Prefer the
host's GUI/TUI single-choice picker, using the same controls as predefined
workflow preflight. Follow the shared question lifecycle and populate the
actual tool `options` field with selectable catalog values. Paginate choices
when the picker limits option counts.

If no permitted native picker or rendered MCP form is usable, use the shared
main-harness text fallback with the same catalog values and stage ordering.
Preserve answers in the conversation; never invent defaults or save a partial
workflow. An asynchronous display acknowledgement is not an
answer: keep the question pending until the user submits a selection.

Do not batch
all harness questions ahead of models and efforts, reuse one step's answers for
another, or silently accept recommended/preselected defaults. Explicit choices
already supplied for a particular step count as answers; ask only its missing
choices. If the user changes a harness or model, discard incompatible dependent
answers and collect them again. On cancellation stop without saving a partial
workflow. If the catalog cannot be loaded, report the failure instead of guessing.

Persist selected `harness`, `model` and supported `effort` directly on each
`agent` step so runtime group defaults cannot replace the author's choices.
For `units`, collect the same sequence separately for every model phase and
bind each to its own `locked: true` tuning group with the selected mapping
under `default`; do not share groups across steps or phases by default.
For `bash`, `approval` and `ask`, show tuning as not applicable and skip the
provider questions because those types do not run models.

Provider permissions and any installation/login requirements remain runtime
preflight concerns. These authoring selections do not grant execution permission.

## 4. Validate and save

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

Show the exact destination and files, then save automatically after all required
tuning answers are received. Do not ask Create/Keep editing or another
confirmation. Recheck reserved names and collisions, make the folder, write `workflow.yaml`
and `README.md`, and compile the final path. If validation fails, fix
only the generated files and rerun it. Do not report success while the
final definition is invalid.

## 5. Finish

Show the saved paths and `/wise-workflow-run <name>`. A bundled workflow
requires a repository version bump when committed, per CONTRIBUTING;
do not commit or publish it unless the user requested that action.
Never launch the workflow merely because authoring succeeded.
