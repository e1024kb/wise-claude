#!/usr/bin/env bash
# wise SessionEnd ingest hook.
#
# The ONE sanctioned hook in the wise plugin (see CONTRIBUTING.md §2 "Hooks"
# and plugins/wise/CLAUDE.md). Its job is deliberately tiny: read the
# SessionEnd payload from stdin, pull out the transcript path, and hand that
# single file to `insights.py ingest`, which appends a compact record to the
# local insights ledger. The heavy lifting (clustering, proposing, drafting)
# lives in the `/wise-insights-mine` skill, never here.
#
# HARD CONSTRAINTS — keep them true:
#   • No LLM, no network.
#   • No dependency bootstrap (bootstrap-deps.sh can prompt/install — forbidden
#     in a hook). insights.py is stdlib-only, so this works even before
#     `/wise-init` has run.
#   • Touches exactly ONE transcript (the one that just ended).
#   • ALWAYS exits 0 and never blocks session teardown — any failure no-ops.
#   • Bash 3.2 compatible (macOS default) — no mapfile/readarray.

# Never let an error escape: a failing SessionEnd hook must not disrupt exit.
set +e

ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# No Python → nothing we can do; no-op cleanly.
command -v python3 >/dev/null 2>&1 || exit 0

PAYLOAD="$(cat)"

# Extract fields with Python (do NOT assume jq is installed). Null-safe.
TRANSCRIPT="$(printf '%s' "$PAYLOAD" | python3 -c 'import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get("transcript_path") or "")' 2>/dev/null)"

SID="$(printf '%s' "$PAYLOAD" | python3 -c 'import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get("session_id") or "")' 2>/dev/null)"

# No transcript path → nothing to ingest.
[ -n "$TRANSCRIPT" ] || exit 0

if [ -n "$SID" ]; then
  python3 "$ROOT/scripts/insights.py" ingest "$TRANSCRIPT" --session-id "$SID" >/dev/null 2>&1
else
  python3 "$ROOT/scripts/insights.py" ingest "$TRANSCRIPT" >/dev/null 2>&1
fi

exit 0
