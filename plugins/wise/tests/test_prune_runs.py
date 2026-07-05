"""Regression test for `cmd_prune_runs` in plugins/wise/scripts/workflows.py.

Pins the fix that stopped non-terminal (resumable) runs from being
deleted once they alone exceeded `WISE_RUN_HISTORY_CAP` — see
docs/plans/001-fix-prune-runs-protects-resumable-runs.md.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS_PATH = REPO / "plugins" / "wise" / "scripts" / "workflows.py"

_spec = importlib.util.spec_from_file_location("workflows", WORKFLOWS_PATH)
workflows = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(workflows)


def _make_run(runs_root: Path, run_id: str, status: str, last_activity_at: str) -> Path:
    """Write `<runs_root>/<run_id>/state.yaml` with `status` and `last_activity_at`."""
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.yaml").write_text(
        f"status: {status}\nlast_activity_at: {last_activity_at}\n"
    )
    return run_dir


@pytest.fixture
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    runs_root = workflows.wise_runs_root_for_cwd()
    runs_root.mkdir(parents=True, exist_ok=True)
    return runs_root


def test_non_terminal_alone_over_cap_survives(_env, monkeypatch):
    runs_root = _env
    monkeypatch.setenv("WISE_RUN_HISTORY_CAP", "2")

    dirs = [
        _make_run(runs_root, f"run-running-{i}", "running", f"2026-01-0{i}T00:00:00Z")
        for i in range(1, 4)
    ]

    assert workflows.cmd_prune_runs() == 0
    assert all(d.is_dir() for d in dirs)


def test_mixed_over_cap_deletes_only_terminal(_env, monkeypatch):
    runs_root = _env
    monkeypatch.setenv("WISE_RUN_HISTORY_CAP", "2")

    non_term_dirs = [
        _make_run(runs_root, f"run-running-{i}", "running", f"2026-02-0{i}T00:00:00Z")
        for i in range(1, 4)
    ]
    term_dirs = [
        _make_run(runs_root, f"run-completed-{i}", "completed", f"2026-01-0{i}T00:00:00Z")
        for i in range(1, 4)
    ]

    workflows.cmd_prune_runs()

    assert all(d.is_dir() for d in non_term_dirs)
    assert all(not d.is_dir() for d in term_dirs)


def test_terminal_budget_partially_filled(_env, monkeypatch):
    runs_root = _env
    monkeypatch.setenv("WISE_RUN_HISTORY_CAP", "4")

    non_term_dirs = [
        _make_run(runs_root, f"run-paused-{i}", "paused", f"2026-03-0{i}T00:00:00Z")
        for i in range(1, 3)
    ]
    # Distinct timestamps: run-completed-3 newest, run-completed-1 oldest.
    term_dirs = [
        _make_run(runs_root, f"run-completed-{i}", "completed", f"2026-01-0{i}T00:00:00Z")
        for i in range(1, 4)
    ]

    workflows.cmd_prune_runs()

    assert all(d.is_dir() for d in non_term_dirs)
    # cap=4, non_term=2 -> term[max(0, 4-2):] == term[2:] -> only the
    # oldest (run-completed-1) is deleted; the two newest survive.
    assert not term_dirs[0].is_dir()
    assert term_dirs[1].is_dir()
    assert term_dirs[2].is_dir()


def test_under_cap_is_a_noop(_env, monkeypatch):
    runs_root = _env
    monkeypatch.setenv("WISE_RUN_HISTORY_CAP", "25")

    dirs = [
        _make_run(runs_root, f"run-completed-{i}", "completed", f"2026-01-0{i}T00:00:00Z")
        for i in range(1, 3)
    ]

    assert workflows.cmd_prune_runs() == 0
    assert all(d.is_dir() for d in dirs)


def test_orphan_dir_deleted_before_terminal_run(_env, monkeypatch):
    runs_root = _env
    monkeypatch.setenv("WISE_RUN_HISTORY_CAP", "1")

    completed_dir = _make_run(
        runs_root, "run-completed-1", "completed", "2026-01-01T00:00:00Z"
    )
    orphan_dir = runs_root / "run-orphan"
    orphan_dir.mkdir(parents=True)

    workflows.cmd_prune_runs()

    assert not orphan_dir.is_dir()
    assert completed_dir.is_dir()
