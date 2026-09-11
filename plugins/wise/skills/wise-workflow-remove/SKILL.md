---
name: wise-workflow-remove
description: >-
  Delete a user-authored workflow definition from
  `the engine-selected user definition directory` — handles both
  layouts (`<name>/workflow.yaml` folder form AND legacy
  `<name>.yaml` flat form). Refuses to touch bundled workflows —
  they ship with the plugin and are replaced by a reinstall, not a
  remove. Invoked as `/wise-workflow-remove` (bare alias) or
  `/wise:wise-workflow-remove` (canonical). Use when the user says
  "remove workflow <name>", "delete workflow <name>", "drop my
  custom workflow", or types `/wise-workflow-remove`.
argument-hint: "<name>"
model: opus
effort: low
allowed-tools: Read, Bash(rm:*), Bash(test:*), Bash(bash:*), AskUserQuestion
---

# /wise-workflow-remove - remove a user definition

Before asking any user question, read and follow the
[question lifecycle](../../references/workflow-host-control.md#keep-asynchronous-questions-open).
Keep asynchronous prompts open until answered; this rule does not authorize
questions in autonomous or otherwise prompt-free procedures.

First read [host control](../../references/workflow-host-control.md). Resolve the
loaded installation, set `WISE_HOST` to this conductor and `WISE_PLUGIN_ROOT`
to that installation. Use its managed launcher for shell commands. Follow the
reference's diagnostics and explicit-answer fallback when MCP or a native picker
is unavailable. Conductor host and child provider are independent.


The first argument is the workflow name. Require
`^[a-z][a-z0-9]*(-[a-z0-9]+)*$`; missing or invalid input stops with a
pointer to `/wise-workflow-list`.

## Locate through canonical roots

```bash
"$HOME/.local/share/wise/bin/wise-engine" --wise-host "$WISE_HOST" definition-roots
"$HOME/.local/share/wise/bin/wise-engine" --wise-host "$WISE_HOST" list-defs
```

Use the returned `user_root` and `bundled_root`, not a duplicated
home-directory or Claude-specific default. Check both folder
`<name>/workflow.yaml` and flat `<name>.yaml` forms. If either exists
under the bundled root, refuse: `Refusing to delete bundled workflow
<name>. Create a user override with a different name or edit the source
repository.` Never remove files from the plugin installation.

Under the user root, prefer folder form when both exist. If neither
exists, say `No user workflow named <name>.` and stop. Verify the real
path remains inside the canonical user root; refuse a definition whose
symlink resolves outside it. Record the exact matched path and form.

## Confirm the exact removal

Show the full path and ask Delete or Keep through the host's structured
picker. Folder deletion removes its `workflow.yaml` plus sibling
prompts/templates/README. Flat deletion removes only that YAML file.
Saved run history is not part of the deletion. On Keep, stop unchanged.

After Delete, recheck the target and its containment. Remove only the
selected folder or flat file, quoting the complete path. Do not touch
another definition, shared data directory, active run, or history.

Report `Removed user workflow <name> from <path>.` If an alternate flat
or folder definition remains, say it is now the discovered definition;
do not delete it without a separate explicit choice. Never delete a
second path merely to make the name disappear.
