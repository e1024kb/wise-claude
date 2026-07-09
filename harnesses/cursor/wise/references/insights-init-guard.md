# insights-init-guard — require /wise-init before any insights command

The `/wise-insights-*` commands are **gated on `/wise-init`**. A user who has not
run the setup wizard cannot run mine or refine — this guard is the very first
step of their procedure, before any enumeration, mining, or drafting.

(Unlike the workflow `init-check.md`, this guard does **not** offer to bootstrap
inline. `/wise-init` is the required entry point; insights stays off until it has
run. The SessionEnd ingest hook is the one exception — it keeps recording
sessions independently so the ledger is already populated once setup is done.)

## Fire (first thing, one bash call)

```bash
python3 "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/cursor}/scripts/init-registry.py" check 2>/dev/null || true
```

## Interpret stdout

- **`INIT:ok`** → setup is complete. Proceed with the rest of the procedure.
- **Anything else** — `INIT:uninit`, `INIT:stale:<dep>`, `INIT:dep-missing:<dep>`,
  or **empty** output (Python / PyYAML not yet installed) → **STOP immediately.**
  Print exactly, then end the turn:

  > This command needs setup first. Run **/wise-init**, then re-run
  > `/wise-insights-<mine|refine>`.

  Do NOT attempt to install anything, do NOT run `bootstrap-deps.sh`, and do NOT
  do any insights work. `/wise-init` is the required gate.
