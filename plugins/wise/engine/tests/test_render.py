from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from wise_engine.ledger import empty_usage
from wise_engine.paths import PLUGIN_ROOT
from wise_engine.render import render, render_step, render_vars, unresolved_placeholders, usage_json


def state(**overrides: Any) -> dict[str, Any]:
    return {
        "run_id": "run-123",
        "project": {"name": "proj", "path": "/repo", "kind": "git"},
        "inputs": {},
        "outputs": {"greeting": "hi"},
        "usage": {
            "subscription": empty_usage(),
            "api-key": empty_usage("api-key"),
            "by_harness": {},
            "by_step": {},
        },
        **overrides,
    }


def test_expands_known_placeholders_and_preserves_unresolved() -> None:
    step = {
        "id": "a",
        "type": "bash",
        "run": "{{workflow.dir}}/script.sh {{run.dir}} {{run.id}} {{project.name}} {{greeting}}",
    }
    assert (
        render_step(step, state(), "/wf", "/run/1")["run"] == "/wf/script.sh /run/1 run-123 proj hi"
    )
    assert (
        render("{{run.dir}}/{{run.id}}/{{project.name}}/{{greeting}}", state(), "", "/tmp/run")
        == "/tmp/run/run-123/proj/hi"
    )
    assert render("{{workflow.dir}}/x", state(), "/wf") == "/wf/x"
    assert render("{{workflow.dir}}/x", state(), "") == "/x"
    assert (
        render("{{run.dir}}/x {{no_such_key}} {{ greeting }}", state(), "/wf")
        == "{{run.dir}}/x {{no_such_key}} {{ greeting }}"
    )


def test_output_shadows_only_unresolved_project_placeholders() -> None:
    snapshot = state(outputs={"project.extra": "shadowed", "project.name": "ignored"})
    assert render_step({"run": "{{project.extra}} {{project.name}}"}, snapshot, "", "") == {
        "run": "shadowed proj"
    }
    assert render("{{project.extra}}", snapshot, "", "") == "shadowed"
    assert render("{{project.name}}", state(project=None), "") == "{{project.name}}"


def test_recursive_render_copies_values_without_rendering_map_keys() -> None:
    step = {
        "options": ["{{run.id}}", "x"],
        "groups": {"nested": "{{greeting}}"},
        "args": ["{{run.id}}", {"nested": "{{greeting}}"}, 1, False, None],
        "{{greeting}}": "hi",
    }
    before = copy.deepcopy(step)
    assert render_step(step, state(), "", "") == {
        "options": ["run-123", "x"],
        "groups": {"nested": "hi"},
        "args": ["run-123", {"nested": "hi"}, 1, False, None],
        "{{greeting}}": "hi",
    }
    assert step == before


def test_inputs_outputs_and_plugin_root() -> None:
    snapshot = state(
        inputs={"ticket_ref": "REF-1", "greeting": "input"}, outputs={"greeting": "output"}
    )
    assert render("{{ticket_ref}} {{greeting}}", snapshot, "") == "REF-1 output"
    assert (
        render("Read ${CLAUDE_PLUGIN_ROOT}/agents/architect.md", snapshot, "/wf")
        == f"Read {PLUGIN_ROOT}/agents/architect.md"
    )
    assert (PLUGIN_ROOT / "agents/architect.md").is_file()


def test_usage_views_and_output_override() -> None:
    snapshot = state()
    snapshot["usage"]["subscription"]["input"] = 300
    snapshot["usage"]["by_harness"]["claude"] = {**empty_usage(), "input": 300}
    snapshot["usage"]["by_step"]["classify"] = {**empty_usage(), "input": 300}
    text = render("Usage:\n{{usage}}", snapshot, "/wf", "/run")
    parsed = json.loads(text.removeprefix("Usage:\n"))
    assert parsed["total"]["input"] == 300
    assert parsed["by_pool"]["subscription"]["input"] == 300
    assert parsed["by_harness"]["claude"]["input"] == 300
    assert parsed["by_step"]["classify"]["input"] == 300
    assert text.removeprefix("Usage:\n") == json.dumps(parsed, ensure_ascii=False, indent=2)
    assert render("{{usage}}", state(outputs={"usage": "mine"}), "/wf") == "mine"
    snapshot["usage"].pop("by_step")
    assert json.loads(usage_json(snapshot))["by_step"] == {}


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, ""),
        (True, "true"),
        (False, "false"),
        (1.0, "1"),
        (-0.0, "0"),
        (1e-6, "0.000001"),
        (1e-7, "1e-7"),
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        ([1.0, None, False], "[1,null,false]"),
        ({"text": "café 🦉"}, '{"text":"café 🦉"}'),
    ],
)
def test_scalar_and_json_stringification(value: Any, expected: str) -> None:
    assert render_vars("{{value}}", {"value": value}) == expected


def test_substitution_is_sequential_literal_and_uses_javascript_key_order() -> None:
    assert render_vars("{{a}} {{b}}", {"a": "{{b}}", "b": "$& $1"}) == "{{b}} $1 {{b}} $1"
    assert render_vars("before {{x}} after", {"x": "$$:$`:$'"}) == "before $:before : after after"
    assert render_vars("{{2}}", {"10": "last", "2": "{{10}}"}) == "last"
    assert (
        render_vars("{{value}}", {"value": {"10": "b", "2": "a", "x": "c"}})
        == '{"2":"a","10":"b","x":"c"}'
    )
    assert render_vars("{{unknown}}", {}) == "{{unknown}}"
    assert unresolved_placeholders("{{a}} {{b}} {{a}} {{ two }} {{}} {{nested{x}}}") == [
        "a",
        "b",
        " two ",
    ]
