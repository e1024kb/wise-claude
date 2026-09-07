---
name: wise-init
description: >-
  First-time setup wizard — walk the user through installing wise's
  system deps (Python 3 + pyyaml/ulid/typing_extensions, bun or Node ≥24,
  the `claude` CLI login, gh CLI + `gh auth login`, markitdown for file-to-markdown extraction),
  self-check the workflow engine and its `wise-engine` MCP server, replace a daemon left
  running on an older engine build, check git over ssh from the engine's child environment,
  report the optional harness CLIs (codex, grok, gemini), and cache the probe results so
  workflow runs skip the live check.
  Idempotent — re-running only prompts for gaps.
  Invoked as `/wise-init` (bare alias) or `/wise:wise-init` (canonical).
  Use when the user says "init wise", "set up wise", "install wise deps",
  "first-time setup", "run the setup wizard", or types `/wise-init`.
argument-hint: ""
allowed-tools: Read, AskUserQuestion, Bash(bash:*), Bash(python3:*), Bash(printf:*), Bash(test:*), Bash(cat:*), Bash(uv:*), Bash(mise exec:*)
---

# /wise-init — first-time setup wizard

## Why this skill exists

Before 0.41.0, every workflow-adjacent wise skill ran
`scripts/bootstrap-deps.sh` as its first step — probing Python,
Node, and the gh CLI on every invocation. That was correct but
slow on the hot path and clumsy for fresh installs: the user got
piecemeal "install X, now install Y, now `gh auth login`" across
successive skill invocations instead of one guided walkthrough.

`/wise-init` is that walkthrough. It probes each dep in turn, shows
installer options with exact commands to paste when something's
missing, pauses for the user to run them, re-probes, and finally
writes a registry file the workflow engine consumes as a
fast-path on every subsequent run. Re-runs are cheap — the wizard
skips deps that are already present.

**The registry lives at `${CLAUDE_PLUGIN_ROOT}/.wise-init-registry.yaml`.**
That's inside the plugin install dir on purpose — it gets wiped on
every `/plugin install wise@…`, which is exactly the invalidation
signal we want: "the plugin updated, something new might be
required, user should re-init".

## Arguments

This skill takes no arguments. Ignore anything the user types beyond
the skill name.

## Procedure

### 1. Preamble

Print one short paragraph to the user introducing the flow. Keep
it under 4 lines:

```
First-time setup. I'll walk you through the system deps wise needs —
Python 3, bun or Node ≥24 (the workflow engine runtime), the claude
CLI login, the gh CLI (with auth), and markitdown (file → markdown text
extraction) — then self-check the engine and its MCP server and report
the optional harness CLIs (codex, grok, gemini). Re-runs are safe: I
skip what's already installed. After this I cache the probe results so
future workflow runs skip the live check.
```

### 2. Python (and its pip modules)

**2a. Probe.**

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/init.sh" probe-python
```

The probe emits **bare** `KEY=VALUE` lines — `STATUS`, `BINARY`,
`VERSION`, `MODULE_YAML`, `MODULE_ULID`, `MODULE_TYPING_EXTENSIONS`
(all three probes reuse the same bare key names by design; see
`init.sh`). Read them into per-dep Claude-side variables — referred
to below as `PY_STATUS`, `PY_BINARY`, `PY_VERSION`, `PY_MODULE_*`.

**2b. Handle the result.**

- **`PY_STATUS=ok` and all modules `ok`:** print one line
  `Python <ver> ✓ at <binary>` and move to §3. No AskUserQuestion
  needed — this is the happy path.

- **`PY_STATUS=ok` but at least one module is `missing`:** offer to
  pip-install the missing ones. `AskUserQuestion`:
  - Question: `Python <ver> is installed but these modules are missing: <list>. Install them now?`
  - Header: `pip install`
  - Options:
    - `Install (recommended)` — description: `Run: <PY_BINARY> -m pip install --user <missing pkgs>`
    - `Skip` — description: `Continue without these modules. wise's workflow engine will fail with an import error later.`
  - multiSelect: false

  On `Install`: run
  ```bash
  "<PY_BINARY>" -m pip install --user --quiet pyyaml python-ulid typing_extensions
  ```
  (substituting the mapping: `yaml → pyyaml`, `ulid → python-ulid`,
  `typing_extensions → typing_extensions`; only include the ones
  that were `missing`). Re-probe via §2a.

  **If pip exits with `error: externally-managed-environment` (PEP 668)** —
  almost always the case when `<PY_BINARY>` is the Homebrew system
  Python on macOS — do NOT silently fall back to
  `--break-system-packages`. Instead, pivot the user onto
  mise-managed Python, which doesn't have the lockdown:

  - `AskUserQuestion`:
    - Question: `pip refused to install into <PY_BINARY> because of PEP 668 (externally-managed-environment). The recommended fix is to install a user-owned Python via mise — it sidesteps the lockdown and pins per-project versions cleanly. How would you like to proceed?`
    - Header: `pip-failed`
    - Options:
      - `Install Python via mise (recommended)` — description: `brew install mise && mise use -g python@latest, then re-probe. The re-probe should pick up the mise-managed interpreter, and pip --user works against it.`
      - `Override with --break-system-packages` — description: `Run: <PY_BINARY> -m pip install --user --break-system-packages <missing pkgs>. Escape hatch — packages can get stranded if brew upgrades the underlying Python.`
      - `Abort init` — description: `Stop here; resolve manually and re-run /wise-init.`
    - multiSelect: false

  On `Install Python via mise`: print the two-line install block
  and pause for the user to run them in their terminal, then jump
  to the `Done — re-probe` follow-up below (same shape as the
  `PY_STATUS=missing` path). On `Override`: run the
  `--break-system-packages` invocation and re-probe via §2a.
  On `Abort`: stop with a one-line summary.

  If after the chosen path any module is still missing, surface
  the pip error and `Abort init`.

- **`PY_STATUS=missing`:** `AskUserQuestion`:
  - Question: `Python 3 isn't installed. How would you like to install it? mise is strongly recommended — it gives you a user-owned Python that pip --user can write into (no PEP 668 lockdown) and lets you pin versions per project.`
  - Header: `Install Python`
  - Options:
    - `mise (strongly recommended)` — description: `brew install mise && mise use -g python@latest. Sidesteps the PEP 668 lockdown that bites Homebrew system Python on macOS.`
    - `brew (system Python)` — description: `brew install python@3. Works, but pip install --user will hit "externally-managed-environment" — you'll have to use --break-system-packages or a venv for every install.`
    - `Manual` — description: `I'll install Python myself — hold the wizard until I'm done.`
  - multiSelect: false

  Whichever the user picks, the wizard's job is just to wait for
  them to run the commands in their own terminal. Claude doesn't
  run the installer — we can't `brew install` a new binary from
  inside a skill. After the user picks, print:

  ```
  Run the commands above in your terminal, then reply "done" (or
  use the "Done — re-probe" option below).
  ```

  Then a follow-up `AskUserQuestion`:
  - Options: `Done — re-probe` / `Abort init`.

  On `Done — re-probe`: re-run §2a. Up to 2 retries total; on the
  third miss offer `Abort init` or continue anyway.

**2c. Record.**

Once §2b terminates with Python usable (or the user explicitly
chose to proceed without it), hold a Python result object in
Claude-side state:

```json
{
  "status": "ok" | "missing",
  "binary": "<PY_BINARY or empty>",
  "version": "<PY_VERSION or empty>",
  "modules": {
    "yaml": "ok" | "missing",
    "ulid": "ok" | "missing",
    "typing_extensions": "ok" | "missing"
  }
}
```

### 3. Engine runtime: bun (preferred) or Node ≥24

**3a. Probe bun first.**

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/init.sh" probe-bun
```

Bare keys `STATUS`, `BINARY`, `VERSION` → `BUN_STATUS`, `BUN_BINARY`,
`BUN_VERSION`.

- **`BUN_STATUS=ok`:** print `bun <ver> ✓ at <binary>` and skip to §3c.
- **`BUN_STATUS=missing`:** fall through to §3b; bun is optional when
  Node ≥24 is present.

**3b. Probe Node.** Same pattern as §2, with `init.sh probe-node`. Bare
keys `STATUS`, `BINARY`, `VERSION`, `MAJOR` → `NODE_STATUS`,
`NODE_BINARY`, `NODE_VERSION`, `NODE_MAJOR`.

- **`NODE_STATUS=ok`:** print `Node <ver> ✓ at <binary>` and move on.
- **`NODE_STATUS=too-old` or `missing` (and no bun):** `AskUserQuestion`:
  - Question: `wise's workflow engine needs bun or Node 24+. Detected <ver or nothing>. How would you like to install a runtime?`
  - Options:
    - `bun (recommended)` — description: `brew install oven-sh/bun/bun`
    - `mise` — description: `mise use -g node@24`
    - `brew` — description: `brew install node@24 && brew link --overwrite --force node@24`
    - `Manual` — description: `I'll install it myself — hold the wizard.`
  Same `Done — re-probe` loop as §2b.

**3c. Probe the claude CLI login.** The engine runs workflow steps as
`claude -p` children under the user's subscription login. A desktop-app
session does not log the terminal CLI in, so this is a common gap.

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/init.sh" probe-claude-auth
```

Bare keys `STATUS`, `BINARY`, `METHOD` → `CLAUDE_AUTH_STATUS`,
`CLAUDE_AUTH_BINARY`, `CLAUDE_AUTH_METHOD`.

- **`CLAUDE_AUTH_STATUS=ok`:** print `claude login ✓ (<method>)`.
- **`CLAUDE_AUTH_STATUS=logged-out`:** tell the user to run
  `claude auth login` in a terminal (not inside this session), wait for
  `Done — re-probe`, re-run the probe.
- **`CLAUDE_AUTH_STATUS=missing`:** the `claude` binary is not on PATH;
  print the install hint from https://code.claude.com/docs and stop the
  wizard at this step (workflows cannot run without it).

Record:

```json
{
  "runtime": {"kind": "bun" | "node", "binary": "...", "version": "..."} | null,
  "claude_auth": {"status": "ok" | "logged-out" | "missing", "method": "..."}
}
```

**3d. Engine self-check.** Skip when §3 found no runtime. A plugin
install copies the engine without its dependencies; the first engine
call installs them (one `installing runtime dependencies` line on
stderr, then the answer).

```bash
bash "${CLAUDE_PLUGIN_ROOT}/engine/engine.sh" version
```

- Prints `wise-engine <version> (<bun|node> <ver>)`: print it and go on.
- Exit 69 or an install error: print the stderr verbatim. Usual causes:
  no network for the dependency fetch, or neither bun nor npm on PATH.
  Record `engine.status: failed` and continue with §4 (the wizard
  finishes; workflows will not run until this passes).

Next, the daemon. A daemon started before a plugin update keeps
serving the old code until a client replaces it, and a desktop session
holds its socket open, so check the build here:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/engine/engine.sh" daemon status
```

- `engined not running`: nothing to do; the first tool call starts it.
- `engined running: pid <n>, v<build>, <socket>`: print it and go on.
- The line ends with `(version mismatch)`: the daemon runs another
  build than the engine on disk. Run
  `bash "${CLAUDE_PLUGIN_ROOT}/engine/engine.sh" daemon stop`, print
  `stale daemon (v<old>) stopped; the next call starts <engine version>`,
  and re-run `daemon status` to confirm `not running`. `stop` waits
  for active runs; if it reports runs still active, say so and leave
  the daemon alone (a run in flight keeps its build).

Then, in the same message, call the `wise_status` MCP tool with no
arguments.

- Result (a run list, possibly empty): MCP `ok`.
- Tool not available in this session: MCP `restart-needed`. Print
  `The wise-engine MCP server loads at session start; open a new
  session after installing the plugin, then re-run /wise-init.`
- `DAEMON_UNAVAILABLE`: MCP `failed`; print the error's message.

**3e. Harness CLIs.** The engine can also dispatch steps to `codex`,
`grok` and `gemini`; each is optional and a workflow that names one
fails at pre-flight with `AUTH_REQUIRED` and the login command when it
is missing.

```bash
bash "${CLAUDE_PLUGIN_ROOT}/engine/engine.sh" auth
```

One `HARNESS=<name> INSTALLED=yes|no LOGIN=ok|missing LOGIN_CMD=<cmd>`
line per harness. Print one row each; for `LOGIN=missing` on an
installed harness, show `LOGIN_CMD` as the thing to run in a terminal
and never run it yourself. The `claude` row must be `LOGIN=ok` here
(same fact as §3c, probed the engine's way); if it is not, the exit
code is 1: repeat the §3c guidance.

Record:

```json
{
  "engine": {"version": "...", "status": "ok" | "failed", "mcp": "ok" | "restart-needed" | "failed",
             "daemon": "not-running" | "current" | "replaced" | "stale-busy"},
  "harnesses": {
    "codex":  {"installed": true|false, "login": "ok" | "missing", "login_cmd": "..."},
    "grok":   {...},
    "gemini": {...}
  }
}
```

### 4. gh CLI + auth

**4a. Probe.**

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/init.sh" probe-gh
```

Bare keys `STATUS`, `BINARY`, `VERSION`, `AUTHENTICATED`, `LOGIN`
→ `GH_STATUS`, `GH_BINARY`, `GH_VERSION`, `GH_AUTHENTICATED`,
`GH_LOGIN`.

**4b. Binary check.**

- **`GH_STATUS=missing`:** offer install options:
  - `brew (recommended)` — description: `brew install gh`
  - `mise` — description: `mise use -g gh@latest`
  - `Manual`
  Same `Done — re-probe` loop. On success, continue to §4c.

**4c. Auth check.**

Once the gh binary is present, check `GH_AUTHENTICATED`:

- **`GH_AUTHENTICATED=true`:** print
  `gh <ver> ✓ (authenticated as <GH_LOGIN>)` and move on.
- **`GH_AUTHENTICATED=false`:** `AskUserQuestion`:
  - Question: `gh is installed but not authenticated. Run "gh auth login" in your terminal to complete the browser flow.`
  - Header: `gh auth`
  - Options:
    - `Done — re-probe` — user ran `gh auth login`; re-probe and check.
    - `Skip auth for now` — description: `Continue without authentication. wise-pr-* skills and any workflow step that hits the GitHub API will fail until you run gh auth login.`
  Re-probe after `Done`. After 2 failed attempts, offer to skip.

Record:

```json
{
  "status": "ok" | "missing",
  "binary": "...",
  "version": "...",
  "authenticated": true | false,
  "login": "<handle or empty>"
}
```

**4d. git over ssh from the engine's child environment.** Engine
children (harness CLIs, bash steps, the unit phases' own `git` and
`gh` calls) start from a clean environment; git reaches an ssh remote
only through the agent socket it inherits. A key that is not loaded in
the agent fails every `git@github.com` call with
`Permission denied (publickey)`, and a workflow dies at its first
`ls-remote`.

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/init.sh" probe-git-ssh
```

Bare keys `STATUS`, `AGENT`, `HOST`, `DETAIL` → `GIT_SSH_STATUS`,
`GIT_SSH_AGENT`, `GIT_SSH_HOST`, `GIT_SSH_DETAIL`.

- **`GIT_SSH_STATUS=ok`:** print `git over ssh ✓ (<DETAIL>)`.
- **`GIT_SSH_STATUS=denied`:** `AskUserQuestion`:
  - Question: `git over ssh is denied from the engine's environment (<DETAIL>). Load your key into the agent in a terminal: ssh-add --apple-use-keychain ~/.ssh/<key> (macOS) or ssh-add ~/.ssh/<key>. If AGENT=unset, start the app from a login that exports SSH_AUTH_SOCK.`
  - Header: `git ssh`
  - Options: `Done — re-probe`; `Skip for now` — description:
    `Continue. Any workflow that pushes or fetches over ssh fails at its first git call until this passes; https remotes with gh credentials are unaffected.`
- **`GIT_SSH_STATUS=unreachable`:** print the detail; a network
  problem, not a setup gap. Record and move on.
- **`GIT_SSH_STATUS=missing-ssh`:** print `ssh not on PATH`; record.
- **`GIT_SSH_STATUS=unknown`:** print the detail verbatim; record.

Record:

```json
{
  "status": "ok" | "denied" | "unreachable" | "missing-ssh" | "unknown",
  "agent": "set" | "unset",
  "host": "github.com",
  "detail": "..."
}
```

**4e. MCP servers a workflow child inherits.** Engine children run
`claude -p` with the CLI's own MCP servers (user, project, plugin and
claude.ai connectors) on top of the engine's channel server. That
inventory is the CLI's, not this app session's: a connector authorized
only in the desktop app, or a server the CLI lists as "Needs
authentication", is unreachable from every child, and a workflow that
needs a tracker fails at its first fetch.

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/init.sh" probe-mcp
```

Bare keys `STATUS`, `COUNT`, `CONNECTED`, `NEEDS_AUTH`, `FAILED`,
`DETAIL` → `MCP_STATUS`, `MCP_COUNT`, `MCP_CONNECTED`,
`MCP_NEEDS_AUTH`, `MCP_FAILED`, `MCP_DETAIL` (name lists are
`;`-separated).

- **`MCP_STATUS=ok`:** print `MCP servers ✓ (<COUNT> connected)`.
- **`MCP_STATUS=partial`:** print one line per name in `MCP_NEEDS_AUTH`
  and `MCP_FAILED`, then `AskUserQuestion`:
  - Question: `These MCP servers are not usable from workflow children: <names>. Authorize each one in a terminal: run claude mcp (or /mcp inside an interactive claude session), pick the server, complete its login. A server the desktop app has but the CLI does not needs to be added to the CLI with claude mcp add.`
  - Header: `MCP servers`
  - Options: `Done — re-probe`; `Skip for now` — description:
    `Continue. Children can still use the connected servers, CLIs and public URLs; a workflow that needs one of the listed servers fails at its first fetch unless the conductor fetches the ticket itself (it does for tickets).`
- **`MCP_STATUS=none`:** print `no MCP servers configured for the CLI`;
  record and move on (tracker access then relies on CLIs and the
  conductor's own fetch).
- **`MCP_STATUS=missing-claude`:** already reported by §3c; record.
- **`MCP_STATUS=unknown`:** print the detail verbatim; record.

Record:

```json
{
  "status": "ok" | "partial" | "none" | "missing-claude" | "unknown",
  "count": 9,
  "connected": ["..."],
  "needs_auth": ["..."],
  "failed": ["..."],
  "detail": "..."
}
```

### 5. markitdown (file → markdown extraction)

The [`markitdown`](https://github.com/microsoft/markitdown) CLI powers
the `wise-markitdown` reference skill — text extraction from PDF /
DOCX / XLSX / PPTX / images / audio / EPUB / ZIP / … to markdown.
Optional in the sense that no workflow engine step needs it, but the
extraction skill degrades to one-shot `uvx` runs without it, so the
wizard installs it properly here.

**5a. Probe.**

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/init.sh" probe-markitdown
```

Bare keys `STATUS`, `BINARY`, `VERSION`, `UV` → `MD_STATUS`,
`MD_BINARY`, `MD_VERSION`, `MD_UV`. `UV` reports whether the `uv`
installer is reachable (directly or via mise) — it is emitted even
when markitdown itself is already installed.

**5b. Handle the result.**

- **`MD_STATUS=ok`:** print `markitdown <ver> ✓ at <binary>` and move
  to §6. (The probe can't tell a bare `markitdown` install from a
  `markitdown[all]` one — if conversions later fail with
  `MissingDependencyException`, the fix is
  `uv tool install --force 'markitdown[all]'`; the `wise-markitdown`
  skill documents this.)

- **`MD_STATUS=missing` and `MD_UV=ok`:** `AskUserQuestion`:
  - Question: `markitdown (file → markdown text extraction: PDF, DOCX, XLSX, PPTX, images, audio, …) isn't installed. Install it now via uv?`
  - Header: `markitdown`
  - Options:
    - `Install (recommended)` — description: `Run: uv tool install 'markitdown[all]' — a user-space tool install, no sudo, no system Python touched.`
    - `Skip` — description: `Continue without it. The wise-markitdown skill will fall back to one-shot uvx runs (re-downloads on a cold cache).`
  - multiSelect: false

  On `Install`: run

  ```bash
  uv tool install 'markitdown[all]'
  ```

  (when `uv` is only reachable through mise, run
  `mise exec uv -- uv tool install 'markitdown[all]'` instead).
  This IS run by the wizard — like the pip-module installs in §2b,
  it's a user-space install with no sudo and no system packages.
  Unpinned on purpose: wise tracks the latest release for every CLI
  dep (gh, node, the pip modules) — markitdown follows the same
  policy.
  Re-probe via §5a; on success print the ✓ line. If the install
  fails, surface the error and record `missing` — never retry blind.

- **`MD_STATUS=missing` and `MD_UV=missing`:** `uv` itself is absent,
  so there's nothing for the wizard to run. `AskUserQuestion`:
  - Question: `markitdown needs the uv installer, which isn't installed either. Install uv first?`
  - Header: `uv missing`
  - Options:
    - `Install uv via mise (recommended)` — description: `Run in your terminal: brew install mise && mise use -g uv@latest — then I re-probe and install markitdown.`
    - `Skip` — description: `Continue without markitdown. Re-run /wise-init after installing uv.`
  - multiSelect: false

  Like the §2b/§3 system installers, installing `uv`/`mise` is the
  user's move — print the command, pause with the same
  `Done — re-probe` / `Abort` follow-up, then resume the
  `MD_UV=ok` branch above. On `Skip`, record `missing` and move on.

**5c. Record.**

```json
{"status": "ok" | "missing", "binary": "...", "version": "..."}
```

### 6. Write the registry

Compose a JSON object from the four result blobs above plus the
plugin version:

```json
{
  "version": 1,
  "plugin_version": "<contents of plugin.json's version field>",
  "completed_at": "<utc ISO8601, see below>",
  "deps": {
    "python":      { ... from §2c ... },
    "node":        { ... runtime from §3a/§3b ... },
    "claude_auth": { ... from §3c ... },
    "engine":      { ... from §3d ... },
    "harnesses":   { ... from §3e ... },
    "gh":          { ... from §4 ... },
    "git_ssh":     { ... from §4d ... },
    "mcp":         { ... from §4e ... },
    "markitdown":  { ... from §5c ... }
  }
}
```

Read the plugin version:

```bash
python3 -c 'import json; print(json.load(open("'"${CLAUDE_PLUGIN_ROOT}"'/.claude-plugin/plugin.json"))["version"])'
```

Compute the timestamp:

```bash
date -u +%Y-%m-%dT%H:%M:%SZ
```

Then write the registry:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/init-registry.py" write '<the JSON blob>'
```

The script prints the registry path on stdout — capture that for
the summary.

### 7. Summary

Print a one-block report:

```
/wise-init complete.

  Python 3.12.5       ✓
  bun 1.4.1           ✓
  claude login        ✓ (claude.ai)
  wise-engine 5.0.0   ✓  MCP ✓  daemon current
  codex               ✓ logged in
  grok                ✓ logged in
  gemini              ⚠ installed, not logged in (optional)
  gh 2.54.0 (auth: your-username) ✓
  git over ssh        ✓ (github.com, agent set)
  MCP servers         ⚠ 7 connected, needs auth: plugin:linear:linear
  markitdown 0.1.3    ✓

Registry cached at:
  ${CLAUDE_PLUGIN_ROOT}/.wise-init-registry.yaml

Workflow runs (/wise-workflow-run, …) will use this cache instead
of re-probing. Re-run /wise-init any time your environment changes
or after `/plugin install wise@…` (which wipes the cache by design).
```

Adjust the row's checkmark to `⚠` and the label suffix when a dep
ended up `missing` or `authenticated: false`. Optional harness rows
are `⚠`, never `✗`: a missing codex, grok or gemini blocks nothing
until a workflow names it. `MCP restart-needed` is the one row that
ends with an instruction (open a new session); `daemon replaced` names
the old build that was stopped, `daemon stale-busy` says a run kept
it. A `git over ssh ⚠ denied` row repeats the ssh-add hint. Be honest — don't
claim success for something the user skipped.

## Guardrails

- **Never run a system installer for the user.** The wizard shows
  the commands, the user pastes them in their own terminal. We can't
  `brew install` / `mise use -g` from inside a skill (and wouldn't
  want to — it prompts for sudo in some environments and changes the
  user's `$PATH`). Our job is guidance + re-probe. The two sanctioned
  exceptions are user-space package installs into an ALREADY-present
  toolchain — `pip install --user` (§2b) and
  `uv tool install` (§5b) — no sudo, no PATH mutation, and only
  after an explicit AskUserQuestion confirm.
- **Never run `gh auth login` for the user.** It opens a browser
  and requires a device code; the user has to be the one driving.
  Pause with `Done — re-probe` and check `GH_AUTHENTICATED` after.
- **Never write anywhere but the registry path.** The registry
  lives at `${CLAUDE_PLUGIN_ROOT}/.wise-init-registry.yaml` and
  nowhere else. `init-registry.py write` enforces this.
- **Never block on a skipped dep.** Record the actual state
  (`status: missing` or `authenticated: false`) and move on. The
  workflow engine's fast-path check treats the registry as
  ground truth — a dep recorded as `missing` tells the engine to
  fall back to the live probe on use.
- **Never invoke another action skill from here.** `/wise-init` is a
  standalone wizard — not composed over `wise-workflow-run` /
  `wise-workflow-resume`.
