#!/usr/bin/env bash
# Install smoke test for the copy-install harness ports.
#
# Asserts the contract the ports' baked default path relies on: a plain
# `./install.sh <harness>` with NO env vars yields (a) the complete
# intact pack — references/agents/workflows/scripts/skills — at
# <data-root>/harness/<harness>, (b) the skills in the harness discovery
# dir, (c) a fresh .wise-version and no stale init registry, and (d) an
# engine that answers via the defaulted expansion without any export.
#
# Usage: bash scripts/install_smoke.sh [harness ...]   (default: cursor hermes)
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HARNESSES="${*:-cursor hermes}"

fail() { echo "install-smoke: $*" >&2; exit 1; }

for h in $HARNESSES; do
  H="$(mktemp -d)" W="$(mktemp -d)"
  HOME="$H" WISE_DATA_DIR="$W" "$REPO_ROOT/install.sh" "$h" --user > /dev/null
  root="$W/harness/$h"
  for sub in references agents workflows scripts skills; do
    [ -d "$root/$sub" ] || fail "$h: missing $root/$sub"
  done
  [ -f "$root/skills/wise-workflow-run/SKILL.md" ] || fail "$h: shared root lacks skills"
  [ -f "$root/scripts/workflows.py" ] || fail "$h: shared root lacks engine"
  [ -f "$root/.wise-version" ] || fail "$h: missing .wise-version"
  [ ! -e "$root/.wise-init-registry.yaml" ] || fail "$h: stale init registry survived install"
  case "$h" in
    codex)  skills_dir="$H/.agents/skills" ;;
    cursor) skills_dir="$H/.cursor/skills" ;;
    hermes) skills_dir="$H/.hermes/skills" ;;
    *) fail "unknown harness '$h'" ;;
  esac
  [ -f "$skills_dir/wise-commit/SKILL.md" ] || fail "$h: discovery dir lacks skills"
  # The baked default resolves the engine with no export.
  env -u WISE_PLUGIN_ROOT WISE_DATA_DIR="$W" bash -c \
    'python3 "${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/'"$h"'}/scripts/workflows.py" list-defs > /dev/null' \
    || fail "$h: engine did not resolve via the baked default"
  rm -rf "$H" "$W"
  echo "install-smoke: $h OK"
done
