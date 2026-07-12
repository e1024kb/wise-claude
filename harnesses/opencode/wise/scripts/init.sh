#!/usr/bin/env bash
# wise plugin — per-dep probe script (bash-only)
#
# Works even when Python is not installed — that's the whole point.
# `scripts/init-registry.py` handles the registry YAML I/O, but THAT
# script requires Python, which is itself one of the deps we're
# probing. To avoid the chicken-and-egg, all probes live here in bash
# and the init wizard (`/wise-init`) calls this script step-by-step.
#
# Subcommands:
#
#   probe-python
#       Emits KEY=VALUE lines:
#         STATUS=ok|missing
#         BINARY=<absolute path>         (when STATUS=ok)
#         VERSION=<x.y.z>                (when STATUS=ok)
#         MODULE_YAML=ok|missing         (when STATUS=ok)
#         MODULE_ULID=ok|missing         (when STATUS=ok)
#         MODULE_TYPING_EXTENSIONS=ok|missing  (when STATUS=ok)
#
#   probe-node
#       Emits:
#         STATUS=ok|missing|too-old
#         BINARY=<absolute path>         (when BINARY is discoverable)
#         VERSION=<x.y.z>                (when parseable)
#         MAJOR=<N>                      (when parseable; integer)
#
#   probe-gh
#       Emits:
#         STATUS=ok|missing
#         BINARY=<absolute path>         (when STATUS=ok)
#         VERSION=<x.y.z>                (when STATUS=ok)
#         AUTHENTICATED=true|false       (when STATUS=ok)
#         LOGIN=<gh login>|              (when AUTHENTICATED=true)
#
#   probe-markitdown
#       Emits:
#         STATUS=ok|missing
#         BINARY=<absolute path>         (when STATUS=ok)
#         VERSION=<x.y.z>                (when STATUS=ok)
#         UV=ok|missing                  (always — the installer probe)
#
# Output is intentionally shell-sourceable — the `/wise-init` wizard
# reads these lines straight into variables.
#
# Exit codes:
#   0 — probe succeeded (regardless of STATUS; "missing" is still a
#       successful probe with a definite answer).
#   1 — bad subcommand, unparseable args.

set -u

NODE_REQUIRED_MAJOR=22

# ---- Python ---------------------------------------------------------------

find_python() {
  if command -v mise >/dev/null 2>&1; then
    local mise_py
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

probe_python() {
  local py
  py="$(find_python || true)"
  if [[ -z "$py" ]]; then
    echo "STATUS=missing"
    return 0
  fi
  local ver
  ver="$("$py" -c 'import sys; print("{}.{}.{}".format(*sys.version_info[:3]))' 2>/dev/null || true)"
  echo "STATUS=ok"
  echo "BINARY=$py"
  echo "VERSION=$ver"
  # Each module probed independently so the wizard can pip-install
  # only the ones that are actually missing.
  for mod in yaml ulid typing_extensions; do
    local var
    # Uppercase + non-alnum → underscore (bash 4 ${var^^} not everywhere).
    var="MODULE_$(echo "$mod" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9' '_')"
    # Strip any trailing underscore from the tr replacement.
    var="${var%_}"
    if "$py" -c "import $mod" >/dev/null 2>&1; then
      echo "$var=ok"
    else
      echo "$var=missing"
    fi
  done
}

# ---- Node -----------------------------------------------------------------

find_node() {
  if command -v mise >/dev/null 2>&1; then
    local mise_node
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
  # "$1 --version" prints "v22.5.0" — strip "v" and take the first
  # dot-separated chunk. Empty on any failure so the caller treats it
  # as outdated.
  local v
  v="$("$1" --version 2>/dev/null || true)"
  v="${v#v}"
  v="${v%%.*}"
  [[ "$v" =~ ^[0-9]+$ ]] && echo "$v" || echo ""
}

probe_node() {
  local nd
  nd="$(find_node || true)"
  if [[ -z "$nd" ]]; then
    echo "STATUS=missing"
    return 0
  fi
  local full_version major
  full_version="$("$nd" --version 2>/dev/null | sed 's/^v//' || true)"
  major="$(node_major_version "$nd")"
  echo "BINARY=$nd"
  echo "VERSION=$full_version"
  echo "MAJOR=$major"
  if [[ -z "$major" ]] || (( major < NODE_REQUIRED_MAJOR )); then
    echo "STATUS=too-old"
  else
    echo "STATUS=ok"
  fi
}

# ---- gh -------------------------------------------------------------------

find_gh() {
  if command -v mise >/dev/null 2>&1; then
    local mise_gh
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

probe_gh() {
  local gh
  gh="$(find_gh || true)"
  if [[ -z "$gh" ]]; then
    echo "STATUS=missing"
    return 0
  fi
  # gh prints "gh version 2.54.0 (2024-07-30)" on line 1 — take the
  # third whitespace-separated token.
  local ver
  ver="$("$gh" --version 2>/dev/null | awk 'NR==1{print $3}' || true)"
  echo "STATUS=ok"
  echo "BINARY=$gh"
  echo "VERSION=$ver"
  if "$gh" auth status >/dev/null 2>&1; then
    echo "AUTHENTICATED=true"
    # `gh api user --jq .login` is the cleanest way to get the
    # authenticated user's handle; fall back to empty if something
    # goes wrong at the API layer.
    local login
    login="$("$gh" api user --jq .login 2>/dev/null || true)"
    echo "LOGIN=$login"
  else
    echo "AUTHENTICATED=false"
    echo "LOGIN="
  fi
}

# ---- markitdown -----------------------------------------------------------

find_uv() {
  if command -v mise >/dev/null 2>&1; then
    local mise_uv
    mise_uv="$(mise which uv 2>/dev/null || true)"
    if [[ -n "$mise_uv" && -x "$mise_uv" ]]; then
      echo "$mise_uv"
      return 0
    fi
  fi
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi
  return 1
}

find_markitdown() {
  if command -v mise >/dev/null 2>&1; then
    local mise_md
    mise_md="$(mise which markitdown 2>/dev/null || true)"
    if [[ -n "$mise_md" && -x "$mise_md" ]]; then
      echo "$mise_md"
      return 0
    fi
  fi
  if command -v markitdown >/dev/null 2>&1; then
    command -v markitdown
    return 0
  fi
  # `uv tool install` drops binaries into the uv tool bin dir
  # (~/.local/bin by default), which is frequently NOT on PATH — a
  # just-installed markitdown would otherwise re-probe as missing.
  local uv uv_bin
  uv="$(find_uv || true)"
  if [[ -n "$uv" ]]; then
    uv_bin="$("$uv" tool dir --bin 2>/dev/null || true)"
    if [[ -n "$uv_bin" && -x "$uv_bin/markitdown" ]]; then
      echo "$uv_bin/markitdown"
      return 0
    fi
  fi
  return 1
}

probe_markitdown() {
  # UV is emitted unconditionally: it's the installer the wizard uses
  # (`uv tool install 'markitdown[all]'`), so the wizard needs to know
  # whether that path is open even when markitdown itself is present.
  local uv_status="missing"
  if [[ -n "$(find_uv || true)" ]]; then
    uv_status="ok"
  fi
  local md
  md="$(find_markitdown || true)"
  if [[ -z "$md" ]]; then
    echo "STATUS=missing"
    echo "UV=$uv_status"
    return 0
  fi
  # "markitdown --version" prints "markitdown 0.1.3" — last token.
  # A binary that can't answer --version is broken (half-finished
  # install, missing interpreter) — report missing so the wizard
  # offers the reinstall instead of caching a dud as healthy.
  local ver
  ver="$("$md" --version 2>/dev/null | awk 'NR==1{print $NF}' || true)"
  if [[ -z "$ver" ]]; then
    echo "STATUS=missing"
    echo "UV=$uv_status"
    return 0
  fi
  echo "STATUS=ok"
  echo "BINARY=$md"
  echo "VERSION=$ver"
  echo "UV=$uv_status"
}

# ---- dispatch -------------------------------------------------------------

case "${1:-}" in
  probe-python)     probe_python ;;
  probe-node)       probe_node ;;
  probe-gh)         probe_gh ;;
  probe-markitdown) probe_markitdown ;;
  *)
    cat <<'USAGE' >&2
Usage: init.sh <subcommand>

Subcommands:
  probe-python      Probe for python3 + the pip modules wise needs.
  probe-node        Probe for node (>= 22 required).
  probe-gh          Probe for the gh CLI + its auth state.
  probe-markitdown  Probe for the markitdown converter + uv installer.

Output format: KEY=VALUE lines. Callers can `source` the output
after prefixing with their namespace.
USAGE
    exit 1
    ;;
esac
