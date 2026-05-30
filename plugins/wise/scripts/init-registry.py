#!/usr/bin/env python3
# wise plugin — registry YAML I/O for the init system.
#
# Writes / reads / validates the registry file at
# ${CLAUDE_PLUGIN_ROOT}/.wise-init-registry.yaml. The registry is
# populated by `/wise-init` and consumed as a fast-path by the workflow
# engine skills (in lieu of running bootstrap-deps.sh every time).
#
# This script requires Python + PyYAML. It's only invoked AFTER
# `scripts/init.sh probe-python` has confirmed they're present, OR in
# the fast-path check from workflow engine skills — where a missing
# Python correctly surfaces via the standard `_bail_missing_deps`
# fallback in `scripts/engine.py`.
#
# Subcommands:
#   path                    Print the registry path (whether or not it exists).
#   read                    Pretty-print the registry YAML; exit 2 if missing.
#   write <json>            Parse $1 as JSON and overwrite the registry.
#   check                   Fast-path validation for workflow engine skills.
#                           Emits one of:
#                             INIT:ok                  (exit 0)
#                             INIT:uninit              (exit 2)  no file
#                             INIT:stale:<dep>         (exit 2)  required dep absent
#                             INIT:dep-missing:<dep>   (exit 2)  dep recorded as missing
#                           Only python is hard-required. node + gh are
#                           needed only by specific steps; those
#                           steps handle the check themselves.

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError as exc:
    print(f"init-registry.py: {exc}. Run scripts/bootstrap-deps.sh first.", file=sys.stderr)
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or SCRIPT_DIR.parent)
REGISTRY_PATH = PLUGIN_ROOT / ".wise-init-registry.yaml"

# Only python is hard-required for the fast-path. node + gh are
# step-level concerns — a workflow with no bash step that uses gh
# should still run on a machine without gh.
REQUIRED_DEPS_FAST_PATH = ("python",)


# ---------- helpers ---------------------------------------------------------


def load_registry() -> dict | None:
    if not REGISTRY_PATH.is_file():
        return None
    with REGISTRY_PATH.open() as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else None


def save_registry(data: dict) -> None:
    tmp = REGISTRY_PATH.with_suffix(REGISTRY_PATH.suffix + ".tmp")
    with tmp.open("w") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)
    tmp.replace(REGISTRY_PATH)


# ---------- subcommands -----------------------------------------------------


def cmd_path() -> int:
    print(REGISTRY_PATH)
    return 0


def cmd_read() -> int:
    data = load_registry()
    if data is None:
        print(f"registry not found at {REGISTRY_PATH}", file=sys.stderr)
        return 2
    print(yaml.safe_dump(data, sort_keys=False, default_flow_style=False), end="")
    return 0


def cmd_write(payload: str) -> int:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        print(f"invalid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print("registry payload must be a JSON object", file=sys.stderr)
        return 1
    # Let the caller pass whatever shape they want — this script is a
    # dumb pipe into YAML. The /wise-init wizard owns the shape.
    save_registry(data)
    print(REGISTRY_PATH)
    return 0


def cmd_check() -> int:
    data = load_registry()
    if data is None:
        print("INIT:uninit")
        return 2
    deps = data.get("deps") or {}
    if not isinstance(deps, dict):
        print("INIT:stale:deps-shape")
        return 2
    for name in REQUIRED_DEPS_FAST_PATH:
        entry = deps.get(name)
        if not isinstance(entry, dict):
            print(f"INIT:stale:{name}")
            return 2
        if entry.get("status") != "ok":
            print(f"INIT:dep-missing:{name}")
            return 2
    print("INIT:ok")
    return 0


# ---------- main ------------------------------------------------------------


def main(argv: list[str]) -> int:
    if len(argv) < 1:
        print("usage: init-registry.py {path|read|write <json>|check}", file=sys.stderr)
        return 1
    cmd = argv[0]
    if cmd == "path":
        return cmd_path()
    if cmd == "read":
        return cmd_read()
    if cmd == "write":
        if len(argv) < 2:
            print("write: missing JSON payload", file=sys.stderr)
            return 1
        return cmd_write(argv[1])
    if cmd == "check":
        return cmd_check()
    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
