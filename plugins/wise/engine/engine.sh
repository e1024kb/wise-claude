#!/usr/bin/env bash
# Run the wise engine CLI as TypeScript source: bun when installed, otherwise Node 24+.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# A plugin install copies the source tree without node_modules; fetch the three runtime deps once.
if [ ! -d "$here/node_modules/@modelcontextprotocol/sdk" ]; then
  echo "wise-engine: installing runtime dependencies in $here" >&2
  if command -v bun >/dev/null 2>&1; then
    install_cmd=(bun install --production)
  elif command -v npm >/dev/null 2>&1; then
    install_cmd=(npm install --omit=dev --no-audit --no-fund --loglevel=error)
  else
    echo "wise-engine: need bun or npm to install dependencies" >&2
    exit 69
  fi
  # Not --silent: the MCP host only shows this stderr in its log, so the
  # install's own output is the only trace of why a first start failed.
  rc=0
  (cd "$here" && "${install_cmd[@]}" >&2) || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "wise-engine: dependency install failed (exit $rc): \`${install_cmd[*]}\` in $here" >&2
    echo "wise-engine: fix: cd \"$here\" && ${install_cmd[*]}  # then start a new session or run /wise-init" >&2
    exit 69
  fi
fi
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
