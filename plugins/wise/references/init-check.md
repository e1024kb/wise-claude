# init-check

Use the current conductor host explicitly: `claude`, `codex`, `cursor`, or `grok`.
Set `WISE_HOST` from the session, never from installed provider CLIs. Resolve the
loaded plugin root as described in [host control](workflow-host-control.md).
Run its guarded `refresh-host` before checking init. An upgrade refresh invalidates
previous runtime/registration probes; run init to rebuild that evidence while
preserving optional skips. Do not interpret a successful file refresh as a native
session connection.

```bash
"${WISE_PYTHON:-python3}" "${WISE_PLUGIN_ROOT}/scripts/init-registry.py" --host "$WISE_HOST" check
```

`INIT:ok` confirms this host's recorded plugin root, managed Python runtime and
requirements fingerprint, and current launcher/registration evidence. It does not
prove native MCP connectivity or provider authentication. Another host's registry
cannot satisfy this check.

For `INIT:uninit`, `INIT:stale:*`, `INIT:dep-missing:*`, or missing Python, run
**/wise-init** to prepare Python 3.11+, refresh this host's registration and verify
its session. Read-only callers may report missing setup and stop.

Init state lives at `$HOME/.local/share/wise/init/<host>.json`. Legacy cache state
is read-only migration input. Preserve all optional-connector skips, including issue trackers. `bootstrap-deps.sh --probe` never installs or writes state. Runtime-only
bootstrap works without a host, but cannot establish host initialization.
