---
name: wise-init
description: >-
  Set up wise's Python 3.11+ runtime and managed engine dependencies, check the engine,
  report provider and connector readiness, and cache the results. GitHub, provider
  logins, SSH, MCP connectors, and markitdown are optional until a selected action
  needs them. Preserve earlier skip decisions on repeat runs. Use when the user says
  "init wise", "set up wise", "install wise deps", "first-time setup", or types /wise-init.
argument-hint: ""
allowed-tools: Read, AskUserQuestion, Bash(bash:*), Bash(python3:*), Bash(printf:*), Bash(test:*), Bash(cat:*), Bash(uv:*), Bash(mise exec:*)
---

# /wise-init

Before asking any user question, read and follow the
[question lifecycle](../../references/workflow-host-control.md#keep-asynchronous-questions-open).
Keep asynchronous prompts open until answered; this rule does not authorize
questions in autonomous or otherwise prompt-free procedures.

Set up the Python runtime, then report the optional capabilities the user selects.
Select the conductor host explicitly from the current session: `claude`, `codex`,
`cursor`, or `grok`. Set `WISE_HOST` to that host. Never infer it from installed
provider CLIs. Resolve `WISE_PLUGIN_ROOT` from this loaded skill's location, two
directories above its skill folder, rather than searching for a newer cache version.
The registry lives at `$HOME/.local/share/wise/init/<host>.json`. Engine dependencies
live in the configured plugin data root. No init state is written into the plugin cache.

## 1. Read existing decisions

```bash
"${WISE_PYTHON:-python3}" "${WISE_PLUGIN_ROOT}/scripts/init-registry.py" --host "$WISE_HOST" read
```

The helper reads a legacy `.wise-init-registry.yaml` from this plugin root only when
this host has no new registry. Its first write preserves the full legacy document
and optional entries in the new location. Never edit or remove that cache file.
Preserve `skipped: true`, missing tools, unauthenticated logins, connector failures,
and history. Retain all earlier optional-connector skips, including issue trackers. Do not
ask again about a skipped capability unless the user asks to revisit it.

Tell the user: "I'll check Python 3.11+, prepare wise's managed engine environment,
and report the optional tools you want to configure. Earlier skips stay in place."

## 2. Python 3.11 or newer

```bash
bash "${WISE_PLUGIN_ROOT}/scripts/init.sh" probe-python
```

Read the bare `STATUS`, `BINARY`, and `VERSION` fields as Python probe results.
`WISE_PYTHON`, when set, selects the interpreter; otherwise it uses `python3` on PATH, including a mise shim when present.

- `STATUS=ok`: report the interpreter and version, then continue.
- `STATUS=missing` or `too-old`: show the detected version and ask which installation
  path the user prefers: their package manager, `mise use -g python@3.12`, or manual
  installation. Explain that `WISE_PYTHON` can select an existing Python 3.11+ binary.
  The user runs system installation commands in their own terminal. Offer
  `Done - re-probe` and `Abort init`, then re-run the probe after they respond.

Python packages are installed only in the managed engine environment. Never use a
user-site install, change system Python packages, or override package-manager protections.

## 3. Managed engine dependencies and self-check

```bash
bash "${WISE_PLUGIN_ROOT}/scripts/bootstrap-deps.sh"
```

This installs the exact hashed requirements in a managed virtual environment and
prints `READY:<managed-python-path>`. It does not require a provider login,
GitHub, or an optional connector. Repeated calls reuse the matching environment;
concurrent calls share an installation lock.

- `BOOTSTRAP:need-python`: return to the Python probe.
- `BOOTSTRAP:install-failed`: show stderr and stop engine setup. Keep optional
  decisions intact. The next invocation retries an incomplete installation.
- `READY:`: bootstrap refreshes the Python and engine registry fields while
  retaining optional entries. Continue with:

```bash
bash "${WISE_PLUGIN_ROOT}/engine/engine.sh" version
bash "${WISE_PLUGIN_ROOT}/engine/engine.sh" daemon status
```

The version line identifies `python`. A stopped daemon is normal; the first client
starts it. A version mismatch identifies an older running build. Run `daemon stop`
only when it can stop without cancelling active work, then verify its status.
If it reports active runs, record `stale-busy` and leave them running.

Register the selected host with a concrete preview, then apply the setup requested
by this init invocation:

```bash
bash "${WISE_PLUGIN_ROOT}/engine/engine.sh" setup-host --host "$WISE_HOST" --plugin-root "$WISE_PLUGIN_ROOT"
bash "${WISE_PLUGIN_ROOT}/engine/engine.sh" setup-host --host "$WISE_HOST" --plugin-root "$WISE_PLUGIN_ROOT" --apply
bash "${WISE_PLUGIN_ROOT}/engine/engine.sh" host-doctor --host "$WISE_HOST"
"${WISE_PYTHON:-python3}" "${WISE_PLUGIN_ROOT}/scripts/init-registry.py" --host "$WISE_HOST" refresh-runtime
```

Show the preview's configuration paths and changes before applying. For a custom
configuration path, pass the same absolute `--config` to setup, doctor, and registry
refresh/check. Follow the shared [host control reference](../../references/workflow-host-control.md)
for host-specific registration, exact Claude installation selectors, and rollback.
The stable launcher is `$HOME/.local/share/wise/bin/wise-engine`; select its host
with `--wise-host "$WISE_HOST"` or `WISE_HOST`.

For Cursor, run `cursor-agent mcp enable wise-engine` as part of this authorized
setup before `cursor-agent mcp list-tools wise-engine`; its native server approval
is separate from writing configuration. For other hosts, use their native checks
in the shared reference.

Doctor validates registration and launcher files; it does not prove a native host
session connected. Reload the host as its setup instructions require, then call
`wise_status` when available. Record a successful call as host verified. If unavailable,
report registration checked but native session unverified. Preserve connection errors.
No Claude installation or login is required to use a different conductor host.

## 4. Optional capabilities

Offer to inspect optional capabilities that the user has not already skipped.
The user may skip any of them; no optional check blocks Python engine readiness.
Do not install a CLI or perform a login merely because a probe reports it missing.
Show the command for the capability the user chooses to configure, then re-probe
after the user completes it. Never print credential values.

### Provider CLIs

```bash
bash "${WISE_PLUGIN_ROOT}/engine/engine.sh" auth
```

Report each harness's `INSTALLED`, `LOGIN`, and `LOGIN_CMD` fields. All five
providers are optional: `claude`, `codex`, `cursor-agent`, `gemini`, and `grok`.
An unsuccessful aggregate auth exit is diagnostic and does not invalidate the
managed runtime. A workflow probes only the providers its enabled steps require.
Explain the selected provider's identity before discussing its login or billing.
Show login commands for the user to run; do not run them yourself.

### GitHub CLI

Only inspect GitHub setup when the user selects GitHub actions:

```bash
bash "${WISE_PLUGIN_ROOT}/scripts/init.sh" probe-gh
```

Report `STATUS`, `VERSION`, `AUTHENTICATED`, and `LOGIN`. If needed, show
`brew install gh` or the user's package-manager equivalent, then `gh auth login`.
Offer `Done - re-probe` and `Skip for now`. A skipped GitHub login affects GitHub
operations, not the core engine or workflows that do not use GitHub.

### Git over SSH

Only inspect SSH when the user selects a workflow with SSH remotes:

```bash
bash "${WISE_PLUGIN_ROOT}/scripts/init.sh" probe-git-ssh
```

Report `STATUS`, `AGENT`, `HOST`, and `DETAIL`. For `denied`, explain loading the
appropriate key with `ssh-add` in the user's terminal. Distinguish an unreachable
network from an authentication failure. HTTPS remotes do not require this check.
Record skipped setup without repeating it on the next init.

### Provider MCP connectors

Only inspect a provider's connector inventory when the user requests it. The
existing Claude-specific inventory probe is:

```bash
bash "${WISE_PLUGIN_ROOT}/scripts/init.sh" probe-mcp
```

Label this result as the Claude CLI inventory, not the current host's inventory.
Report `CONNECTED`, `NEEDS_AUTH`, and `FAILED` separately. The connected count is
the number of entries in `CONNECTED`, not `COUNT`. A failed connection is not
necessarily an authentication problem. Show the relevant provider's configuration
or login instruction and offer `Done - re-probe` or `Skip for now`.

A connector may have its own runtime dependency. Check that dependency only for
the chosen connector; it is not an engine prerequisite.

### Markitdown

Only inspect file extraction when the user selects it:

```bash
bash "${WISE_PLUGIN_ROOT}/scripts/init.sh" probe-markitdown
```

If missing and `UV=ok`, offer `Install` or `Skip`. On an explicit install choice,
run `uv tool install 'markitdown[all]'`, or the equivalent through the detected mise
installation. This creates an isolated optional tool environment. If uv is absent,
show its installation instructions for the user to run, or record a skip. Never
replace the engine's locked requirements with optional extraction packages.

## 5. Save optional results and report

Merge the actual optional results into the registry without replacing the runtime
fields written by the host-specific runtime refresh. Include `skipped: true` for explicit skips. Existing
skipped entries remain unless the user chose to revisit them. Write the optional result entries with the registry helper:

```bash
"${WISE_PYTHON:-python3}" "${WISE_PLUGIN_ROOT}/scripts/init-registry.py" --host "$WISE_HOST" write '<JSON containing the optional deps entries>'
```

The current registry is standard-library readable. If the helper reports that an
older registry cannot be read, re-run bootstrap to migrate it with the managed
parser, then retry the merge. Never erase an unreadable registry to make the check pass.

Summarize Python and managed-engine readiness first. Then list the optional
capabilities inspected, including missing, failed, unavailable, and skipped states.
Name the registry path. Claim host MCP readiness only after an actual tool call
succeeded; runtime readiness and provider logins do not establish host registration.
