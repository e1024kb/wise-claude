# insights-init-guard

Insights commands require the init runtime check before enumeration, mining or drafting.
The SessionEnd ingest hook records sessions independently.

```bash
"${WISE_PYTHON:-python3}" "${WISE_PLUGIN_ROOT}/scripts/init-registry.py" --host "$WISE_HOST" check 2>/dev/null || true
```

- `INIT:ok`: continue with the insights procedure.
- Any other result, including no output when Python is unavailable: stop and say:

  > This command needs setup first. Run **/wise-init**, then re-run
  > `/wise-insights-<mine|refine>`.

Do not bootstrap from this guard. Python 3.11+ and the managed environment are
required; optional connector skips do not block insights. Select WISE_HOST from the current conductor session and resolve WISE_PLUGIN_ROOT
from the loaded installation. The registry check validates registration files;
native MCP connectivity still requires a successful host tool call.
