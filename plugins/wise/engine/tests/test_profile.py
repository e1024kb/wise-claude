import os
import time
from pathlib import Path

import pytest

from wise_engine.profile import (
    PROFILE_GC_SECONDS,
    current_session_id,
    cwd_session_dir,
    profile_dir,
    profile_get,
    profile_set,
    runs_root_for_cwd,
    session_label,
    session_path,
    synthetic_session_id,
)


@pytest.fixture
def opts(tmp_path):
    return {
        "env": {"XDG_DATA_HOME": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "sess-profile-1"},
        "home": str(tmp_path / "home"),
        "cwd": str(tmp_path),
    }


def test_profile_roundtrip_normalization_and_invalid(opts):
    assert profile_get(opts) == "medium"
    for text, expected in [("low", "low"), ("  MAX ", "max")]:
        result = profile_set(text, opts)
        assert result["ok"] and result["session"] == "sess-profile-1"
        assert profile_get(opts) == expected
        assert Path(result["path"]).read_text() == expected + "\n"
        assert result["path"] == str(
            Path(opts["env"]["XDG_DATA_HOME"]) / "wise/profile/sess-profile-1"
        )
    assert profile_set("turbo", opts) == {
        "ok": False,
        "error": "profile-level",
        "message": "INVALID:profile-level:turbo",
    }


@pytest.mark.parametrize("garbage", [b"weird value\n", b"\xff\xfe\x00gar"])
def test_invalid_store_defaults(opts, garbage):
    directory = Path(profile_dir(opts))
    directory.mkdir(parents=True)
    (directory / "sess-profile-1").write_bytes(garbage)
    assert profile_get(opts) == "medium"


def test_failed_replace_cleans_temp(opts):
    directory = Path(profile_dir(opts))
    target = directory / "sess-profile-1"
    target.mkdir(parents=True)
    (target / "block").write_text("x")
    with pytest.raises(OSError):
        profile_set("low", opts)
    assert not list(directory.glob(".tmp-profile-*"))


def test_prune_stale_siblings(opts):
    directory = Path(profile_dir(opts))
    directory.mkdir(parents=True)
    stale, fresh = directory / "stale", directory / "fresh"
    stale.write_text("low")
    old = time.time() - PROFILE_GC_SECONDS - 3600
    os.utime(stale, (old, old))
    fresh.write_text("max")
    (directory / "old-dir").mkdir()
    os.utime(directory / "old-dir", (old, old))
    assert profile_set("medium", opts)["ok"]
    assert not stale.exists() and fresh.exists() and (directory / "old-dir").exists()


@pytest.mark.parametrize("evil", ["../../../../tmp/pwned", "/etc/foo", "..", "a/b", ".", "x" * 129])
def test_traversal_rejected(opts, evil):
    opts["env"]["CLAUDE_CODE_SESSION_ID"] = evil
    assert profile_set("low", opts)["error"] == "profile-no-session"
    assert profile_get(opts) == "medium"


def test_session_precedence_and_transcripts(opts):
    opts["env"]["WISE_SESSION_ID"] = "wise-sess-9"
    assert current_session_id(opts) == "sess-profile-1"
    del opts["env"]["CLAUDE_CODE_SESSION_ID"]
    assert profile_set("max", opts)["session"] == "wise-sess-9"
    del opts["env"]["WISE_SESSION_ID"]
    assert current_session_id(opts) == synthetic_session_id(opts)
    directory = Path(cwd_session_dir(opts))
    directory.mkdir(parents=True)
    older = directory / "older.jsonl"
    older.write_text("{}")
    os.utime(older, (1, 1))
    (directory / "newer.jsonl").write_text("{}")
    (directory / "ignored.txt").write_text("")
    (directory / "directory.jsonl").mkdir()
    assert current_session_id(opts) == "newer"
    assert session_path("newer", opts) == str(directory / "newer.jsonl")
    assert session_path("missing", opts) is None


def test_labels_and_paths():
    assert session_label("01ABC", "ticket-plan") == "01ABC_ticket-plan"
    assert session_label("01ABC", "a-b-c-d-e-f-g-h-i") == "01ABC_a-b-c-d-e-f-g"
    assert session_label("01ABC", "---") == "01ABC_workflow"
    assert (
        runs_root_for_cwd({"env": {"XDG_DATA_HOME": "/x"}, "home": "/h", "cwd": "/a/b"})
        == "/x/wise/runs/-a-b"
    )
    assert profile_dir({"env": {}, "home": "/h"}) == "/h/.local/share/wise/profile"
