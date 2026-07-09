# init-check — dependency fast-path for workflow skills

The workflow skills (`run` / `resume` / `list` / `status`) all gate
their first real work on the init-registry fast-path, falling back to a
full dep probe when the registry is missing or stale. The protocol is
identical; only the caller's own data call(s) differ, plus whether the
caller drives the install loop (state-mutating skills) or just relays
and stops (read-only skills).

## The one-message fire

In a SINGLE assistant message with NO text between the tool uses, fire
together:

1. The init-check:
   ```bash
   python3 "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/pi}/scripts/init-registry.py" check 2>/dev/null || true
   ```
2. The caller's data call(s) — e.g. `workflows.py list-defs`,
   `list-resumable-runs`, `runs-root`, `list-runs <runs-root>`, or
   `dump-state <state>` (whatever the caller specifies).
3. Any `ToolSearch` the caller needs (e.g. `select:AskUserQuestion`
   for a picker).

Parse all results together in the next message.

**Why parallel is safe.** `workflows.py` and `init-registry.py` both
hard-fail identically when Python / PyYAML are absent (import error at
the top of the file), so a broken environment makes the data-call
output suspect — and the fallback below discards it. Worst case is one
wasted fork whose stderr was silenced.

## Interpreting the result

- **stdout `INIT:ok`** → registry good; use the data-call output
  directly and proceed.
- **Anything else** (`INIT:uninit` / `INIT:stale:*` /
  `INIT:dep-missing:*` / empty stdout when Python is missing) →
  discard the data-call output, nudge once, and run the full probe:

  ```bash
  echo "Tip: run /wise-init to cache dep probe results and speed up future runs." >&2
  bash "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/pi}/scripts/bootstrap-deps.sh"
  ```

  Parse the probe stdout:

  - `READY:<py-path>` → re-run the data call(s) and continue.
  - `BOOTSTRAP:need-python` (+ one or more `OPTION:` lines):
    - **State-mutating callers** (`wise-workflow-run`) drive an install
      loop — relay the `OPTION:` install commands via `AskUserQuestion`
      (`Install mise (recommended)` / `Install system Python 3` /
      `Abort`, each description mirroring the `OPTION:` text), tell the
      user to run them out of band, add a `Re-check` option, and re-run
      bootstrap until `READY` or `Abort`.
    - **Read-only callers** (`wise-workflow-list` / `-status`, and
      `-resume`'s picker) may simply relay the `OPTION:` lines and stop
      — there is nothing to proceed to without Python.
  - `BOOTSTRAP:pip-failed` → relay stderr and stop.

  Bootstrap auto-populates `.wise-init-registry.yaml` on a successful
  probe, so the next invocation hits the fast path.
