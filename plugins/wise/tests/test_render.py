"""Pins the two template renderers (plugins/wise/scripts/workflows.py):

    `_render_step` — used by cmd_next_wave; knows {{workflow.dir}}
    `cmd_render`   — the CLI form; does NOT know {{workflow.dir}}

Both are sequential `str.replace` passes where outputs substitute LAST, so
an output key colliding with a project/run key shadows it, and any
unresolved `{{...}}` is left verbatim.
"""

from __future__ import annotations


def _state(**overrides):
    base = {
        "run_id": "run-123",
        "project": {"name": "proj", "path": "/repo"},
        "outputs": {"greeting": "hi"},
    }
    base.update(overrides)
    return base


def test_render_step_expands_all_known_placeholders(workflows_module):
    step_def = {
        "id": "a",
        "type": "bash",
        "run": "{{workflow.dir}}/script.sh {{run.dir}} {{run.id}} "
               "{{project.name}} {{greeting}}",
    }
    result = workflows_module._render_step(
        step_def, _state(), workflow_dir="/wf", run_dir="/run/1",
    )
    assert result["definition"]["run"] == "/wf/script.sh /run/1 run-123 proj hi"


def test_render_step_output_shadows_unresolved_project_placeholder(workflows_module):
    # project has no "extra" key, so the `for k, v in project.items()` pass
    # leaves the literal text `{{project.extra}}` untouched. Because
    # outputs substitute LAST, an output key literally named "project.extra"
    # then matches — and shadows — that still-unresolved placeholder,
    # pinning the substitution order.
    state = _state(project={"name": "proj"}, outputs={"project.extra": "shadowed-value"})
    step_def = {"id": "a", "type": "bash", "run": "{{project.extra}}"}
    result = workflows_module._render_step(step_def, state, workflow_dir="", run_dir="")
    assert result["definition"]["run"] == "shadowed-value"


def test_render_step_unresolved_placeholder_left_verbatim(workflows_module):
    step_def = {"id": "a", "type": "bash", "run": "{{no_such_key}}"}
    result = workflows_module._render_step(step_def, _state(), workflow_dir="", run_dir="")
    assert result["definition"]["run"] == "{{no_such_key}}"


def test_render_step_recurses_into_lists_and_dicts(workflows_module):
    step_def = {
        "id": "a",
        "type": "prompt",
        "args": ["{{run.id}}", {"nested": "{{greeting}}"}],
    }
    result = workflows_module._render_step(step_def, _state(), workflow_dir="", run_dir="")
    assert result["definition"]["args"] == ["run-123", {"nested": "hi"}]


def test_cmd_render_expands_project_run_and_outputs(workflows_module, tmp_path, capsys):
    state_path = tmp_path / "state.yaml"
    workflows_module.save_yaml(state_path, _state())
    template = "{{run.dir}}/{{run.id}}/{{project.name}}/{{greeting}}"

    rc = workflows_module.cmd_render(template, str(state_path))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == f"{tmp_path}/run-123/proj/hi"


def test_cmd_render_leaves_workflow_dir_literal(workflows_module, tmp_path, capsys):
    state_path = tmp_path / "state.yaml"
    workflows_module.save_yaml(state_path, _state())

    rc = workflows_module.cmd_render("{{workflow.dir}}/x", str(state_path))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "{{workflow.dir}}/x"


def test_cmd_render_output_shadows_unresolved_project_placeholder(workflows_module, tmp_path, capsys):
    state = _state(project={"name": "proj"}, outputs={"project.extra": "shadowed"})
    state_path = tmp_path / "state.yaml"
    workflows_module.save_yaml(state_path, state)

    rc = workflows_module.cmd_render("{{project.extra}}", str(state_path))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "shadowed"
