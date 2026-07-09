#!/usr/bin/env bash
# wise plugin — runtime dependency bootstrap
#
# Ensures every CLI / language-runtime dependency a wise skill may need
# is present and usable. Called as the first step of every skill (or
# via engine.sh) before Python-backed helpers run, and
# before MCP servers that need Node are exercised.
#
# Current checks, in order:
#
#   1. python3 on PATH, with yaml + ulid + typing_extensions modules
#      importable (needed by scripts/engine.py and scripts/workflows.py).
#   2. node >= 22 on PATH (needed by any npx-driven MCP server wise
#      may use).
#   3. gh CLI on PATH and authenticated (needed by the wise-pr-*
#      family of skills and the ticket-auto workflow for PR creation,
#      reviewer requests, and CI check watching).
#
# Each check fail-fasts with a distinct BOOTSTRAP:need-<dep> tag on
# stdout plus a block of OPTION: lines describing how the user can
# install the missing tool. Skills relay the block to the user via
# AskUserQuestion; once the tool is installed the skill re-runs this
# script, which re-probes and either succeeds or surfaces the next
# missing dep.
#
# Exit codes:
#   0 — environment ready. stdout: READY:<python-path> on one line,
#       READY-NODE:<node-path> on the next, READY-GH:<gh-path> on
#       the third. Callers that only care about Python can ignore
#       the second and third lines.
#   2 — one of the hard deps is missing. stdout: BOOTSTRAP:need-<dep>
#       followed by OPTION: + NOTE: lines.
#   3 — Python is present but module install failed. stdout:
#       BOOTSTRAP:pip-failed. stderr has the pip output.
#
# Usage:
#   bash scripts/bootstrap-deps.sh         # auto-install missing python libs
#   bash scripts/bootstrap-deps.sh --probe # check only; never mutate the system

set -u

MODE="install"
if [[ "${1:-}" == "--probe" ]]; then
  MODE="probe"
fi

NODE_REQUIRED_MAJOR=22

# ---- Python -----------------------------------------------------------------

find_python() {
  # Prefer a mise-managed python if present — mise is the recommended
  # install path, and its shim ensures per-project version pinning.
  if command -v mise >/dev/null 2>&1; then
    mise_py="$(mise which python3 2>/dev/null || true)"
    if [[ -n "$mise_py" && -x "$mise_py" ]]; then
      echo "$mise_py"
      return 0
    fi
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  return 1
}

PY="$(find_python || true)"

if [[ -z "$PY" ]]; then
  cat <<'EOF'
BOOTSTRAP:need-python
OPTION:mise:Install mise (recommended) — https://mise.jdx.dev
  brew install mise
  mise use -g python@latest
OPTION:system:Install Python 3 directly
  brew install python@3
NOTE:Python 3 is required by wise's helper scripts (engine.py and
NOTE:workflows.py). Re-run your wise command after installation completes.
EOF
  exit 2
fi

# ---- Python modules ---------------------------------------------------------

check_modules() {
  "$PY" - <<'PY' 2>/dev/null
import importlib, sys
# typing_extensions is a transitive dep of python-ulid on Python < 3.11;
# probe it explicitly so macOS system Python (3.9) works out of the box.
missing = []
for mod in ("yaml", "ulid", "typing_extensions"):
    try:
        importlib.import_module(mod)
    except ImportError:
        missing.append(mod)
if missing:
    print(" ".join(missing))
    sys.exit(1)
sys.exit(0)
PY
}

MISSING="$(check_modules)"

if [[ -n "$MISSING" ]]; then
  if [[ "$MODE" == "probe" ]]; then
    echo "BOOTSTRAP:missing-modules $MISSING"
    exit 3
  fi
  declare -a PKGS
  for mod in $MISSING; do
    case "$mod" in
      yaml)              PKGS+=("pyyaml") ;;
      ulid)              PKGS+=("python-ulid") ;;
      typing_extensions) PKGS+=("typing_extensions") ;;
      *)                 PKGS+=("$mod") ;;
    esac
  done
  echo "BOOTSTRAP:installing ${PKGS[*]}" >&2
  PIP_ERR="$(mktemp -t wise-pip-error.XXXXXX 2>/dev/null || true)"
  if [[ -n "$PIP_ERR" ]]; then
    trap 'rm -f "$PIP_ERR"' EXIT
    PIP_ERR_TARGET="$PIP_ERR"
  else
    PIP_ERR_TARGET="/dev/null"
  fi
  if ! "$PY" -m pip install --user --quiet "${PKGS[@]}" 2>"$PIP_ERR_TARGET"; then
    echo "BOOTSTRAP:pip-failed"
    if [[ -n "$PIP_ERR" ]] && grep -q "externally-managed-environment" "$PIP_ERR" 2>/dev/null; then
      cat <<EOF
NOTE:Detected pip lockdown (PEP 668 / externally-managed-environment) on
NOTE:$PY. This is the default for Homebrew Python on macOS and many
NOTE:distro packages on Linux — pip refuses to install into the system
NOTE:interpreter. The recommended fix is to install a user-owned Python
NOTE:via mise, which sidesteps the lockdown and gives you per-project
NOTE:version pinning at the same time.
OPTION:mise:Install Python via mise (recommended) — https://mise.jdx.dev
  brew install mise
  mise use -g python@latest
  # then re-run your wise command
OPTION:break-system:Override the lockdown (escape hatch, not recommended)
  $PY -m pip install --user --break-system-packages ${PKGS[*]}
NOTE:Re-running with --break-system-packages can leave packages stranded
NOTE:against a Python that brew may upgrade out from under you. Prefer
NOTE:the mise path unless you know why you're choosing the override.
EOF
    fi
    echo "bootstrap-deps: pip install failed — pip stderr follows:" >&2
    [[ -n "$PIP_ERR" ]] && cat "$PIP_ERR" >&2 || true
    exit 3
  fi
  if [[ -n "$(check_modules)" ]]; then
    echo "BOOTSTRAP:pip-failed"
    echo "bootstrap-deps: modules still missing after install" >&2
    exit 3
  fi
fi

# ---- Node ≥ 22 --------------------------------------------------------------

find_node() {
  if command -v mise >/dev/null 2>&1; then
    mise_node="$(mise which node 2>/dev/null || true)"
    if [[ -n "$mise_node" && -x "$mise_node" ]]; then
      echo "$mise_node"
      return 0
    fi
  fi
  if command -v node >/dev/null 2>&1; then
    command -v node
    return 0
  fi
  return 1
}

node_major_version() {
  # "$1 --version" prints something like "v22.5.0" — strip "v" and take
  # the first dot-separated chunk. Return empty string on any failure so
  # the caller treats it as outdated.
  local v
  v="$("$1" --version 2>/dev/null || true)"
  v="${v#v}"
  v="${v%%.*}"
  [[ "$v" =~ ^[0-9]+$ ]] && echo "$v" || echo ""
}

NODE="$(find_node || true)"
NODE_MAJOR=""
[[ -n "$NODE" ]] && NODE_MAJOR="$(node_major_version "$NODE")"

if [[ -z "$NODE" ]] || [[ -z "$NODE_MAJOR" ]] || (( NODE_MAJOR < NODE_REQUIRED_MAJOR )); then
  if [[ -n "$NODE" && -n "$NODE_MAJOR" ]]; then
    DETECTED_MSG="detected node v${NODE_MAJOR}.x at $NODE, but v${NODE_REQUIRED_MAJOR}+ is required"
  elif [[ -n "$NODE" ]]; then
    DETECTED_MSG="detected a node binary at $NODE whose version could not be parsed"
  else
    DETECTED_MSG="no node binary found on PATH"
  fi
  cat <<EOF
BOOTSTRAP:need-node
NOTE:$DETECTED_MSG
OPTION:mise:Install node ${NODE_REQUIRED_MAJOR} via mise (recommended) — https://mise.jdx.dev
  brew install mise
  mise use -g node@${NODE_REQUIRED_MAJOR}
OPTION:system:Install node ${NODE_REQUIRED_MAJOR} via brew
  brew install node@${NODE_REQUIRED_MAJOR}
  brew link --overwrite --force node@${NODE_REQUIRED_MAJOR}
NOTE:Node ${NODE_REQUIRED_MAJOR}+ is required by any npx-driven MCP server
NOTE:wise may use. Re-run your wise command after installation completes.
EOF
  exit 2
fi

# ---- gh CLI -----------------------------------------------------------------

find_gh() {
  if command -v mise >/dev/null 2>&1; then
    mise_gh="$(mise which gh 2>/dev/null || true)"
    if [[ -n "$mise_gh" && -x "$mise_gh" ]]; then
      echo "$mise_gh"
      return 0
    fi
  fi
  if command -v gh >/dev/null 2>&1; then
    command -v gh
    return 0
  fi
  return 1
}

GH="$(find_gh || true)"

if [[ -z "$GH" ]]; then
  cat <<'EOF'
BOOTSTRAP:need-gh
OPTION:brew:Install GitHub CLI (recommended) — https://cli.github.com
  brew install gh
  gh auth login
OPTION:mise:Install via mise
  mise use -g gh@latest
  gh auth login
NOTE:gh is required by the wise-pr-* family of skills and the ticket-auto
NOTE:workflow for PR creation, reviewer requests, and CI check watching.
NOTE:After installing, run `gh auth login` once to grant repo access,
NOTE:then re-run your wise command.
EOF
  exit 2
fi

# Probe auth — skip the check in `--probe` mode (where we must not
# mutate anything and a network-ish call could stall). In install
# mode, surface the unauth state so the user runs `gh auth login`
# before the first skill tries to hit the API.
if [[ "$MODE" != "probe" ]]; then
  if ! "$GH" auth status >/dev/null 2>&1; then
    cat <<EOF
BOOTSTRAP:need-gh-auth
OPTION:login:Authenticate the gh CLI
  $GH auth login
NOTE:gh was detected at $GH but is not authenticated. The wise-pr-*
NOTE:skills and the ticket-auto workflow need repo read+write scopes
NOTE:to create PRs, add reviewers, and read check runs. Run the login
NOTE:command above and re-run your wise command.
EOF
    exit 2
  fi
fi

# ---- Ready ------------------------------------------------------------------

# Auto-populate the init registry so subsequent `/wise-workflow-*`
# invocations hit the fast-path (`init-registry.py check` → INIT:ok)
# instead of re-running this full probe. Matches the schema `/wise-init`
# writes. Best-effort: a failed write is not fatal to the probe.
if [[ "$MODE" != "probe" ]]; then
  PY_VERSION="$("$PY" -c 'import sys; print(".".join(str(x) for x in sys.version_info[:3]))' 2>/dev/null || true)"
  NODE_VERSION_FULL="$("$NODE" --version 2>/dev/null || true)"
  NODE_VERSION_FULL="${NODE_VERSION_FULL#v}"
  GH_VERSION="$("$GH" --version 2>/dev/null | awk 'NR==1 {print $3}' || true)"
  GH_LOGIN="$("$GH" api user --jq .login 2>/dev/null || true)"
  PLUGIN_VERSION="$("$PY" -c "import json; print(json.load(open('${CLAUDE_PLUGIN_ROOT:-$(dirname "$0")/..}/.claude-plugin/plugin.json'))['version'])" 2>/dev/null || true)"
  COMPLETED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  REG_JSON="$("$PY" - "$PY" "$PY_VERSION" "$NODE" "$NODE_VERSION_FULL" "$GH" "$GH_VERSION" "$GH_LOGIN" "$PLUGIN_VERSION" "$COMPLETED_AT" <<'PY'
import json, sys
py_bin, py_ver, node_bin, node_ver, gh_bin, gh_ver, gh_login, plugin_ver, completed_at = sys.argv[1:10]
print(json.dumps({
    "version": 1,
    "plugin_version": plugin_ver,
    "completed_at": completed_at,
    "source": "bootstrap-deps.sh",
    "deps": {
        "python": {
            "status": "ok",
            "binary": py_bin,
            "version": py_ver,
            "modules": {"yaml": "ok", "ulid": "ok", "typing_extensions": "ok"},
        },
        "node": {"status": "ok", "binary": node_bin, "version": node_ver},
        "gh": {
            "status": "ok",
            "binary": gh_bin,
            "version": gh_ver,
            "authenticated": True,
            "login": gh_login,
        },
    },
}))
PY
)"
  if [[ -n "$REG_JSON" ]]; then
    "$PY" "${CLAUDE_PLUGIN_ROOT:-$(dirname "$0")/..}/scripts/init-registry.py" write "$REG_JSON" >/dev/null 2>&1 || true
  fi
fi

echo "READY:$PY"
echo "READY-NODE:$NODE"
echo "READY-GH:$GH"
exit 0
