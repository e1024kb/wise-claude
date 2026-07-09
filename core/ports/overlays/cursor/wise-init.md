Genuinely divergent prose in wise-init — Claude-plugin semantics
(plugin.json version, `/plugin install wise@…` cache wipe,
"Claude"-the-agent wording) replaced with installer-pack semantics.
Hunks apply to the skill body AFTER the mechanical env-var rewrite;
each find-text must match exactly once.

<<<<<<<
That's inside the plugin install dir on purpose — it gets wiped on
every `/plugin install wise@…`, which is exactly the invalidation
signal we want: "the plugin updated, something new might be
required, user should re-init".
=======
That's inside the pack's install dir on purpose — running
`./install.sh cursor --force` deletes it, which is exactly the
invalidation signal we want: "the pack was reinstalled, something
new might be required, user should re-init" — the next probe-gated
skill (or a re-run of `/wise-init`) re-probes from scratch.
>>>>>>>

<<<<<<<
`init.sh`). Read them into per-dep Claude-side variables — referred
to below as `PY_STATUS`, `PY_BINARY`, `PY_VERSION`, `PY_MODULE_*`.
=======
`init.sh`). Read them into per-dep variables held in your working
memory — referred to below as `PY_STATUS`, `PY_BINARY`,
`PY_VERSION`, `PY_MODULE_*`.
>>>>>>>

<<<<<<<
  them to run the commands in their own terminal. Claude doesn't
=======
  them to run the commands in their own terminal. The agent doesn't
>>>>>>>

<<<<<<<
Claude-side state:
=======
working memory:
>>>>>>>

<<<<<<<
Same pattern as §2, but with `init.sh probe-node`. Bare keys
`STATUS`, `BINARY`, `VERSION`, `MAJOR` → `NODE_STATUS`,
=======
Same pattern as §2, but with `init.sh probe-node`:

```bash
bash "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/cursor}/scripts/init.sh" probe-node
```

Bare keys `STATUS`, `BINARY`, `VERSION`, `MAJOR` → `NODE_STATUS`,
>>>>>>>

<<<<<<<
Compose a JSON object from the three result blobs above plus the
plugin version:

=======
Compose a JSON object from the three result blobs above plus the
pack version:

>>>>>>>

<<<<<<<
  "plugin_version": "<contents of plugin.json's version field>",
=======
  "plugin_version": "<contents of the pack's .wise-version file>",
>>>>>>>

<<<<<<<
Read the plugin version:
=======
Read the pack version from the `.wise-version` file the installer
drops at the shared root — leave `plugin_version` empty if the file
isn't there:
>>>>>>>

<<<<<<<
python3 -c 'import json; print(json.load(open("'"${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/cursor}"'/.claude-plugin/plugin.json"))["version"])'
=======
cat "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/cursor}/.wise-version" 2>/dev/null || true
>>>>>>>

<<<<<<<
or after `/plugin install wise@…` (which wipes the cache by design).
=======
or after `./install.sh cursor --force` (which deletes the cache by
design).
>>>>>>>
