#!/usr/bin/env bash
# engine.sh — wise plugin Python bootstrap.
#
# Thin wrapper that ensures Python 3 is available and delegates to
# scripts/engine.py. The only supported subcommand is `list-skills`,
# which walks the plugin's skills/ dir and emits a JSON catalog the
# `/wise` natural-language helper consumes. Kept as a bash entry point
# because the `/wise` SKILL.md grants `Bash(${CLAUDE_PLUGIN_ROOT}/scripts/engine.sh:*)`
# as a narrow permission — the shim is the stable public surface; the
# Python file is an implementation detail.
#
# If Python is missing, bootstrap-deps.sh emits the BOOTSTRAP: protocol
# tags (need-python with mise/brew install options, or pip-failed) that
# the caller already knows how to parse.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  # Hand off to bootstrap-deps.sh so the caller gets the standard
  # need-python options. bootstrap-deps.sh exits non-zero here.
  exec bash "$SCRIPT_DIR/bootstrap-deps.sh"
fi

exec python3 "$SCRIPT_DIR/engine.py" "$@"
