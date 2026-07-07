"""Round-trips the state.yaml lifecycle (plugins/wise/scripts/workflows.py):

    cmd_init_state (399-443) -> cmd_start_run (446-474)
    -> cmd_update_step (802-812) -> cmd_record_output (824-829)
    -> cmd_reset_running (834-845)

against a tmpdir state.yaml, via the shared `wise_env` fixture.
"""

from __future__ import annotations

import json


def _def_yaml(workflows_module, tmp_path):
    def_path = tmp_path / "workflow.yaml"
    workflows_module.save_yaml(def_path, {
        "name": "demo",
        "version": 1,
        "steps": [{"id": "a", "type": "bash"}, {"id": "b", "type": "bash"}],
    })
    return def_path


def test_init_state_writes_stub(workflows_module, wise_env, tmp_path):
    def_path = _def_yaml(workflows_module, tmp_path)
    run_dir = tmp_path / "run-1"
    ctx = json.dumps({"claude_session_id": "sess-1", "session_label": "run-1_demo"})

    rc = workflows_module.cmd_init_state(str(def_path), str(run_dir), "run-1", ctx)
    assert rc == 0

    state_path = run_dir / "state.yaml"
    assert state_path.is_file()
    assert (run_dir / "logs").is_dir()

    state = workflows_module.load_yaml(state_path)
    assert state["status"] == "initializing"
    assert state["run_id"] == "run-1"
    assert state["workflow_name"] == "demo"
    assert state["claude_session_id"] == "sess-1"
    assert [s["status"] for s in state["steps"]] == ["pending", "pending"]
    assert [s["id"] for s in state["steps"]] == ["a", "b"]
    assert isinstance(state["last_activity_at"], str) and state["last_activity_at"].endswith("Z")


def test_start_run_merges_inputs_and_flips_running(
    workflows_module, wise_env, tmp_path, monkeypatch
):
    def_path = _def_yaml(workflows_module, tmp_path)
    run_dir = tmp_path / "run-1"
    workflows_module.cmd_init_state(
        str(def_path), str(run_dir), "run-1",
        json.dumps({"claude_session_id": None, "session_label": None}),
    )
    state_path = run_dir / "state.yaml"
    before = workflows_module.load_yaml(state_path)["last_activity_at"]

    # utc_now() has one-second granularity, so init and start_run could
    # produce the identical stamp; force a strictly later value to prove
    # cmd_start_run actually refreshes last_activity_at.
    monkeypatch.setattr(workflows_module, "utc_now", lambda: "2099-01-01T00:00:00Z")

    ctx = json.dumps({
        "control_mode": "wave-sync",
        "worktree": {"path": "/tmp/wt", "branch": "b", "created_by_ws": True},
        "project": {"path": "/tmp/proj", "name": "proj", "kind": "backend"},
        "inputs": {"ticket": "ABC-1"},
    })
    rc = workflows_module.cmd_start_run(str(state_path), ctx)
    assert rc == 0

    state = workflows_module.load_yaml(state_path)
    assert state["status"] == "running"
    assert state["control_mode"] == "wave-sync"
    assert state["worktree"]["branch"] == "b"
    assert state["project"]["kind"] == "backend"
    assert state["outputs"] == {"ticket": "ABC-1"}
    assert state["last_activity_at"] > before
    assert state["last_activity_at"] == "2099-01-01T00:00:00Z"


def test_update_step_mutates_and_unknown_step_returns_1(workflows_module, wise_env, tmp_path):
    def_path = _def_yaml(workflows_module, tmp_path)
    run_dir = tmp_path / "run-1"
    workflows_module.cmd_init_state(
        str(def_path), str(run_dir), "run-1",
        json.dumps({"claude_session_id": None, "session_label": None}),
    )
    state_path = run_dir / "state.yaml"

    rc = workflows_module.cmd_update_step(str(state_path), "a", ["status=running"])
    assert rc == 0
    state = workflows_module.load_yaml(state_path)
    step_a = next(s for s in state["steps"] if s["id"] == "a")
    assert step_a["status"] == "running"

    rc = workflows_module.cmd_update_step(str(state_path), "no-such-step", ["status=running"])
    assert rc == 1


def test_record_output_captures_named_output(workflows_module, wise_env, tmp_path):
    def_path = _def_yaml(workflows_module, tmp_path)
    run_dir = tmp_path / "run-1"
    workflows_module.cmd_init_state(
        str(def_path), str(run_dir), "run-1",
        json.dumps({"claude_session_id": None, "session_label": None}),
    )
    state_path = run_dir / "state.yaml"

    rc = workflows_module.cmd_record_output(str(state_path), "pr_url", "https://example/1")
    assert rc == 0
    state = workflows_module.load_yaml(state_path)
    assert state["outputs"]["pr_url"] == "https://example/1"


def test_reset_running_pops_running_fields_and_reverts_to_pending(
    workflows_module, wise_env, tmp_path
):
    def_path = _def_yaml(workflows_module, tmp_path)
    run_dir = tmp_path / "run-1"
    workflows_module.cmd_init_state(
        str(def_path), str(run_dir), "run-1",
        json.dumps({"claude_session_id": None, "session_label": None}),
    )
    state_path = run_dir / "state.yaml"

    state = workflows_module.load_yaml(state_path)
    for step in state["steps"]:
        if step["id"] == "a":
            step["status"] = "running"
            step["started_at"] = "2026-01-01T00:00:00Z"
            step["run_id"] = "sub-run"
            step["log"] = "/tmp/x.log"
        else:
            step["status"] = "completed"
    state["status"] = "interrupted"
    workflows_module.save_yaml(state_path, state)

    rc = workflows_module.cmd_reset_running(str(state_path))
    assert rc == 0

    state = workflows_module.load_yaml(state_path)
    step_a = next(s for s in state["steps"] if s["id"] == "a")
    assert step_a["status"] == "pending"
    assert "started_at" not in step_a
    assert "run_id" not in step_a
    assert "log" not in step_a
    step_b = next(s for s in state["steps"] if s["id"] == "b")
    assert step_b["status"] == "completed"  # untouched — was not running
    assert state["status"] == "running"  # re-armed from "interrupted"
