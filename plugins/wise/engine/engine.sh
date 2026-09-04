#!/usr/bin/env bash
# Run the wise engine CLI as TypeScript source: bun when installed, otherwise Node 24+.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v bun >/dev/null 2>&1; then
  exec bun "$here/src/cli.ts" "$@"
fi
if command -v node >/dev/null 2>&1; then
  major="$(node -p 'process.versions.node.split(".")[0]')"
  if [ "$major" -ge 24 ]; then
    exec node "$here/src/cli.ts" "$@"
  fi
  echo "wise-engine: node $major found, need bun or node >= 24" >&2
  exit 69
fi
echo "wise-engine: neither bun nor node found on PATH" >&2
exit 69
