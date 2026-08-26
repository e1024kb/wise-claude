"""Pins the session token-budget profile store
(plugins/wise/scripts/workflows.py):

    cmd_profile_set / cmd_profile_get / _profile_dir

Contract under test:
- set→get round-trips a valid level for the current session id;
- get NEVER fails: missing store, garbage content, and unresolvable
  paths all degrade to the default ``medium`` with exit 0;
- set validates the level (exit 2 + ``INVALID:profile-level:*``),
  writes atomically (no temp files left behind), and opportunistically
  prunes sibling files older than the GC window;
- the store honours ``XDG_DATA_HOME`` via ``wise_data_root()``.
"""

from __future__ import annotations

import os
import time


def _set_session(monkeypatch, sid="sess-profile-1"):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    return sid


def test_profile_get_missing_store_defaults_medium(workflows_module, wise_env, monkeypatch, capsys):
    _set_session(monkeypatch)
    assert workflows_module.cmd_profile_get() == 0
    assert capsys.readouterr().out.strip() == "medium"


def test_profile_set_get_roundtrip(workflows_module, wise_env, monkeypatch, capsys):
    sid = _set_session(monkeypatch)
    assert workflows_module.cmd_profile_set("low") == 0
    out = capsys.readouterr().out
    assert f"PROFILE: level=low scope=session session={sid}" in out
    assert workflows_module.cmd_profile_get() == 0
    assert capsys.readouterr().out.strip() == "low"


def test_profile_set_normalises_case_and_whitespace(workflows_module, wise_env, monkeypatch, capsys):
    _set_session(monkeypatch)
    assert workflows_module.cmd_profile_set("  MAX ") == 0
    capsys.readouterr()
    workflows_module.cmd_profile_get()
    assert capsys.readouterr().out.strip() == "max"


def test_profile_set_invalid_level_exits_2(workflows_module, wise_env, monkeypatch, capsys):
    _set_session(monkeypatch)
    assert workflows_module.cmd_profile_set("turbo") == 2
    assert "INVALID:profile-level:turbo" in capsys.readouterr().err


def test_profile_get_garbage_content_defaults_medium(workflows_module, wise_env, monkeypatch, capsys):
    sid = _set_session(monkeypatch)
    pdir = workflows_module._profile_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / sid).write_text("weird value\n")
    assert workflows_module.cmd_profile_get() == 0
    assert capsys.readouterr().out.strip() == "medium"


def test_profile_store_honours_xdg_data_home(workflows_module, wise_env, monkeypatch, tmp_path, capsys):
    sid = _set_session(monkeypatch)
    workflows_module.cmd_profile_set("low")
    capsys.readouterr()
    stored = tmp_path / "wise" / "profile" / sid
    assert stored.is_file()
    assert stored.read_text().strip() == "low"


def test_profile_set_failed_replace_leaves_no_temp(workflows_module, wise_env, monkeypatch, capsys):
    """The cleanup branch: a failing os.replace must propagate AND unlink
    the temp file (otherwise every failed write leaks a .tmp-profile-*
    that only the 30-day GC would reap)."""
    import pytest as _pytest
    _set_session(monkeypatch)
    real_replace = workflows_module.os.replace

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(workflows_module.os, "replace", boom)
    with _pytest.raises(OSError):
        workflows_module.cmd_profile_set("low")
    monkeypatch.setattr(workflows_module.os, "replace", real_replace)
    capsys.readouterr()
    pdir = workflows_module._profile_dir()
    leftovers = [e.name for e in pdir.iterdir() if e.name.startswith(".tmp-profile-")]
    assert leftovers == []


def test_profile_set_prunes_stale_siblings(workflows_module, wise_env, monkeypatch, capsys):
    _set_session(monkeypatch, "sess-current")
    pdir = workflows_module._profile_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    stale = pdir / "sess-dead"
    stale.write_text("low\n")
    old = time.time() - (workflows_module._PROFILE_GC_SECONDS + 3600)
    os.utime(stale, (old, old))
    fresh = pdir / "sess-alive"
    fresh.write_text("max\n")

    assert workflows_module.cmd_profile_set("medium") == 0
    capsys.readouterr()
    assert not stale.exists()
    assert fresh.exists()


def test_profile_set_rejects_traversal_session_id(workflows_module, wise_env, monkeypatch, capsys):
    """A hostile session-id env var must never escape the profile dir —
    it is treated as "no session" (exit 2, nothing written anywhere).
    Assert against the path the write WOULD resolve to (`_profile_dir()
    / evil`), so a validation regression fails here rather than passing
    on a hard-coded unrelated location."""
    for evil in ("../../../../tmp/pwned", "/etc/foo", "..", "a/b"):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", evil)
        assert workflows_module.cmd_profile_set("low") == 2
        assert "INVALID:profile-no-session" in capsys.readouterr().err
        would_be = (workflows_module._profile_dir() / evil).resolve()
        # ".." resolves to an existing DIRECTORY (the data root) — the
        # regression signal is a written profile FILE at the target.
        assert not would_be.is_file()


def test_profile_get_traversal_session_id_defaults_medium(workflows_module, wise_env, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "../outside")
    assert workflows_module.cmd_profile_get() == 0
    assert capsys.readouterr().out.strip() == "medium"


def test_profile_get_non_utf8_store_defaults_medium(workflows_module, wise_env, monkeypatch, capsys):
    sid = _set_session(monkeypatch)
    pdir = workflows_module._profile_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / sid).write_bytes(b"\xff\xfe\x00garbage")
    assert workflows_module.cmd_profile_get() == 0
    assert capsys.readouterr().out.strip() == "medium"


def test_wise_session_id_used_when_claude_var_absent(workflows_module, wise_env, monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("WISE_SESSION_ID", "wise-sess-9")
    assert workflows_module.cmd_profile_set("max") == 0
    assert "session=wise-sess-9" in capsys.readouterr().out
    workflows_module.cmd_profile_get()
    assert capsys.readouterr().out.strip() == "max"
