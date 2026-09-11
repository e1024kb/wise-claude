from __future__ import annotations

import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from .constants import PROFILE_LEVELS
from .paths import cwd_slug, plugin_data_root, wise_data_root

__all__ = ["plugin_data_root", "wise_data_root"]
PROFILE_DEFAULT = "medium"
PROFILE_GC_SECONDS = 30 * 24 * 3600


def _env(opts: dict[str, Any]) -> dict[str, str]:
    return dict(opts.get("env", os.environ))


def _home(opts: dict[str, Any]) -> Path:
    return Path(opts.get("home", _env(opts).get("HOME", str(Path.home()))))


def _data_root(opts: dict[str, Any]) -> Path:
    env = _env(opts)
    env["HOME"] = str(_home(opts))
    return wise_data_root(env)


def runs_root_for_cwd(opts: dict[str, Any] | None = None) -> str:
    options = opts or {}
    return str(_data_root(options) / "runs" / cwd_slug(options.get("cwd")))


def cwd_session_dir(opts: dict[str, Any] | None = None) -> str:
    options = opts or {}
    return str(_home(options) / ".claude/projects" / cwd_slug(options.get("cwd")))


def synthetic_session_id(opts: dict[str, Any] | None = None) -> str:
    return "local-" + (cwd_slug((opts or {}).get("cwd")).strip("-") or "workspace")


def current_session_id(opts: dict[str, Any] | None = None) -> str:
    options = opts or {}
    env = _env(options)
    for name in ("CLAUDE_CODE_SESSION_ID", "WISE_SESSION_ID"):
        sid = env.get(name, "").strip()
        if sid:
            return sid
    directory = Path(cwd_session_dir(options))
    if directory.is_dir():
        newest = None
        for entry in directory.iterdir():
            if not entry.name.endswith(".jsonl"):
                continue
            try:
                if not entry.is_file():
                    continue
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest[0]:
                newest = (mtime, entry.stem)
        if newest:
            return newest[1]
    return synthetic_session_id(options)


def session_path(session_id: str, opts: dict[str, Any] | None = None) -> str | None:
    path = Path(
        os.path.normpath(os.path.join(cwd_session_dir(opts), f"{session_id}.jsonl".lstrip("/")))
    )
    return str(path) if path.is_file() else None


def session_label(run_id: str, workflow_name: str) -> str:
    tokens = [part for part in workflow_name.split("-") if part][:7]
    return run_id + "_" + ("-".join(tokens) or "workflow")


def profile_dir(opts: dict[str, Any] | None = None) -> str:
    return str(_data_root(opts or {}) / "profile")


def profile_safe_sid(opts: dict[str, Any] | None = None) -> str | None:
    sid = current_session_id(opts)
    return sid if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", sid) else None


def is_profile_level(value: str) -> bool:
    return value in PROFILE_LEVELS


def profile_set(raw_level: str, opts: dict[str, Any] | None = None) -> dict[str, Any]:
    options = opts or {}
    level = raw_level.strip().lower()
    if not is_profile_level(level):
        return {"ok": False, "error": "profile-level", "message": f"INVALID:profile-level:{level}"}
    sid = profile_safe_sid(options)
    if not sid:
        return {"ok": False, "error": "profile-no-session", "message": "INVALID:profile-no-session"}
    directory = Path(profile_dir(options))
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / sid
    temporary = directory / (".tmp-profile-" + secrets.token_hex(6))
    try:
        temporary.write_text(level + "\n")
        temporary.replace(target)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    cutoff = options.get("now", time.time)() - PROFILE_GC_SECONDS
    try:
        entries = list(directory.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if entry.name == sid:
            continue
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass
    return {"ok": True, "level": level, "session": sid, "path": str(target)}


def profile_get(opts: dict[str, Any] | None = None) -> str:
    sid = profile_safe_sid(opts)
    if sid:
        try:
            level = (Path(profile_dir(opts)) / sid).read_text().strip().lower()
            if is_profile_level(level):
                return level
        except (OSError, UnicodeError):
            pass
    return PROFILE_DEFAULT
