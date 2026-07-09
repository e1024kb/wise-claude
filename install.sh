#!/usr/bin/env bash
# Universal installer for the wise plugin across harnesses.
#
# Pure copier / marketplace-command wrapper — it never transforms files
# (the per-harness ports under harnesses/<harness>/wise/ are the
# committed, hand-maintained source of truth). It copies the whole port
# pack (references/agents/workflows/scripts/skills) to a stable shared
# root — the path port skills resolve by default, overridable via
# WISE_PLUGIN_ROOT — and additionally copies the skills into the
# harness's skill discovery directory.
#
# Usage:
#   ./install.sh <harness> [--user|--project <dir>] [--force] [--uninstall]
#
#   <harness>   claude | codex | cursor | hermes | opencode
#   --user      (default) install for the current user
#   --project <dir>  install into a project's skills dir instead
#   --force     overwrite an existing differing install
#   --uninstall remove exactly what this script installs
#
# Bash 3.2 compatible (macOS default shell).
set -eu

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
WISE_HOME="${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}"
SHARED_ROOT_BASE="$WISE_HOME/harness"   # WISE_PLUGIN_ROOT lives under here

HARNESS=""
SCOPE="user"
PROJECT="."
FORCE=0
UNINSTALL=0

die() { echo "install.sh: $*" >&2; exit 1; }

# ---- arg parse -------------------------------------------------------------
[ $# -ge 1 ] || die "usage: ./install.sh <claude|codex|cursor|hermes|opencode> [--user|--project <dir>] [--force] [--uninstall]"
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
  claude|codex|cursor|hermes|opencode) ;;
  *) die "unknown harness '$HARNESS' (expected claude|codex|cursor|hermes|opencode)" ;;
esac

PACK="$REPO_ROOT/harnesses/$HARNESS/wise"
[ -d "$PACK" ] || die "port not found: $PACK"

plugin_version() {
  sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' \
    "$REPO_ROOT/harnesses/claude/wise/.claude-plugin/plugin.json" | head -1
}

# ---- skill discovery dir per harness/scope ---------------------------------
skills_target() {
  if [ "$SCOPE" = "project" ]; then
    # .agents/skills is the cross-harness standard project path.
    echo "$PROJECT/.agents/skills"
    return
  fi
  case "$HARNESS" in
    codex)    echo "$HOME/.agents/skills" ;;
    cursor)   echo "$HOME/.cursor/skills" ;;
    hermes)   echo "$HOME/.hermes/skills" ;;
    opencode) echo "$HOME/.config/opencode/skills" ;;
    claude)   echo "" ;;  # claude uses the plugin marketplace, not a copy
  esac
}

SHARED_ROOT="$SHARED_ROOT_BASE/$HARNESS"

# Lay the complete intact pack at the shared root. Port skills resolve
# this path by default (WISE_PLUGIN_ROOT only overrides it), so the pack
# must stay whole: skills/ included — cross-skill reads like
# skills/wise-commit/commit-routine.md resolve against it. commands/ is
# opencode-only (slash wrappers); the existence guard skips it elsewhere.
lay_shared_root() {
  mkdir -p "$SHARED_ROOT"
  # Reinstall invalidates the cached dependency probe (mirrors the Claude
  # port, where /plugin install wipes the plugin dir and the registry
  # with it).
  rm -f "$SHARED_ROOT/.wise-init-registry.yaml"
  for sub in references agents workflows scripts skills commands; do
    [ -d "$PACK/$sub" ] || continue
    if [ -e "$SHARED_ROOT/$sub" ] && [ "$FORCE" = "0" ]; then
      die "$SHARED_ROOT/$sub exists (use --force to overwrite)"
    fi
    rm -rf "$SHARED_ROOT/$sub"
    cp -R "$PACK/$sub" "$SHARED_ROOT/$sub"
  done
  plugin_version > "$SHARED_ROOT/.wise-version"
}

# ---- opencode extras: command wrappers + subagent role cards ----------------
# User-scope only — opencode reads both from ~/.config/opencode/, which
# this installer has no project-scoped analogue for. The in-pack agent
# filenames stay neutral (<role>.md); the copy adds the wise- prefix so
# the cards register as @wise-<role> without colliding with user agents.
OPENCODE_CONFIG="$HOME/.config/opencode"

install_opencode_extras() {
  mkdir -p "$OPENCODE_CONFIG/commands" "$OPENCODE_CONFIG/agents"
  for f in "$PACK"/commands/*.md; do
    dest="$OPENCODE_CONFIG/commands/$(basename "$f")"
    if [ -e "$dest" ] && [ "$FORCE" = "0" ]; then
      die "$dest exists (use --force to overwrite)"
    fi
    cp "$f" "$dest"
  done
  for f in "$PACK"/agents/*.md; do
    dest="$OPENCODE_CONFIG/agents/wise-$(basename "$f")"
    if [ -e "$dest" ] && [ "$FORCE" = "0" ]; then
      die "$dest exists (use --force to overwrite)"
    fi
    cp "$f" "$dest"
  done
}

remove_opencode_extras() {
  for f in "$PACK"/commands/*.md; do
    rm -f "$OPENCODE_CONFIG/commands/$(basename "$f")"
  done
  for f in "$PACK"/agents/*.md; do
    rm -f "$OPENCODE_CONFIG/agents/wise-$(basename "$f")"
  done
}

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
  if [ "$HARNESS" = "opencode" ] && [ "$SCOPE" = "user" ]; then
    remove_opencode_extras
    echo "Removed wise command wrappers and wise-<role> agents from $OPENCODE_CONFIG."
  fi
  exit 0
fi

# ---- codex: try the plugin marketplace first -------------------------------
if [ "$HARNESS" = "codex" ] && [ "$SCOPE" = "user" ] && command -v codex >/dev/null 2>&1; then
  if codex plugin marketplace add "$REPO_ROOT" >/dev/null 2>&1 \
     && codex plugin install wise >/dev/null 2>&1; then
    # The marketplace install lands in a versioned cache dir, so the pack
    # still gets laid at the stable shared root the skills default to.
    lay_shared_root
    echo "Installed the Codex plugin via 'codex plugin marketplace add'."
    echo "Shared assets → $SHARED_ROOT (skills resolve this automatically;"
    echo "export WISE_PLUGIN_ROOT only to override)."
    bash "$PACK/scripts/bootstrap-deps.sh" --probe || true
    echo "If anything above is missing, run the wise-init skill in your"
    echo "harness to finish dependency setup."
    exit 0
  fi
  echo "codex plugin install unavailable; falling back to a plain skills copy."
fi

# ---- copy install (codex fallback, cursor, hermes, opencode) ---------------
mkdir -p "$TARGET"

# 1) whole pack → shared root (the default WISE_PLUGIN_ROOT)
lay_shared_root

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

# 3) opencode only: slash-command wrappers + wise-<role> subagent cards
if [ "$HARNESS" = "opencode" ]; then
  if [ "$SCOPE" = "user" ]; then
    install_opencode_extras
    echo "Command wrappers → $OPENCODE_CONFIG/commands"
    echo "Subagent role cards → $OPENCODE_CONFIG/agents (as wise-<role>.md)"
  else
    echo "Note: --project install skips the opencode command wrappers and"
    echo "subagent cards — they are user-scope only (~/.config/opencode/)."
    echo "Run './install.sh opencode --user' to add them."
  fi
fi
echo
echo "Skills resolve $SHARED_ROOT automatically (it is their baked-in"
echo "default); export WISE_PLUGIN_ROOT only to override it."
echo
echo "Prerequisites: git, gh (authenticated) for PR skills; python3 + pyyaml +"
echo "python-ulid for the workflow engine. Persistent state: $WISE_HOME"
echo "(override with WISE_DATA_DIR)."
echo
bash "$PACK/scripts/bootstrap-deps.sh" --probe || true
echo
echo "If anything above is missing, run the wise-init skill in your harness"
echo "to finish dependency setup."
echo
echo "Note: this port excludes wise-supervise, the wise-insights-* skills,"
echo "and wise-skills-create/edit (Claude Code only). See"
echo "docs/compatibility.md for the full matrix."
