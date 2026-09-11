from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

ENGINE_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = ENGINE_ROOT.parent


def wise_data_root(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    base = values.get("XDG_DATA_HOME") or str(
        Path(values.get("HOME", str(Path.home()))) / ".local/share"
    )
    return Path(base) / "wise"


def plugin_data_root(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    override = values.get("CLAUDE_PLUGIN_DATA") or values.get("WISE_DATA_DIR")
    return Path(override) if override else wise_data_root(values)


def cwd_slug(cwd: str | Path | None = None) -> str:
    return str(Path(cwd or Path.cwd()).resolve()).replace("/", "-")


def runs_root(cwd: str | Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    return wise_data_root(env) / "runs" / cwd_slug(cwd)
