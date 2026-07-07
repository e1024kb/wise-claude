"""Shared pytest fixtures for the wise workflow-engine test suite.

DRYs the importlib loader `test_prune_runs.py` hand-rolled (PLAN-001) into
one module-level import + a `workflows` fixture, plus a `wise_env` fixture
for tmpdir/XDG isolation (same pattern as `test_prune_runs.py:36-42`).
`test_prune_runs.py` itself is left untouched — it landed, passes, and its
own copy of the loader is harmless duplication (see PLAN-004 Decisions).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS_PATH = REPO / "plugins" / "wise" / "scripts" / "workflows.py"

_spec = importlib.util.spec_from_file_location("workflows", WORKFLOWS_PATH)
assert _spec is not None, f"cannot load workflows module from {WORKFLOWS_PATH}"
assert _spec.loader is not None, f"no loader for workflows module at {WORKFLOWS_PATH}"
workflows = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(workflows)


@pytest.fixture
def workflows_module():
    """The `workflows.py` module, loaded once at collection time via
    `importlib`, shared by every test file in this suite."""
    return workflows


@pytest.fixture
def wise_env(tmp_path, monkeypatch):
    """Isolate `wise_data_root()` under a fresh tmpdir and chdir into a
    tmp workspace, so `wise_runs_root_for_cwd()` resolves entirely inside
    `tmp_path` — no test ever touches the real `~/.local/share/wise`.

    Returns the (created) per-workspace runs root.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    runs_root = workflows.wise_runs_root_for_cwd()
    runs_root.mkdir(parents=True, exist_ok=True)
    return runs_root
