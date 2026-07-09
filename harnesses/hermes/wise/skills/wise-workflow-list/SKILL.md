---
name: wise-workflow-list
description: >-
  List every workflow available to the wise plugin — both the bundled
  defaults that ship with the plugin and the user-authored ones under the
  wise data dir's workflows/definitions/. Read-only. Invoked as
  `/wise-workflow-list`. Use when the user says "list workflows", "show
  workflows", "which workflows are available", "what workflows can I run",
  or types `/wise-workflow-list`.
---

# /wise-workflow-list — list available workflows

> **Shared-file resolution:** `${WISE_PLUGIN_ROOT}` defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/hermes` — where `./install.sh hermes` puts this pack. Export `WISE_PLUGIN_ROOT` only to override.

## Why this skill exists

Users need a quick way to see which workflows they can run. Workflows
come from two places: bundled defaults under `${WISE_PLUGIN_ROOT}/workflows/`
(shipped with the plugin) and user-authored ones under
`${WISE_DATA_DIR:-$HOME/.local/share/wise}/workflows/definitions/` (written by
`/wise-workflow-create`). Under each root, a workflow can live in one of
two layouts — `<name>/workflow.yaml` (folder form, preferred) or
`<name>.yaml` (legacy flat form). This skill lists both layouts from
both roots and flags any name collisions (a user def shadows a bundled
def of the same name).

## Arguments

This skill takes no arguments. Ignore anything the user types beyond
the skill name.

## Procedure

### 1. Init-check + list — in ONE message

Run the init-check per `${WISE_PLUGIN_ROOT}/references/init-check.md`,
firing `init-registry.py check` and the data call
`workflows.py list-defs` together in one message. On `INIT:ok`, use the
`list-defs` output and jump to §2; otherwise follow the reference's
fallback. This skill is read-only, so on `BOOTSTRAP:need-python` it
relays the `OPTION:` lines and stops rather than driving an install
loop.

### 2. Interpret the `list-defs` output

Stdout is a JSON array of `{name, description, source, shadowed}`
objects, with user entries first and bundled entries after (folder
form wins on same-root collision; user wins on cross-root
collision).

### 3. Render

Partition the entries by `source` (bundled vs user) and render:

```
Bundled workflows (<N>):

  example-workflow
    Reference workflow that exercises every step type. Harmless to run.

User workflows (<M>):

  my-release-flow
    My custom release checklist.

Run any workflow with:
  /wise-workflow-run <name>
```

Rules:

- Entries with `shadowed: true` are bundled defs that a user def of
  the same name has overridden — print them under the Bundled list
  with a trailing `(shadowed by user <name>)` line. The user entry
  (which appears earlier in the JSON) does NOT get a shadows-note;
  it's the authoritative one at run time.
- If either list is empty, still print its heading with `(none)`:
  `User workflows (0):\n\n  (none)`.
- `description` is printed wrapped to a single paragraph; the engine
  already handled YAML folding (`>-`, `>`) before emitting the JSON.
- Footer: end with the one-line invocation hint above.

## Guardrails

- Read-only. Never write to any file.
- Do not invoke any other skill.
- Do not spawn subagents.
