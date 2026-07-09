Genuinely divergent prose in wise-init — Claude-plugin semantics
(plugin.json version, `/plugin install wise@…` cache wipe,
"Claude"-the-agent wording) replaced with installer-pack semantics.
Hunks apply to the skill body AFTER the mechanical env-var rewrite;
each find-text must match exactly once.

<<<<<<<
Before 0.41.0, every workflow-adjacent wise skill ran
=======
Historically, every workflow-adjacent wise skill ran
>>>>>>>

<<<<<<<
**The registry lives at `${WISE_PLUGIN_ROOT}/.wise-init-registry.yaml`.**
That's inside the plugin install dir on purpose — it gets wiped on
every `/plugin install wise@…`, which is exactly the invalidation
signal we want: "the plugin updated, something new might be
required, user should re-init".
=======
**The registry lives at `${WISE_PLUGIN_ROOT}/.wise-init-registry.yaml`**
— i.e. at the shared install root. That's on purpose: rerunning
`./install.sh opencode --force` deletes the registry, so a reinstall
naturally re-probes — exactly the invalidation signal we want: "the
pack updated, something new might be required, user should re-init".
>>>>>>>

<<<<<<<
`init.sh`). Read them into per-dep Claude-side variables — referred
=======
`init.sh`). Read them into per-dep working variables — referred
>>>>>>>

<<<<<<<
  them to run the commands in their own terminal. Claude doesn't
=======
  them to run the commands in their own terminal. The agent doesn't
>>>>>>>

<<<<<<<
Claude-side state:
=======
working state:
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
  "plugin_version": "<contents of the .wise-version file, or empty>",
>>>>>>>

<<<<<<<
Read the plugin version:
=======
Read the pack version (from the `.wise-version` file `./install.sh`
writes at the shared root; if the file is absent, use an empty
string for `plugin_version`):
>>>>>>>

<<<<<<<
python3 -c 'import json; print(json.load(open("'"${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/opencode}"'/.claude-plugin/plugin.json"))["version"])'
=======
cat "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/opencode}/.wise-version" 2>/dev/null || true
>>>>>>>

<<<<<<<
or after `/plugin install wise@…` (which wipes the cache by design).
=======
or after reinstalling the pack (`./install.sh opencode --force`
deletes the cache by design).
>>>>>>>
