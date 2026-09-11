#!/usr/bin/env bash
# Install locked engine packages in the managed environment; --probe never installs.

set -u
WISE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$WISE_SCRIPT_DIR/init.sh"
WISE_BOOTSTRAP_PY="$(find_python || true)"

if [[ -z "$WISE_BOOTSTRAP_PY" ]] || ! "$WISE_BOOTSTRAP_PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
  cat <<'EOF'
BOOTSTRAP:need-python
OPTION:mise:Install Python 3.11 or newer with mise
  mise use -g python@3.12
OPTION:system:Install Python 3.11 or newer with your package manager
  brew install python@3.12
NOTE:Set WISE_PYTHON to a Python 3.11+ executable when python3 selects an older version.
NOTE:Engine packages install in wise's managed environment, without modifying system packages.
EOF
  exit 2
fi

WISE_BOOTSTRAP_FLAG="--prepare"
if [[ "${1:-}" == "--probe" ]]; then
  WISE_BOOTSTRAP_FLAG="--probe"
elif [[ $# -gt 0 ]]; then
  echo "usage: bootstrap-deps.sh [--probe]" >&2
  exit 2
fi

if ! WISE_MANAGED_PY="$("$WISE_BOOTSTRAP_PY" - "$WISE_SCRIPT_DIR/../engine" "$WISE_BOOTSTRAP_FLAG" <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from wise_engine.bootstrap import main
raise SystemExit(main(sys.argv[2:]))
PY
)"; then
  if [[ "$WISE_BOOTSTRAP_FLAG" == "--probe" ]]; then
    echo "BOOTSTRAP:missing-engine"
  else
    echo "BOOTSTRAP:install-failed"
  fi
  exit 3
fi

if [[ "$WISE_BOOTSTRAP_FLAG" != "--probe" && -n "${WISE_HOST:-}" ]]; then
  WISE_ENGINE_BASE_PYTHON="$WISE_BOOTSTRAP_PY" "$WISE_MANAGED_PY" "$WISE_SCRIPT_DIR/init-registry.py" refresh-runtime >/dev/null || {
    echo "bootstrap-deps: runtime ready; registry refresh failed" >&2
  }
fi
printf 'READY:%s\n' "$WISE_MANAGED_PY"
