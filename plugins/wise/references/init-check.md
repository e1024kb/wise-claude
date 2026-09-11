# init-check

The init registry caches Python runtime readiness and optional setup decisions.
The engine launcher independently prepares its managed dependencies when needed.

Run the check with the selected Python interpreter:

```bash
"${WISE_PYTHON:-python3}" "${CLAUDE_PLUGIN_ROOT}/scripts/init-registry.py" check 2>/dev/null || true
```

- `INIT:ok`: the recorded Python runtime matches the current managed environment
  and hashed requirements. Continue with the caller's engine or helper command.
- `INIT:uninit`, `INIT:stale:*`, `INIT:dep-missing:*`, or no result: prepare the
  managed runtime, then repeat the caller's command:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap-deps.sh"
```

`READY:<managed-python-path>` means runtime setup completed. `BOOTSTRAP:need-python`
requires Python 3.11 or newer; show the installation options and let the user choose
how to install or select Python. `BOOTSTRAP:install-failed` means the managed
package installation failed; show stderr. Read-only callers may report the missing
runtime and stop instead of driving an installation walkthrough.

The `--probe` form never installs or writes the registry. Successful installation
refreshes the runtime entries while preserving optional connector skips. GitHub,
provider logins, SSH and connector runtimes are checked only by actions that need
them. `INIT:ok` does not prove host MCP registration or provider authentication.
