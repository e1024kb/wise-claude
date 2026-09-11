#!/usr/bin/env python3
"""Store host-owned readiness and preserve optional setup decisions outside plugin caches."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import shutil
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ENGINE_ROOT = SCRIPT_DIR.parent / "engine"
PLUGIN_ROOT = SCRIPT_DIR.parent
LEGACY_PATH = PLUGIN_ROOT / ".wise-init-registry.yaml"
HOST = os.environ.get("WISE_HOST", "")
CONFIG = None
REGISTRY_PATH = Path.home() / ".local/share/wise/init" / f"{HOST}.json"
sys.dont_write_bytecode = True
REGISTRY_VERSION = 3
REQUIRED_DEPS_FAST_PATH = ("python", "engine")


def load_registry() -> dict | None:
    path = REGISTRY_PATH if REGISTRY_PATH.exists() else LEGACY_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        try:
            sys.path.insert(0, str(ENGINE_ROOT))
            from wise_engine.yaml_compat import parse_yaml

            try:
                data = parse_yaml(text)
            except Exception:
                return None
        except (ImportError, ValueError):
            return None
    return data if isinstance(data, dict) else None


def save_registry(data: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=REGISTRY_PATH.name + ".", suffix=".tmp", dir=REGISTRY_PATH.parent
    )
    tmp = Path(tmp_name)
    try:
        try:
            fh = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with fh:
            # JSON remains valid YAML and lets the fast-path use only the standard library.
            json.dump(data, fh, indent=2, ensure_ascii=True)
            fh.write("\n")
        os.replace(tmp, REGISTRY_PATH)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def merge_registry(data: dict) -> dict:
    existing = load_registry()
    if existing is None:
        if REGISTRY_PATH.exists() or LEGACY_PATH.exists():
            raise ValueError(
                "Existing init registry cannot be read; optional setup decisions are preserved"
            )
        existing = {}
    if existing and not REGISTRY_PATH.exists():
        existing = {
            **existing,
            "migration": {"source": str(LEGACY_PATH), "legacy": existing},
        }
    merged = {**existing, **data}
    if isinstance(existing.get("deps"), dict) and isinstance(data.get("deps"), dict):
        deps = dict(existing["deps"])
        for key, value in data["deps"].items():
            previous = deps.get(key)
            deps[key] = (
                {**previous, **value}
                if isinstance(previous, dict) and isinstance(value, dict)
                else value
            )
        merged["deps"] = deps
    return merged


def runtime_readiness(binary: str) -> dict | None:
    code = """from pathlib import Path
import json, sys
sys.path.insert(0, sys.argv[1])
from wise_engine.bootstrap import ensure_environment, environment_key
from wise_engine.paths import ENGINE_ROOT, plugin_data_root
requirements = ENGINE_ROOT / "requirements.txt"
interpreter = ensure_environment(requirements, plugin_data_root(), probe=True)
print(json.dumps(dict(python=sys.executable, version=".".join(map(str, sys.version_info[:3])), interpreter=str(interpreter), requirements_key=environment_key(requirements))))
"""
    try:
        result = subprocess.run(
            [binary, "-c", code, str(ENGINE_ROOT)],
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if result.returncode:
            return None
        parsed = json.loads(result.stdout)
        return parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def registration_readiness(interpreter: str) -> dict | None:
    code = """import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from wise_engine.host_setup import doctor, location
home = Path.home()
result = doctor(home=home, host=sys.argv[2], config=sys.argv[3] or None)
state = location(home)
result["binding"] = json.loads((state / "installations.json").read_text())["hosts"][sys.argv[2]]
result["launcher_hashes"] = {name: hashlib.sha256((state / name).read_bytes()).hexdigest() for name in ("bin/wise-engine", "launcher.py")}
print(json.dumps(result))
"""
    try:
        result = subprocess.run(
            [interpreter, "-c", code, str(ENGINE_ROOT), HOST, CONFIG or ""],
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        value = json.loads(result.stdout) if result.returncode == 0 else None
        if (
            isinstance(value, dict)
            and value.get("host") == HOST
            and value.get("registration_ok")
            and value.get("launcher_ok")
            and Path(value.get("plugin_root", "")).resolve() == PLUGIN_ROOT.resolve()
        ):
            return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


def plugin_version() -> str:
    return json.loads((PLUGIN_ROOT / ".claude-plugin/plugin.json").read_text())[
        "version"
    ]


def cmd_path() -> int:
    print(REGISTRY_PATH)
    return 0


def cmd_read() -> int:
    data = load_registry()
    if data is None:
        print(f"registry not found or unreadable at {REGISTRY_PATH}", file=sys.stderr)
        return 2
    print(json.dumps(data, indent=2))
    return 0


def cmd_write(payload: str) -> int:
    try:
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("registry payload must be a JSON object")
        save_registry(merge_registry(data))
    except (ValueError, OSError) as error:
        print(f"init-registry: {error}", file=sys.stderr)
        return 1
    print(REGISTRY_PATH)
    return 0


def cmd_refresh_runtime() -> int:
    binary = (
        os.environ.get("WISE_ENGINE_BASE_PYTHON")
        or os.environ.get("WISE_PYTHON")
        or sys.executable
    )
    ready = runtime_readiness(binary)
    if ready is None:
        print("INIT:stale:engine", file=sys.stderr)
        return 2
    data = dict(
        version=REGISTRY_VERSION,
        plugin_version=plugin_version(),
        host=HOST,
        plugin_root=str(PLUGIN_ROOT.resolve()),
        registration=registration_readiness(ready["interpreter"]),
        completed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        source="bootstrap-deps.sh",
        deps=dict(
            python=dict(status="ok", binary=ready["python"], version=ready["version"]),
            engine=dict(
                status="ok",
                runtime="python",
                python=ready["interpreter"],
                requirements_key=ready["requirements_key"],
            ),
        ),
    )
    try:
        merged = merge_registry(data)
        merged["deps"]["python"].pop("modules", None)
        merged["deps"].pop("node", None)
        merged["deps"].pop("bun", None)
        save_registry(merged)
    except (ValueError, OSError) as error:
        print(f"init-registry: {error}", file=sys.stderr)
        return 1
    print(REGISTRY_PATH)
    return 0


def cmd_check() -> int:
    data = load_registry()
    if data is None:
        print("INIT:uninit")
        return 2
    if data.get("version") != REGISTRY_VERSION:
        print("INIT:stale:runtime-schema")
        return 2
    deps = data.get("deps")
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
    if data.get("plugin_version") != plugin_version():
        print("INIT:stale:plugin-version")
        return 2
    binary = os.environ.get("WISE_PYTHON") or shutil.which("python3")
    ready = runtime_readiness(binary) if isinstance(binary, str) and binary else None
    engine = deps["engine"]
    if (
        ready is None
        or engine.get("runtime") != "python"
        or engine.get("python") != ready["interpreter"]
        or engine.get("requirements_key") != ready["requirements_key"]
    ):
        print("INIT:stale:engine")
        return 2
    if data.get("host") != HOST or data.get("plugin_root") != str(
        PLUGIN_ROOT.resolve()
    ):
        print("INIT:stale:host-ownership")
        return 2
    registration = registration_readiness(ready["interpreter"])
    if registration is None or data.get("registration") != registration:
        print("INIT:stale:registration")
        return 2
    print("INIT:ok")
    return 0


def main(argv: list[str]) -> int:
    global HOST, CONFIG, REGISTRY_PATH
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--host",
        default=os.environ.get("WISE_HOST"),
        choices=("claude", "codex", "cursor", "grok"),
    )
    parser.add_argument("--config")
    parser.add_argument(
        "command", choices=("path", "read", "write", "check", "refresh-runtime")
    )
    parser.add_argument("payload", nargs="?")
    args = parser.parse_args(argv)
    if args.host not in ("claude", "codex", "cursor", "grok"):
        print("init-registry: select --host or WISE_HOST explicitly", file=sys.stderr)
        return 2
    HOST, CONFIG = args.host, args.config
    REGISTRY_PATH = Path.home() / ".local/share/wise/init" / f"{HOST}.json"
    if args.command == "write":
        return cmd_write(args.payload) if args.payload is not None else 1
    return {
        "path": cmd_path,
        "read": cmd_read,
        "check": cmd_check,
        "refresh-runtime": cmd_refresh_runtime,
    }[args.command]()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
