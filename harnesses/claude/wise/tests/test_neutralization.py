"""Engine neutralization (harnesses/claude/wise/scripts/workflows.py):
the workflow engine must run outside Claude Code — under Codex / Cursor /
Hermes — where `$CLAUDE_PLUGIN_DATA` and the `~/.claude/projects/`
transcript tree do not exist.

Pins:
- `plugin_data_root()` prefers `$CLAUDE_PLUGIN_DATA`, then the
  harness-neutral `$WISE_DATA_DIR`, then the XDG-honouring data root.
- `_current_session_id()` prefers an injected id (Claude or
  `$WISE_SESSION_ID`), then a Claude transcript, then a synthetic
  per-workspace id — never None — so runs stay taggable/resumable with
  no transcript access.
- `find-runs-by-session` locates a run tagged with the synthetic id
  (the off-Claude resume path).
"""
from __future__ import annotations

import io
from contextlib import redirect_stdout


def test_plugin_data_root_prefers_claude_plugin_data(workflows_module, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "claude"))
    monkeypatch.setenv("WISE_DATA_DIR", str(tmp_path / "wise"))
    assert workflows_module.plugin_data_root() == tmp_path / "claude"


def test_plugin_data_root_falls_back_to_wise_data_dir(workflows_module, monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.setenv("WISE_DATA_DIR", str(tmp_path / "wise"))
    assert workflows_module.plugin_data_root() == tmp_path / "wise"


def test_plugin_data_root_falls_back_to_data_root(workflows_module, monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("WISE_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    # Co-located with runs/insights under the XDG data root.
    assert workflows_module.plugin_data_root() == tmp_path / "wise"
    assert workflows_module.plugin_data_root() == workflows_module.wise_data_root()


def test_session_id_prefers_claude_env(workflows_module, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-sid-123")
    monkeypatch.setenv("WISE_SESSION_ID", "wise-sid-456")
    assert workflows_module._current_session_id() == "claude-sid-123"


def test_session_id_uses_wise_session_id_off_claude(workflows_module, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("WISE_SESSION_ID", "wise-sid-456")
    assert workflows_module._current_session_id() == "wise-sid-456"


def test_session_id_synthetic_when_no_transcript(workflows_module, monkeypatch, tmp_path):
    # No injected id and a HOME with no ~/.claude/projects transcript dir
    # (a non-Claude harness): must degrade to a synthetic per-workspace id,
    # never None.
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("WISE_SESSION_ID", raising=False)
    monkeypatch.setattr(workflows_module, "HOME", tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    sid = workflows_module._current_session_id()
    assert sid is not None
    assert sid.startswith("local-")
    # Stable across calls in the same workspace (resume relies on this).
    assert sid == workflows_module._current_session_id()


def test_find_runs_by_session_matches_synthetic_id(workflows_module, monkeypatch, tmp_path, wise_env):
    """The off-Claude resume path: a run tagged with the synthetic session
    id is found by `find-runs-by-session`, with no transcript involved."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("WISE_SESSION_ID", raising=False)
    monkeypatch.setattr(workflows_module, "HOME", tmp_path / "home")

    sid = workflows_module._current_session_id()
    assert sid.startswith("local-")

    run_dir = wise_env / "01JRUNULIDSYNTHETIC0000000"
    run_dir.mkdir(parents=True)
    (run_dir / "state.yaml").write_text(
        "run_id: 01JRUNULIDSYNTHETIC0000000\n"
        "workflow_name: example-workflow\n"
        "status: running\n"
        f"claude_session_id: {sid}\n"
        "last_activity_at: '2999-01-01T00:00:00Z'\n"
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = workflows_module.cmd_find_runs_by_session(sid)
    out = buf.getvalue()
    assert rc == 0
    assert "01JRUNULIDSYNTHETIC0000000" in out
    assert "example-workflow" in out
