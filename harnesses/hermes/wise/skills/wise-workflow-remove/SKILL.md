---
name: wise-workflow-remove
description: >-
  Delete a user-authored workflow definition from `the wise data dir's
  workflows/definitions/` — handles both layouts (`<name>/workflow.yaml`
  folder form AND legacy `<name>.yaml` flat form). Refuses to touch
  bundled workflows — they ship with the plugin and are replaced by a
  reinstall, not a remove. Invoked as `/wise-workflow-remove`. Use when
  the user says "remove workflow <name>", "delete workflow <name>", "drop
  my custom workflow", or types `/wise-workflow-remove`.
---

## Harness adaptation note

This skill was authored for Claude Code and adapted for Nous Research Hermes Agent. Where the steps below reference Claude-specific tools, substitute:

- **AskUserQuestion** — ask the user the same question in plain chat and wait for their reply.


# /wise-workflow-remove — delete a user workflow

## Why this skill exists

`/wise-workflow-create` writes a YAML to `${WISE_DATA_DIR:-$HOME/.local/share/wise}/workflows/definitions/`.
This skill is the matching removal path. Bundled workflows are
immutable from the plugin's perspective (they live in the read-only
plugin install directory) — this skill refuses to delete them rather
than silently failing or producing confusing errors.

## Arguments

Read `$ARGUMENTS`. The first whitespace-separated token is the
`name` — the kebab-case workflow name (required). Matches the
filename without the `.yaml` extension. When `$ARGUMENTS` is empty,
stop with a clear error pointing at `/wise-workflow-list` to discover
available workflows.

## Procedure

### 1. Parse and validate

Read `name` from `$ARGUMENTS`. Reject anything that doesn't match
`^[a-z][a-z0-9]*(-[a-z0-9]+)*$` — the same kebab-case shape
`/wise-workflow-create` enforces; anything else is not a filename we
would have written.

### 2. Refuse bundled

Check both layouts under the bundled root — a bundled workflow in
either form is off-limits:

```bash
test -f "${WISE_PLUGIN_ROOT}/workflows/${name}/workflow.yaml" \
  || test -f "${WISE_PLUGIN_ROOT}/workflows/${name}.yaml"
```

If either exits 0, the target is a bundled workflow. Stop with:

```
Refusing to delete bundled workflow <name>.

Bundled workflows ship with the wise plugin and are replaced by a
reinstall. If you want to override this workflow locally, create a
user version with the same name at:

  ${WISE_DATA_DIR:-$HOME/.local/share/wise}/workflows/definitions/<name>/workflow.yaml

The user version wins at run time.
```

### 3. Locate the user definition

Check both layouts and remember which one matched:

```bash
if [ -f "${WISE_DATA_DIR:-$HOME/.local/share/wise}/workflows/definitions/${name}/workflow.yaml" ]; then
  TARGET_KIND=folder
  TARGET="${WISE_DATA_DIR:-$HOME/.local/share/wise}/workflows/definitions/${name}"
elif [ -f "${WISE_DATA_DIR:-$HOME/.local/share/wise}/workflows/definitions/${name}.yaml" ]; then
  TARGET_KIND=flat
  TARGET="${WISE_DATA_DIR:-$HOME/.local/share/wise}/workflows/definitions/${name}.yaml"
else
  echo "No user workflow named ${name}. Run /wise-workflow-list to see what's registered."
  exit 1
fi
```

### 4. Confirm

Use `AskUserQuestion`. The delete description differs by layout —
folder form removes the whole folder (including any sibling
`templates/` or `prompts/` the user put there).

- Question: `Delete user workflow <name>?`
- Header: `Confirm delete`
- Options:
  - `Delete` — for folder form: `This removes ${WISE_DATA_DIR:-$HOME/.local/share/wise}/workflows/definitions/<name>/ entirely (workflow.yaml + any sibling templates/ and prompts/). Any in-flight runs of this workflow remain in ~/.local/share/wise/runs/<cwd-slug>/ and are not affected.`; for flat form: `This removes ${WISE_DATA_DIR:-$HOME/.local/share/wise}/workflows/definitions/<name>.yaml. Any in-flight runs of this workflow remain in ~/.local/share/wise/runs/<cwd-slug>/ and are not affected.`
  - `Keep` — `Abort without changing anything.`

On `Keep`, stop without touching the file.

### 5. Delete

On `Delete`, remove whichever form matched:

```bash
if [ "$TARGET_KIND" = folder ]; then
  rm -rf "$TARGET"
else
  rm "$TARGET"
fi
```

Then confirm:

```
Removed user workflow <name>.

Any existing runs of this workflow under
  ~/.local/share/wise/runs/<cwd-slug>/
are unaffected. Resume them with:
  /wise-workflow-resume <run-ulid>
```

## Guardrails

- Never remove a bundled workflow.
- Never remove a run directory — resuming a run after its definition
  was deleted is a legitimate (if narrow) use case.
- Do not invoke any other skill.
