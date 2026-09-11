#!/usr/bin/env bash
# The skill catalog uses the same managed Python packages as the workflow engine.

set -u
WISE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$WISE_SCRIPT_DIR/init.sh"
WISE_CATALOG_PY="$(find_python || true)"
if [[ -z "$WISE_CATALOG_PY" ]] || ! "$WISE_CATALOG_PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
  exec bash "$WISE_SCRIPT_DIR/bootstrap-deps.sh"
fi
exec "$WISE_CATALOG_PY" - "$WISE_SCRIPT_DIR/../engine" "$WISE_SCRIPT_DIR/engine.py" "$@" <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from wise_engine.bootstrap import main
raise SystemExit(main(["--script", sys.argv[2], "--", *sys.argv[3:]]))
PY
