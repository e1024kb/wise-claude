#!/usr/bin/env bash
# Universal installer for the wise plugin across harnesses.
#
# Pure copier / marketplace-command wrapper — it never transforms files
# (the per-harness ports under harnesses/<harness>/wise/ are the
# committed, hand-maintained source of truth). It copies a port's shared
# assets (references/agents/workflows/scripts) to a stable
# WISE_PLUGIN_ROOT location and its skills into the harness's skill
# discovery directory.
#
# Usage:
#   ./install.sh <harness> [--user|--project <dir>] [--force] [--uninstall]
#
#   <harness>   claude | codex | cursor | hermes
#   --user      (default) install for the current user
#   --project <dir>  install into a project's skills dir instead
#   --force     overwrite an existing differing install
#   --uninstall remove exactly what this script installs
#
# Bash 3.2 compatible (macOS default shell).
set -eu

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
WISE_HOME="${WISE_DATA_DIR:-$HOME/.local/share/wise}"
SHARED_ROOT_BASE="$WISE_HOME/harness"   # WISE_PLUGIN_ROOT lives under here

HARNESS=""
SCOPE="user"
PROJECT="."
FORCE=0
UNINSTALL=0

die() { echo "install.sh: $*" >&2; exit 1; }

# ---- arg parse -------------------------------------------------------------
[ $# -ge 1 ] || die "usage: ./install.sh <claude|codex|cursor|hermes> [--user|--project <dir>] [--force] [--uninstall]"
HARNESS="$1"; shift
while [ $# -gt 0 ]; do
  case "$1" in
    --user) SCOPE="user" ;;
    --project) SCOPE="project"; shift; [ $# -ge 1 ] || die "--project needs a <dir>"; PROJECT="$1" ;;
    --force) FORCE=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

case "$HARNESS" in
  claude|codex|cursor|hermes) ;;
  *) die "unknown harness '$HARNESS' (expected claude|codex|cursor|hermes)" ;;
esac

PACK="$REPO_ROOT/harnesses/$HARNESS/wise"
[ -d "$PACK" ] || die "port not found: $PACK"

# ---- skill discovery dir per harness/scope ---------------------------------
skills_target() {
  if [ "$SCOPE" = "project" ]; then
    # .agents/skills is the cross-harness standard project path.
    echo "$PROJECT/.agents/skills"
    return
  fi
  case "$HARNESS" in
    codex)  echo "$HOME/.agents/skills" ;;
    cursor) echo "$HOME/.cursor/skills" ;;
    hermes) echo "$HOME/.hermes/skills" ;;
    claude) echo "" ;;  # claude uses the plugin marketplace, not a copy
  esac
}

SHARED_ROOT="$SHARED_ROOT_BASE/$HARNESS"

# ---- claude: marketplace flow ----------------------------------------------
if [ "$HARNESS" = "claude" ]; then
  if [ "$UNINSTALL" = "1" ]; then
    echo "To uninstall the Claude plugin, run in Claude Code:"
    echo "  /plugin uninstall wise"
    echo "  /plugin marketplace remove wise-claude"
    exit 0
  fi
  if command -v claude >/dev/null 2>&1; then
    claude plugin marketplace add "$REPO_ROOT" || true
    claude plugin install wise@wise-claude || true
    echo "Installed the Claude Code plugin from $REPO_ROOT."
  else
    echo "Claude Code CLI not found. Run these in Claude Code:"
    echo "  /plugin marketplace add e1024kb/wise-claude"
    echo "  /plugin install wise@wise-claude"
  fi
  exit 0
fi

TARGET="$(skills_target)"

# ---- uninstall -------------------------------------------------------------
if [ "$UNINSTALL" = "1" ]; then
  for d in "$PACK"/skills/*/; do
    name="$(basename "$d")"
    rm -rf "$TARGET/$name"
  done
  rm -rf "$SHARED_ROOT"
  echo "Removed wise skills from $TARGET and shared root $SHARED_ROOT."
  exit 0
fi

# ---- codex: try the plugin marketplace first -------------------------------
if [ "$HARNESS" = "codex" ] && [ "$SCOPE" = "user" ] && command -v codex >/dev/null 2>&1; then
  if codex plugin marketplace add "$REPO_ROOT" >/dev/null 2>&1 \
     && codex plugin install wise >/dev/null 2>&1; then
    echo "Installed the Codex plugin via 'codex plugin marketplace add'."
    echo "If a step can't find shared files, export:"
    echo "  export WISE_PLUGIN_ROOT=<the installed plugin dir>"
    exit 0
  fi
  echo "codex plugin install unavailable; falling back to a plain skills copy."
fi

# ---- copy install (codex fallback, cursor, hermes) -------------------------
mkdir -p "$TARGET" "$SHARED_ROOT"

# 1) shared assets → WISE_PLUGIN_ROOT
for sub in references agents workflows scripts; do
  [ -d "$PACK/$sub" ] || continue
  if [ -e "$SHARED_ROOT/$sub" ] && [ "$FORCE" = "0" ]; then
    die "$SHARED_ROOT/$sub exists (use --force to overwrite)"
  fi
  rm -rf "$SHARED_ROOT/$sub"
  cp -R "$PACK/$sub" "$SHARED_ROOT/$sub"
done

# 2) skills → discovery dir (each as a top-level <skill>/ dir)
for d in "$PACK"/skills/*/; do
  name="$(basename "$d")"
  if [ -e "$TARGET/$name" ] && [ "$FORCE" = "0" ]; then
    die "$TARGET/$name exists (use --force to overwrite)"
  fi
  rm -rf "$TARGET/$name"
  cp -R "$d" "$TARGET/$name"
done

echo "Installed $(ls "$PACK"/skills | wc -l | tr -d ' ') wise skills → $TARGET"
echo "Shared assets → $SHARED_ROOT"
echo
echo "Add this to your shell profile so skills/workflows resolve shared files:"
echo "  export WISE_PLUGIN_ROOT=\"$SHARED_ROOT\""
echo
echo "Prerequisites: git, gh (authenticated) for PR skills; python3 + pyyaml +"
echo "python-ulid for the workflow engine. Persistent state: $WISE_HOME"
echo "(override with WISE_DATA_DIR)."
echo
echo "Note: this port excludes wise-supervise, the wise-insights-* skills,"
echo "wise-skills-create/edit, and wise-init (Claude Code only). See"
echo "docs/compatibility.md for the full matrix."
