from __future__ import annotations

import hashlib
import json
import platform

from .paths import ENGINE_ROOT, PLUGIN_ROOT


def plugin_version() -> str:
    version = json.loads((PLUGIN_ROOT / ".claude-plugin/plugin.json").read_text())["version"]
    if not isinstance(version, str):
        raise ValueError("plugin.json has no string version")
    return version


def source_build_id() -> str:
    digest = hashlib.sha256()
    root = ENGINE_ROOT / "wise_engine"
    for path in sorted(p for p in root.rglob("*") if p.suffix in {".py", ".json", ".md"}):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"{plugin_version()}+py.{digest.hexdigest()[:10]}"


def runtime_version() -> str:
    return platform.python_version()
