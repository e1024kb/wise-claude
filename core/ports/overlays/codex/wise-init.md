Genuinely divergent prose in wise-init — Claude-plugin semantics
(plugin.json version, `/plugin install wise@…` cache wipe,
"Claude"-the-agent wording) replaced with installer-pack semantics.
Hunks apply to the skill body AFTER the mechanical env-var rewrite;
each find-text must match exactly once.

<<<<<<<
Before 0.41.0, every workflow-adjacent wise skill ran
=======
Every workflow-adjacent wise skill used to run
>>>>>>>

<<<<<<<
That's inside the plugin install dir on purpose — it gets wiped on
every `/plugin install wise@…`, which is exactly the invalidation
signal we want: "the plugin updated, something new might be
required, user should re-init".
=======
That's inside the shared install root on purpose — `./install.sh codex
--force` deletes it when it re-lays the pack, which is exactly the
invalidation signal we want: "the pack was reinstalled, something new
might be required, user should re-init".
>>>>>>>

<<<<<<<
`init.sh`). Read them into per-dep Claude-side variables — referred
to below as `PY_STATUS`, `PY_BINARY`, `PY_VERSION`, `PY_MODULE_*`.
=======
`init.sh`). Read them into per-dep variables in your own working
memory — referred to below as `PY_STATUS`, `PY_BINARY`, `PY_VERSION`,
`PY_MODULE_*`.
>>>>>>>

<<<<<<<
  them to run the commands in their own terminal. Claude doesn't
=======
  them to run the commands in their own terminal. The wizard doesn't
>>>>>>>

<<<<<<<
Claude-side state:
=======
your working state:
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
  "plugin_version": "<contents of the pack's .wise-version marker, or empty>",
>>>>>>>

<<<<<<<
Read the plugin version:
=======
Read the pack version (`install.sh` writes the `.wise-version`
marker at the shared root; when it's absent — e.g. a hand-copied
pack — use the empty string):
>>>>>>>

<<<<<<<
python3 -c 'import json; print(json.load(open("'"${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/codex}"'/.claude-plugin/plugin.json"))["version"])'
=======
cat "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/codex}/.wise-version" 2>/dev/null || true
>>>>>>>

<<<<<<<
or after `/plugin install wise@…` (which wipes the cache by design).
=======
or after `./install.sh codex --force` (which deletes the cache by
design).
>>>>>>>
