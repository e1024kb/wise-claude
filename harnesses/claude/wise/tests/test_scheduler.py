"""Pins `_trigger_rule_satisfied` and `cmd_next_wave`
(harnesses/claude/wise/scripts/workflows.py) — the DAG scheduler's core
trigger-rule truth table, terminal classification, and `when:` evaluation.
"""

from __future__ import annotations

import json

import pytest


def _deps(*statuses):
    return [{"status": s} for s in statuses]


# ---- _trigger_rule_satisfied truth table -----------------------------------

@pytest.mark.parametrize(
    "rule, statuses, expected",
    [
        # all-success: runnable only when every dep completed.
        ("all-success", ("completed", "completed"), (True, False)),
        ("all-success", ("completed", "failed"), (False, True)),
        ("all-success", ("completed", "skipped"), (False, True)),
        ("all-success", ("completed", "cancelled"), (False, True)),
        ("all-success", ("completed", "running"), (False, False)),
        # one-success: runnable as soon as any dep completed.
        ("one-success", ("completed", "pending"), (True, False)),
        ("one-success", ("failed", "skipped"), (False, True)),
        ("one-success", ("failed", "running"), (False, False)),
        # all-done: runnable once every dep reached ANY terminal state.
        ("all-done", ("completed", "failed"), (True, False)),
        ("all-done", ("completed", "running"), (False, False)),
        # none-failed-min-one-success: needs all terminal, zero failed, >=1 success.
        ("none-failed-min-one-success", ("completed", "skipped"), (True, False)),
        ("none-failed-min-one-success", ("completed", "failed"), (False, True)),
        ("none-failed-min-one-success", ("skipped", "cancelled"), (False, False)),
        ("none-failed-min-one-success", ("completed", "running"), (False, False)),
    ],
)
def test_trigger_rule_truth_table(workflows_module, rule, statuses, expected):
    assert workflows_module._trigger_rule_satisfied(rule, _deps(*statuses)) == expected


def test_trigger_rule_empty_deps_always_runnable(workflows_module):
    assert workflows_module._trigger_rule_satisfied("all-success", []) == (True, False)
    assert workflows_module._trigger_rule_satisfied("one-success", []) == (True, False)


def test_trigger_rule_unknown_delegates_to_all_success(workflows_module):
    deps_ok = _deps("completed", "completed")
    deps_bad = _deps("completed", "failed")
    assert (
        workflows_module._trigger_rule_satisfied("bogus-rule", deps_ok)
        == workflows_module._trigger_rule_satisfied("all-success", deps_ok)
        == (True, False)
    )
    assert (
        workflows_module._trigger_rule_satisfied("bogus-rule", deps_bad)
        == workflows_module._trigger_rule_satisfied("all-success", deps_bad)
        == (False, True)
    )


# ---- cmd_next_wave ----------------------------------------------------------

def _write_def(workflows_module, path, steps):
    workflows_module.save_yaml(path, {"name": "t", "steps": steps})


def _write_state(workflows_module, path, steps, outputs=None):
    workflows_module.save_yaml(path, {
        "run_id": "run-1",
        "project": {},
        "outputs": outputs or {},
        "steps": steps,
    })


def _run_next_wave(workflows_module, capsys, def_path, state_path):
    rc = workflows_module.cmd_next_wave(str(def_path), str(state_path))
    out = capsys.readouterr().out
    return rc, json.loads(out)


def test_next_wave_unreachable_pending_is_terminal_failed(workflows_module, tmp_path, capsys):
    def_path = tmp_path / "workflow.yaml"
    state_path = tmp_path / "state.yaml"
    # The definition no longer declares step "orphan" (e.g. removed from the
    # workflow.yaml after the state was created), so it can never be matched
    # to a step_def and never becomes runnable or gets marked skipped — it is
    # stuck "pending" forever. That must classify as terminal:failed.
    _write_def(workflows_module, def_path, [{"id": "a", "type": "bash"}])
    _write_state(workflows_module, state_path, [
        {"id": "a", "status": "completed"},
        {"id": "orphan", "status": "pending"},
    ])
    rc, result = _run_next_wave(workflows_module, capsys, def_path, state_path)
    assert rc == 0
    assert result["runnable"] == []
    assert result["to_skip"] == []
    assert result["terminal"] == "failed"


def test_next_wave_all_terminal_is_completed(workflows_module, tmp_path, capsys):
    def_path = tmp_path / "workflow.yaml"
    state_path = tmp_path / "state.yaml"
    _write_def(workflows_module, def_path, [{"id": "a", "type": "bash"}])
    _write_state(workflows_module, state_path, [{"id": "a", "status": "completed"}])
    rc, result = _run_next_wave(workflows_module, capsys, def_path, state_path)
    assert rc == 0
    assert result["runnable"] == []
    assert result["terminal"] == "completed"


@pytest.mark.parametrize(
    "when_expr, output_val, should_run",
    [
        ("mode == 'fast'", "fast", True),
        ("mode == 'fast'", "slow", False),
        ("mode != 'fast'", "slow", True),
        ("mode != 'fast'", "fast", False),
        # a non-matching (unparseable) `when:` expression is treated TRUE.
        ("this is not a valid expr @@@", "anything", True),
    ],
)
def test_next_wave_when_semantics(
    workflows_module, tmp_path, capsys, when_expr, output_val, should_run
):
    def_path = tmp_path / "workflow.yaml"
    state_path = tmp_path / "state.yaml"
    _write_def(workflows_module, def_path, [
        {"id": "a", "type": "bash", "when": when_expr},
    ])
    _write_state(
        workflows_module, state_path,
        [{"id": "a", "status": "pending"}],
        outputs={"mode": output_val},
    )
    rc, result = _run_next_wave(workflows_module, capsys, def_path, state_path)
    assert rc == 0
    ran_ids = [s["id"] for s in result["runnable"]]
    if should_run:
        assert ran_ids == ["a"]
        assert result["to_skip"] == []
    else:
        assert ran_ids == []
        assert result["to_skip"] == ["a"]
