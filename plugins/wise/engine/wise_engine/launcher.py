from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


class LaunchError(RuntimeError):
    pass


def validate_root(root: str | Path) -> Path:
    path = Path(root).expanduser().absolute()
    try:
        manifest = json.loads((path / ".claude-plugin/plugin.json").read_text())
        if manifest.get("name") != "wise" or not (path / "engine/engine.sh").is_file():
            raise ValueError("not a Wise installation")
    except (OSError, ValueError) as error:
        raise LaunchError(
            f"Wise installation is unavailable at {path}; refresh with /wise-init"
        ) from error
    return path


def resolve_root(registry: dict[str, Any], host: str | None = None) -> Path:
    key = host or registry.get("default_host")
    binding = registry.get("hosts", {}).get(key)
    if not isinstance(binding, dict):
        raise LaunchError("No Wise installation is registered for this host; run /wise-init")
    source = binding.get("source")
    if source:
        try:
            entries = json.loads(Path(source["path"]).read_text())["plugins"][source["key"]]
            matches = [
                entry
                for entry in entries
                if entry.get("scope") == source["scope"]
                and entry.get("projectPath") == source.get("project_path")
            ]
            if len(matches) != 1:
                raise ValueError("active installation is ambiguous or missing")
            return validate_root(matches[0]["installPath"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise LaunchError(
                "Active Wise install metadata changed; refresh with /wise-init"
            ) from error
    return validate_root(binding["plugin_root"])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    host = os.environ.get("WISE_HOST")
    registry = Path(__file__).resolve().with_name("installations.json")
    try:
        if args[:1] == ["--wise-host"]:
            if len(args) < 2:
                raise LaunchError("--wise-host requires a host name")
            host = args[1]
            args = args[2:]
        override = os.environ.get("WISE_PLUGIN_ROOT")
        data = json.loads(registry.read_text()) if registry.exists() else {}
        root = validate_root(override) if override else resolve_root(data, host)
        host = host or data.get("default_host")
        if host:
            os.environ["WISE_HOST"] = host
        binding = data.get("hosts", {}).get(host, {})
        if "WISE_PYTHON" not in os.environ and binding.get("python"):
            os.environ["WISE_PYTHON"] = binding["python"]
        if args == ["install-root"]:
            print(root)
            return 0
        os.execv("/bin/bash", ["bash", str(root / "engine/engine.sh"), *args])
    except (OSError, ValueError, LaunchError) as error:
        print(f"wise-engine: {error}", file=sys.stderr)
        return 69
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
