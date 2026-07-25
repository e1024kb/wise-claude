"""Pins `session-end-ingest.sh`'s always-exit-0, empty-stdout hard contract
(plugins/wise/hooks/session-end-ingest.sh) via subprocess — the hook must
never disrupt session teardown, whatever garbage it's fed on stdin.

All four crafted cases short-circuit before `insights.py` is ever invoked
(empty stdin / garbage JSON / missing transcript_path -> exit 0 at the
`[ -n "$TRANSCRIPT" ] || exit 0` guard; no python3 -> exit 0 at the
`command -v python3` guard), so this is hermetic with no ingest side effects.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HOOK = REPO / "plugins" / "wise" / "hooks" / "session-end-ingest.sh"


def _run(stdin_bytes: bytes, env: dict) -> subprocess.CompletedProcess:
    # Absolute path to bash so subprocess doesn't need to resolve it via
    # `env["PATH"]` — that PATH is what the hook itself sees for its
    # internal `command -v python3` check.
    return subprocess.run(
        ["/bin/bash", str(HOOK)],
        input=stdin_bytes,
        capture_output=True,
        env=env,
        timeout=10,
    )


def _base_env(tmp_path) -> dict:
    env = dict(os.environ)
    env["XDG_DATA_HOME"] = str(tmp_path)
    return env


def test_empty_stdin_exits_0_with_empty_stdout(tmp_path):
    result = _run(b"", _base_env(tmp_path))
    assert result.returncode == 0
    assert result.stdout == b""


def test_garbage_non_json_stdin_exits_0_with_empty_stdout(tmp_path):
    result = _run(b"not json at all {{{", _base_env(tmp_path))
    assert result.returncode == 0
    assert result.stdout == b""


def test_valid_json_missing_transcript_path_exits_0_with_empty_stdout(tmp_path):
    result = _run(b'{"session_id": "abc-123"}', _base_env(tmp_path))
    assert result.returncode == 0
    assert result.stdout == b""


def test_python3_absent_exits_0_with_empty_stdout(tmp_path):
    # Stub PATH down to a directory with no python3 at all — the hook's
    # `command -v python3` guard must short-circuit before touching JSON.
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = _base_env(tmp_path)
    env["PATH"] = str(empty_bin)
    result = _run(b'{"transcript_path": "/tmp/whatever.jsonl"}', env)
    assert result.returncode == 0
    assert result.stdout == b""
