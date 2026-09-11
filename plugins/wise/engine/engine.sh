#!/usr/bin/env bash
# Resolve this install before entering its managed Python environment.
set -euo pipefail
python_bin="${WISE_PYTHON:-python3}"
if ! command -v "$python_bin" >/dev/null 2>&1; then
  echo "wise-engine: Python 3.11 or newer is required; set WISE_PYTHON to its executable" >&2
  exit 69
fi
exec "$python_bin" -c '
import pathlib, sys
sys.dont_write_bytecode = True
if sys.version_info < (3, 11):
    print("wise-engine: Python 3.11 or newer is required", file=sys.stderr)
    raise SystemExit(69)
sys.path.insert(0, str(pathlib.Path(sys.argv.pop(1)).resolve().parent))
from wise_engine.bootstrap import main
raise SystemExit(main(["--", *sys.argv[1:]]))
' "${BASH_SOURCE[0]}" "$@"
