from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from wise_engine import ledger, supervision
from wise_engine.paths import PLUGIN_ROOT, runs_root

HELPER = PLUGIN_ROOT / "scripts/wise-helpers.py"


@pytest.fixture
def invoke(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path),
        "WISE_SESSION_ID": "session",
    }

    def call(*args, **overrides):
        return subprocess.run(
            [sys.executable, "-S", str(HELPER), *map(str, args)],
            cwd=tmp_path,
            env={**env, **overrides},
            capture_output=True,
            text=True,
            timeout=5,
        )

    return call, env


def test_standalone_profile_and_sessions(invoke, tmp_path):
    call, env = invoke
    assert call("profile-get").stdout == "medium\n"
    answer = call("profile-set", " MAX ")
    assert answer.returncode == 0 and answer.stderr == ""
    assert answer.stdout == "PROFILE: level=max scope=session session=session\n"
    assert call("profile-get").stdout == "max\n"
    assert call("profile-get", WISE_SESSION_ID="other").stdout == "medium\n"
    answer = call("profile-set", "wrong")
    assert answer.returncode == 2 and answer.stderr == "INVALID:profile-level:wrong\n"
    answer = call("profile-set", "low", WISE_SESSION_ID="../escape")
    assert answer.returncode == 2 and answer.stderr == "INVALID:profile-no-session\n"
    assert call("profile-get", WISE_SESSION_ID="../escape").stdout == "medium\n"
    assert call("current-session-id", CLAUDE_CODE_SESSION_ID="claude").stdout == "claude\n"
    assert call("current-session-id").stdout == "session\n"
    assert call("current-session-id", WISE_SESSION_ID="").stdout.startswith("local-")
    assert call("session-label", "run", "--", "-a-b-c-d-e-f-g-h").stdout == "run_a-b-c-d-e-f-g\n"
    assert call("session-path", "absent").returncode == 2
    session_dir = tmp_path / ".claude/projects" / str(tmp_path.resolve()).replace("/", "-")
    session_dir.mkdir(parents=True)
    transcript = session_dir / "found.jsonl"
    transcript.write_text("")
    assert call("session-path", "found").stdout == str(transcript) + "\n"
    assert call("current-session-id", WISE_SESSION_ID="").stdout == "found\n"
    assert call("runs-root").stdout == str(runs_root(tmp_path, env)) + "\n"


def test_standalone_supervision_cli(invoke, tmp_path):
    call, _ = invoke
    assert json.loads(call("supervise-config").stdout) == dict(
        stale_secs=180, poll_secs=30, max_nudges=2, max_respawns=1
    )
    assert json.loads(call("supervise-config", WISE_WORKER_POLL_SECS="7").stdout)["poll_secs"] == 7
    assert call("worker-heartbeat", tmp_path, "worker", "testing", "task-1").returncode == 0
    assert "\tphase=testing\ttask=task-1\n" in (tmp_path / "workers/worker.hb").read_text()
    assert call("stale-workers", tmp_path, "worker").stdout == ""
    assert call("stale-workers", tmp_path, "worker,missing").stdout == "missing\tNONE\tmissing\t?\n"
    invalid = call("worker-heartbeat", tmp_path, "../bad")
    assert invalid.returncode == 2 and "INVALID:worker-name:" in invalid.stderr


def test_history_uses_v2_and_protects_legacy(invoke, tmp_path):
    call, env = invoke
    root = runs_root(tmp_path, env)
    legacy = root / "old/state.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("status: running\n")
    for name, status in (("active", "running"), ("done", "completed")):
        directory = root / name
        directory.mkdir()
        (directory / "state.json").write_text(
            json.dumps(
                dict(
                    run_id=name,
                    status=status,
                    workflow={"name": "work"},
                    last_activity_at=ledger.utc_now(),
                    harness_session="session",
                )
            )
        )
    listed = call("list-resumable-runs")
    assert [row["run_id"] for row in json.loads(listed.stdout)] == ["active"]
    assert "LEGACY-RUN:old:" in listed.stderr and "migrate" in listed.stderr
    found = call("find-runs-by-session", "session")
    assert found.stdout.startswith("active\twork\trunning\t") and found.stdout.endswith("\tfresh\n")
    assert "done" in call("list-runs", root).stdout
    assert json.loads(call("dump-state", root / "active/state.json").stdout)["run_id"] == "active"
    assert call("dump-state", legacy).returncode == 2
    pruned = call("prune-runs", WISE_RUN_HISTORY_CAP="1")
    assert pruned.stdout == "PRUNED:done\n"
    assert legacy.read_text() == "status: running\n" and (root / "active").is_dir()
    retired = call("next-wave")
    assert retired.returncode == 2


def test_heartbeat_atomic_unique_and_failure_cleanup(tmp_path):
    sources = []
    replace = os.replace

    def record(source, target):
        sources.append(source)
        return replace(source, target)

    with patch.object(supervision.os, "replace", record):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(
                pool.map(
                    lambda task: supervision.worker_heartbeat(
                        tmp_path, "worker", "phase", str(task)
                    ),
                    range(12),
                )
            )
    assert len(set(sources)) == 12
    workers = tmp_path / "workers"
    assert len(list(workers.iterdir())) == 1
    before = (workers / "worker.hb").read_text()
    with (
        patch.object(supervision.os, "replace", side_effect=OSError("blocked")),
        pytest.raises(OSError),
    ):
        supervision.worker_heartbeat(tmp_path, "worker")
    assert (workers / "worker.hb").read_text() == before
    assert list(workers.glob("*.tmp")) == []


@pytest.mark.parametrize("name", ["../bad", "a/b", "", "..", "worker\n"])
def test_heartbeat_rejects_invalid_names(tmp_path, name):
    with pytest.raises(ValueError, match="INVALID:worker-name"):
        supervision.worker_heartbeat(tmp_path, name)
    assert not (tmp_path / "workers").exists()


def test_stale_boundaries_annotations_and_corrupt_files(tmp_path):
    now = datetime(2026, 9, 11, tzinfo=timezone.utc)
    for name, seconds in (("boundary", 180), ("stale", 181), ("future", -5)):
        supervision.worker_heartbeat(
            tmp_path, name, "phase", "task", now=now - timedelta(seconds=seconds)
        )
    workers = tmp_path / "workers"
    (workers / "empty.hb").write_text("")
    (workers / "invalid.hb").write_text("invalid\tphase=x")
    (workers / "binary.hb").write_bytes(b"\xff")
    rows = supervision.stale_workers(tmp_path, "boundary,absent", now=now)
    assert [(row["name"], row["status"], row["age"]) for row in rows] == [
        ("binary", "stale", None),
        ("empty", "stale", None),
        ("invalid", "stale", None),
        ("stale", "stale", 181),
        ("absent", "missing", None),
    ]
    assert supervision.format_stale_worker(rows[-1]) == "absent\tNONE\tmissing\t?"
    assert supervision.read_heartbeat(tmp_path / "missing") is None
    assert supervision.supervise_config(
        {
            "WISE_WORKER_STALE_SECS": "0",
            "WISE_WORKER_POLL_SECS": "garbage",
            "WISE_WORKER_MAX_NUDGES": "-1",
        }
    ) == supervision.supervise_config({})


def test_insights_hook_paths_work_without_site_packages(tmp_path):
    script = PLUGIN_ROOT / "scripts/insights.py"
    for override, expected in (
        ({}, tmp_path / ".local/share/wise/insights"),
        ({"XDG_DATA_HOME": str(tmp_path / "xdg")}, tmp_path / "xdg/wise/insights"),
    ):
        env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"], **override}
        answer = subprocess.run(
            [sys.executable, "-S", str(script), "data-root"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert answer.returncode == 0 and answer.stdout == str(expected) + "\n"
        assert answer.stderr == ""
        missing = subprocess.run(
            [sys.executable, "-S", str(script), "ingest", str(tmp_path / "missing.jsonl")],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert missing.returncode == 0 and missing.stdout == ""


def test_insights_compaction_is_atomic_and_uses_unique_temporaries(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "standalone_insights", PLUGIN_ROOT / "scripts/insights.py"
    )
    assert spec and spec.loader
    insights = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(insights)
    sources = []
    replace = os.replace

    def record(source, target):
        sources.append(str(source))
        return replace(source, target)

    with patch.object(insights.os, "replace", record):
        insights.compact_ledger({"s1": {"session_id": "s1", "n": 1}})
        insights.compact_ledger({"s1": {"session_id": "s1", "n": 2}})
    assert len(set(sources)) == 2
    ledger_path = insights.ledger_path()
    lines = ledger_path.read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["n"] == 2
    assert list(ledger_path.parent.glob("*.tmp")) == []


def test_synthetic_session_is_found_in_workspace_history(invoke, tmp_path):
    call, env = invoke
    session_id = call("current-session-id", WISE_SESSION_ID="").stdout.strip()
    assert session_id.startswith("local-")
    directory = runs_root(tmp_path, env) / "synthetic-run"
    directory.mkdir(parents=True)
    (directory / "state.json").write_text(
        json.dumps(
            {
                "run_id": "synthetic-run",
                "workflow": {"name": "work"},
                "status": "running",
                "harness_session": session_id,
                "last_activity_at": "2999-01-01T00:00:00Z",
            }
        )
    )
    answer = call("find-runs-by-session", session_id, WISE_SESSION_ID="")
    assert answer.returncode == 0
    assert answer.stdout == "synthetic-run\twork\trunning\t2999-01-01T00:00:00Z\tfresh\n"
