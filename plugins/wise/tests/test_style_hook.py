"""Pins the contract of `session-start-style.sh`
(plugins/wise/hooks/session-start-style.sh): it always exits 0, prints one
valid JSON object with the STE100 context for SessionStart and
SubagentStart, and prints nothing when the user opts out or the event is
unknown.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
HOOK = REPO / "plugins" / "wise" / "hooks" / "session-start-style.sh"


def _run(args: list[str], env_extra: dict | None = None, stdin: bytes = b"{}"):
    """Run the hook with WISE_STE unset unless env_extra sets it."""
    env = dict(os.environ)
    env.pop("WISE_STE", None)
    env.update(env_extra or {})
    return subprocess.run(
        ["/bin/bash", str(HOOK), *args],
        input=stdin,
        capture_output=True,
        env=env,
        timeout=10,
    )


@pytest.mark.parametrize("event", ["SessionStart", "SubagentStart"])
def test_emits_context_json(event):
    """Each supported event prints one JSON object with the STE100 context."""
    result = _run([event])
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    out = payload["hookSpecificOutput"]
    assert out["hookEventName"] == event
    assert "ASD-STE100" in out["additionalContext"]
    assert "WISE_STE=off" in out["additionalContext"]


def test_defaults_to_session_start():
    """With no argument, the hook reports the SessionStart event."""
    result = _run([])
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"


@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "no"])
def test_opt_out_prints_nothing(value):
    """Each opt-out value of WISE_STE turns the hook off."""
    result = _run(["SessionStart"], {"WISE_STE": value})
    assert result.returncode == 0
    assert result.stdout == b""


def test_unknown_event_prints_nothing():
    """An unsupported event name prints nothing."""
    result = _run(["PreToolUse"])
    assert result.returncode == 0
    assert result.stdout == b""


def test_garbage_stdin_still_exits_0():
    """Stdin that is not JSON does not change the output."""
    result = _run(["SessionStart"], stdin=b"not json {{{")
    assert result.returncode == 0
    json.loads(result.stdout)
