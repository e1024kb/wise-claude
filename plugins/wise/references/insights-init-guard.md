# insights-init-guard

Insights commands require the init runtime check before enumeration, mining or drafting.
The SessionEnd ingest hook records sessions independently.

```bash
"${WISE_PYTHON:-python3}" "${CLAUDE_PLUGIN_ROOT}/scripts/init-registry.py" check 2>/dev/null || true
```

- `INIT:ok`: continue with the insights procedure.
- Any other result, including no output when Python is unavailable: stop and say:

  > This command needs setup first. Run **/wise-init**, then re-run
  > `/wise-insights-<mine|refine>`.

Do not bootstrap from this guard. Python 3.11+ and the managed environment are
required; optional connector skips do not block insights. The registry check does
not establish host MCP registration.
