import copy
import re
from pathlib import Path

import pytest

from wise_engine.defs import validate_def
from wise_engine.migrate import (
    MigrationError,
    enum_from_until,
    format_migration,
    migrate_def,
    migrate_file,
    render_def,
)
from wise_engine.yaml_compat import parse_yaml

FIXTURES = Path(__file__).resolve().parents[1] / "test/fixtures/migrate"
V1_KEYS = set(
    "max_iterations agent payload command success cwd question header skip_label confirm_label confirm_value surface".split()
)


def v1(extra=None, steps=None):
    return {
        "version": 1,
        "name": "t",
        "steps": steps if steps is not None else [{"id": "a", "type": "prompt", "prompt": "x"}],
        **(extra or {}),
    }


def convert(raw):
    return migrate_def(raw, "t.yaml")


def note(result, path, kind=None, text=""):
    assert any(
        item["path"] == path and (kind is None or item["kind"] == kind) and text in item["message"]
        for item in result["notes"]
    ), result["notes"]


@pytest.mark.parametrize(
    "name,counts",
    [
        ("ticket-auto", (28, 5, 1)),
        ("impl-plan-auto", (13, 4, 1)),
        ("ticket-plan", (74, 11, 8)),
        ("example-workflow", (29, 1, 1)),
    ],
)
def test_bundled_v1_roundtrip_and_notes(name, counts):
    source = (FIXTURES / f"{name}.v1.yaml").read_text()
    raw = parse_yaml(source)
    original = copy.deepcopy(raw)
    result = convert(raw)
    definition = result["def"]
    assert raw == original
    assert convert(raw) == result
    assert (
        tuple(
            sum(n["kind"] == kind for n in result["notes"])
            for kind in ("rewritten", "warning", "manual")
        )
        == counts
    )
    assert validate_def(definition, name).get("def") is not None
    assert parse_yaml(render_def(definition)) == definition
    assert render_def(definition) == render_def(definition)
    assert all(not V1_KEYS.intersection(step) for step in definition["steps"])
    steps = {step["id"]: step for step in definition["steps"]}
    if name == "ticket-auto":
        access = steps["ensure-access"]
        assert access["outputs"] == ["access_status"] and access["max_turns"] == 1
        assert access["schema"]["properties"]["access_status"]["enum"] == ["ok", "blocked"]
        assert access["prompt"].endswith("`access_status` = one of ok | blocked.\n")
        note(result, "steps[5].type", "manual", "wise_ask")
        assert definition["preflight"] == {"control-mode": "synchronous", "worktree": "current"}
    elif name == "ticket-plan":
        assert steps["analyze-design"]["group"] == "evidence"
        assert "model" not in steps["analyze-design"]
        assert steps["resolve-gaps"]["when"] == "readiness == 'gaps' && gap_mode == 'ask'"
        assert steps["resolve-gaps"]["allow_text"] is True
        assert steps["ensure-access"]["outputs"] == ["ensure_access"]
        note(result, "steps[8].model", "warning", "sonnet")
        note(result, "steps[9].agent", "manual", "led by architect")
    elif name == "impl-plan-auto":
        assert steps["preflight-checks"]["run"].startswith("set -e\n")
        note(result, "steps[3].type", "manual", "interactive")
    else:
        assert steps["list-workflows"]["skill"] == "wise:wise-workflow-list"
        assert steps["classify"]["schema"]["properties"]["release_kind"]["enum"] == [
            "frontend",
            "backend",
            "fullstack",
            "other",
        ]
        assert steps["pick-emoji"]["until"] == "^.+$"


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("^(a|b|c)$", ["a", "b", "c"]),
        ("(yes|no)", ["yes", "no"]),
        ("^ACCESS: (ok|blocked)$", ["ok", "blocked"]),
        ("^(?:plan-only|now)$", ["plan-only", "now"]),
        ("^ready|gaps$", ["ready", "gaps"]),
        ("^(a|a|b)$", ["a", "b"]),
        ("^.+$", None),
        (r"DONE: n=(\d+)", None),
        ("(a|b)(c|d)", None),
        ("(a.*|b)", None),
    ],
)
def test_plain_enum_detection(expression, expected):
    assert enum_from_until(expression) == expected


def test_enum_schema_and_non_enum_warning():
    result = convert(
        v1(
            steps=[
                {
                    "id": "a",
                    "type": "prompt",
                    "prompt": "Pick one.\n",
                    "until": "^(patch|minor|major)$",
                    "outputs": ["kind"],
                },
                {"id": "b", "type": "prompt", "prompt": "no outputs", "until": "^STATE: (on|off)$"},
                {
                    "id": "c",
                    "type": "prompt",
                    "prompt": "x",
                    "until": r"DONE: n=(\d+) list=(\S+)",
                    "outputs": ["n", "list"],
                },
                {
                    "id": "d",
                    "type": "prompt",
                    "prompt": "x",
                    "until": "^(ok|no)$",
                    "outputs": ["x", "y"],
                },
            ]
        )
    )
    a, b, c, d = result["def"]["steps"]
    assert a["schema"] == {
        "type": "object",
        "properties": {"kind": {"type": "string", "enum": ["patch", "minor", "major"]}},
        "required": ["kind"],
        "additionalProperties": False,
    }
    assert (
        a["prompt"]
        == "Pick one.\n\nReturn the field directly as the structured result (no wrapping, no JSON-in-a-string): `kind` = one of patch | minor | major.\n"
    )
    assert list(b) == ["id", "type", "prompt", "schema", "outputs"] and b["outputs"] == ["b"]
    assert "schema" not in c and "schema" not in d
    note(result, "steps[2].until", "warning", "n: { type: string }, list: { type: string }")
    note(result, "steps[3].until", "warning")


def test_interactive_supervised_and_skill_payloads():
    result = convert(
        v1(
            steps=[
                {"id": "a", "type": "interactive", "prompt": "ask"},
                {"id": "b", "type": "supervised-prompt", "prompt": "long"},
                {"id": "c", "type": "skill", "skill": "/wise:wise-commit", "payload": {}},
                {
                    "id": "d",
                    "type": "skill",
                    "skill": "wise-pr-create",
                    "payload": {"base": "main"},
                },
                {"id": "e", "type": "skill", "skill": "wise-report", "payload": "--full"},
            ]
        )
    )
    a, b, c, d, e = result["def"]["steps"]
    assert a["type"] == b["type"] == "agent"
    note(result, "steps[0].type", "manual", "wise_ask")
    note(result, "steps[1].type", "manual", "timeout")
    assert c == {"id": "c", "type": "agent", "skill": "wise:wise-commit"}
    assert d == {
        "id": "d",
        "type": "agent",
        "prompt": 'Run /wise-pr-create with: {"base":"main"}',
        "harness": "claude",
    }
    assert e["prompt"] == "Run /wise-report with: --full"


def test_bash_and_ask_rewrites():
    result = convert(
        v1(
            steps=[
                {
                    "id": "a",
                    "type": "bash",
                    "command": "make test\n",
                    "cwd": "{{project.path}}",
                    "success": {"exit_code": 0},
                    "timeout": 30,
                },
                {
                    "id": "b",
                    "type": "bash",
                    "command": "ls\n",
                    "cwd": "{{project.path}}/sub",
                    "success": {"exit_code": 0, "stdout_matches": "OK"},
                },
                {"id": "c", "type": "bash", "command": "false", "success": {"exit_code": 1}},
                {
                    "id": "free",
                    "type": "ask",
                    "question": "Comments?",
                    "header": "Review",
                    "output": "comments",
                    "skip_label": "Skip",
                },
                {
                    "id": "binary",
                    "type": "ask",
                    "question": "Watch?",
                    "output": "watch",
                    "skip_label": "No",
                    "confirm_label": "Yes",
                    "confirm_value": "yes",
                },
            ]
        )
    )
    a, b, c, free, binary = result["def"]["steps"]
    assert a == {"id": "a", "type": "bash", "run": "make test\n", "timeout": 30}
    assert b["run"] == 'cd "{{project.path}}/sub" || exit 1\nls\n'
    note(result, "steps[1].success", "warning", "stdout_matches")
    note(result, "steps[2].success", "warning", "exit_code")
    assert free == {
        "id": "free",
        "type": "ask",
        "message": "Comments?",
        "output": "comments",
        "options": ["Skip"],
        "allow_text": True,
    }
    assert binary["options"] == ["No", "Yes"] and "allow_text" not in binary
    note(result, "steps[3].skip_label", "manual", "comments to ''")


def test_roster_roles_and_team_collapse():
    bindings = [
        "architect",
        "off",
        "auto",
        False,
        [{"role": "qa-engineer"}, {"role": "wise:architect", "lead": True}, "product-manager"],
    ]
    result = convert(
        v1(
            steps=[
                {"id": chr(97 + i), "type": "prompt", "prompt": "Do it.\n", "agent": value}
                for i, value in enumerate(bindings)
            ]
        )
    )
    steps = result["def"]["steps"]
    assert steps[0]["prompt"].startswith(
        "Act as the wise `architect` agent (see ${CLAUDE_PLUGIN_ROOT}/agents/architect.md).\n\n"
    )
    assert all(step["prompt"] == "Do it.\n" for step in steps[1:4])
    assert steps[4]["prompt"].startswith(
        "Act as the wise `architect` agent leading this step and cover the `qa-engineer`, `product-manager` lenses too"
    )
    note(result, "steps[4].agent", "manual", "model / effort overrides dropped")


@pytest.mark.parametrize(
    "turns,existing,expected",
    [
        (1, None, 1),
        (10, None, 10),
        (11, None, None),
        (0, None, None),
        (1.5, None, None),
        (True, None, None),
        (3, 20, 20),
    ],
)
def test_iterations_and_existing_turn_cap(turns, existing, expected):
    step = {"id": "a", "type": "prompt", "prompt": "x", "max_iterations": turns}
    if existing is not None:
        step["max_turns"] = existing
    result = convert(v1(steps=[step]))
    assert result["def"]["steps"][0].get("max_turns") == expected
    note(
        result,
        "steps[0].max_iterations",
        "rewritten" if existing is None and expected is not None else "warning",
    )


def test_when_inherit_surface_unknowns_and_key_order():
    raw = v1(
        {
            "author": "me",
            "tuning": {"groups": [{"id": "g", "steps": ["a"], "colour": "red"}], "extra": True},
            "description": "d",
            "mystery": 1,
        },
        [
            {
                "id": "a",
                "type": "prompt",
                "depends_on": [],
                "prompt": "x",
                "model": "opus",
                "until": "^(a|b)$",
                "outputs": ["o"],
            },
            {
                "id": "b",
                "type": "prompt",
                "prompt": "x",
                "when": ["a", "b"],
                "model": "inherit",
                "surface": "chat",
                "bogus": "y",
            },
        ],
    )
    result = convert(raw)
    definition = result["def"]
    assert list(definition) == [
        "version",
        "name",
        "steps",
        "author",
        "tuning",
        "description",
        "mystery",
    ]
    assert list(definition["steps"][0]) == [
        "id",
        "type",
        "group",
        "depends_on",
        "prompt",
        "schema",
        "outputs",
    ]
    assert definition["steps"][1]["when"] == "a && b" and "model" not in definition["steps"][1]
    for path in (
        "mystery",
        "tuning.extra",
        "tuning.groups[0].colour",
        "steps[1].bogus",
        "steps[1].surface",
    ):
        note(result, path, "warning")


def test_tuning_profiles_defaults_and_bindings():
    result = convert(
        v1(
            {
                "tuning": {
                    "groups": [
                        {"id": "g", "steps": ["a"]},
                        {"id": "h", "default": "sonnet / high"},
                        {"id": "missing"},
                    ]
                },
                "profiles": {
                    "low": {
                        "tuning": {"g": "claude-opus-4-8 / high"},
                        "caps": {"max_fix_attempts": 3},
                        "step-preset": "quick",
                        "team-mode": "solo",
                    },
                    "medium": {},
                    "max": {"tuning": {"g": "default"}, "skip": ["x"], "description": "all in"},
                },
            },
            [{"id": "a", "type": "prompt", "prompt": "x", "model": "opus", "effort": "high"}],
        )
    )
    definition = result["def"]
    assert definition["tuning"]["groups"][0]["default"] == {
        "harness": "claude",
        "model": "opus",
        "effort": "high",
    }
    assert definition["tuning"]["groups"][1]["default"] == {
        "harness": "claude",
        "model": "sonnet",
        "effort": "high",
    }
    note(result, "tuning.groups[2].default", "manual")
    assert definition["profiles"] == {
        "low": {
            "tuning": {"g": {"model": "claude-opus-4-8", "effort": "high"}},
            "caps": {"max_fix_attempts": 3},
        },
        "medium": {},
        "max": {"description": "all in"},
    }
    note(result, "profiles.low.step-preset", "warning")
    note(result, "profiles.low.team-mode", "rewritten")
    note(result, "profiles.max.tuning.g", "rewritten")


def test_input_menus_and_context():
    result = convert(
        v1(
            {
                "inputs": [
                    {"name": "ticket_ids", "prompt": "Which tickets?"},
                    {"name": "config_prompt", "prompt": "Guidance?", "optional": True},
                    {
                        "name": "mode",
                        "prompt": "Mode?",
                        "default": "auto",
                        "options": [
                            {"value": "auto", "label": "Automatic", "description": "d"},
                            "ask",
                        ],
                    },
                    {
                        "name": "kind",
                        "prompt": "Kind?",
                        "options": ["a.b", "c"],
                        "validate": "^.*$",
                    },
                    {"name": "escaped", "prompt": "Escaped?", "options": ["a.b", "a+b", "a-b"]},
                    {"name": "plain", "prompt": "Plain?", "from-context": "guidance"},
                ]
            }
        )
    )
    inputs = result["def"]["inputs"]
    assert inputs[0]["from-context"] == "ticket[].ref" and inputs[1]["from-context"] == "guidance"
    assert (
        inputs[2]["prompt"] == "Mode? (auto: Automatic | ask)"
        and inputs[2]["validate"] == "^(auto|ask)$"
    )
    assert inputs[3]["validate"] == "^.*$"
    assert inputs[4]["validate"] == r"^(a\.b|a\+b|a-b)$"
    assert not any(n["path"] == "inputs[5].from-context" for n in result["notes"])


def test_optional_flattening_and_labels():
    result = convert(
        v1(
            {
                "step-select": {
                    "optional": [
                        {"id": "a", "label": "Stage A", "ask-group": "Research"},
                        {"id": "gaps", "label": "Gaps", "steps": ["b", "c"]},
                        "d",
                    ],
                    "presets": [{"id": "quick", "skip": ["a"]}],
                }
            },
            [
                {
                    "id": ident,
                    "type": "prompt",
                    "prompt": "x",
                    **({"description": "Own words"} if ident == "b" else {}),
                }
                for ident in "abcd"
            ],
        )
    )
    assert result["def"]["step-select"] == {"optional": ["a", "b", "c", "d"]}
    assert [step.get("description") for step in result["def"]["steps"]] == [
        "Stage A",
        "Own words",
        "Gaps",
        None,
    ]
    note(result, "step-select.optional[1]", "manual", "covered 2 steps")
    note(result, "step-select.presets", "warning")


def test_preflight_project_requires_and_noop():
    result = convert(
        v1(
            {
                "preflight": {
                    "control-mode": "auto-advance",
                    "worktree": "prompt",
                    "rename_session": "skip",
                    "tuning": "prompt",
                    "step-select": "prompt",
                },
                "project-selection": "prompt",
                "agents": "auto",
                "requires": [
                    {"plugin": "wise"},
                    {"skill": "skill-creator:skill-creator"},
                    {"plugin": "wise"},
                ],
            }
        )
    )
    assert result["def"]["preflight"] == {"control-mode": "interactive", "worktree": "current"}
    assert result["def"]["project-selection"] == "ask" and "agents" not in result["def"]
    assert result["def"]["requires"] == {"plugins": ["wise", "skill-creator"]}
    assert "preflight" not in convert(v1({"preflight": {"rename_session": "skip"}}))["def"]
    assert convert(v1({"project-selection": "any"}))["def"]["project-selection"] == "none"
    raw = {"version": 2, "name": "t", "steps": []}
    assert convert(raw)["def"] is raw and len(convert(raw)["notes"]) == 1
    assert convert("nope")["def"] == "nope"
    note(convert({"name": "t", "steps": []}), "version", "rewritten", "added")


def test_render_block_flow_and_yaml11_quoting():
    definition = {
        "version": 2,
        "name": "t",
        "tuning": {
            "groups": [
                {"id": "g", "default": {"harness": "claude", "model": "opus", "effort": "high"}}
            ]
        },
        "profiles": {"low": {"tuning": {"g": {"model": "sonnet"}}}},
        "steps": [
            {
                "id": "a",
                "type": "agent",
                "prompt": "Line one\n\n  indented {{x}}\nLast\n",
                "schema": {
                    "type": "object",
                    "properties": {"v": {"type": "string", "enum": ["yes", "no"]}},
                    "required": ["v"],
                },
                "outputs": ["v"],
                "until": r"DONE: n=(\d+)",
                "depends_on": ["one", "two", "three", "four"],
            }
        ],
    }
    text = render_def(definition)
    assert parse_yaml(text) == definition
    assert "prompt: |\n" in text and 'enum: ["yes", "no"]' in text
    assert "required: [v]" in text and "outputs: [v]" in text
    assert "default: {harness: claude, model: opus, effort: high}" in text
    assert "g: {model: sonnet}" in text and "v: {type: string, enum:" in text
    assert "depends_on:\n" in text and r"\\d" not in text


def test_file_dry_run_out_write_backup_and_noop(tmp_path):
    source = (FIXTURES / "ticket-auto.v1.yaml").read_text()
    path = tmp_path / "ticket-auto.yaml"
    path.write_text(source)
    dry = migrate_file(path)
    assert dry["ok"] and dry["dry_run"] and not dry["already_v2"]
    assert dry["written"] == [] and dry["backup"] is None and path.read_text() == source
    assert "dry run, nothing written" in format_migration(dry)
    assert "result validates with no errors" in format_migration(dry)
    out = tmp_path / "nested/out.yaml"
    result = migrate_file(path, out=out)
    assert result["written"] == [str(out)] and result["backup"] is None
    assert out.read_text() == result["yaml"] and path.read_text() == source
    result = migrate_file(path, write=True)
    assert result["written"] == [str(path)] and result["backup"] == str(path) + ".v1.bak"
    assert Path(result["backup"]).read_text() == source
    second = migrate_file(path, write=True)
    assert second["already_v2"] and second["written"] == [] and len(second["notes"]) == 1
    assert path.read_text() == result["yaml"] and Path(result["backup"]).read_text() == source
    assert "already v2, nothing to migrate" in format_migration(second)


def test_existing_backup_invalid_output_and_missing_file(tmp_path):
    path = tmp_path / "weird.yaml"
    path.write_text("version: 1\nname: weird\nsteps:\n  - id: a\n    type: weird\n")
    backup = Path(str(path) + ".v1.bak")
    backup.write_text("original backup")
    result = migrate_file(path, write=True, out=tmp_path / "out.yaml")
    assert not result["ok"] and len(result["written"]) == 2
    assert backup.read_text() == "original backup"
    note(result, "", "warning", "kept the existing backup")
    assert "validation error(s)" in format_migration(result)
    with pytest.raises(MigrationError) as error:
        migrate_file(tmp_path / "missing.yaml")
    assert error.value.exit_code == 2


def test_roundtrip_comments_retained_with_removed_construct_review(tmp_path):
    source = "# workflow heading\nversion: 1 # schema version\nname: comments\nagents: auto # routing policy\nsteps:\n  # step heading\n  - id: a\n    type: bash\n    command: echo ok # command comment\n    success: {exit_code: 0} # success comment\n"
    path = tmp_path / "comments.yaml"
    path.write_text(source)
    result = migrate_file(path)
    for comment in (
        "workflow heading",
        "schema version",
        "routing policy",
        "step heading",
        "command comment",
        "success comment",
    ):
        assert "# " + comment in result["yaml"]
    assert re.search(r"run: echo ok +# command comment", result["yaml"])
    assert parse_yaml(result["yaml"]) == convert(parse_yaml(source))["def"]
    note(result, "", "manual", "comments on removed constructs")
    assert path.read_text() == source


def test_legacy_run_rejected_without_touching_history(tmp_path):
    path = tmp_path / "state.yaml"
    source = "version: 1\nrun_id: old\nsteps: {done: {status: completed}}\n"
    path.write_text(source)
    for ref in (path, tmp_path):
        with pytest.raises(MigrationError, match="UNSUPPORTED_V1_RUN"):
            migrate_file(ref, write=True)
    assert path.read_text() == source and not Path(str(path) + ".v1.bak").exists()


def test_removed_comment_is_not_confused_with_prompt_text(tmp_path):
    path = tmp_path / "comments.yaml"
    path.write_text(
        "version: 1\nname: comments\nagents: auto # retained note\nsteps:\n  - id: a\n    type: prompt\n    prompt: |\n      # retained note\n"
    )
    result = migrate_file(path)
    assert result["yaml"].count("# retained note") == 2
    assert parse_yaml(result["yaml"])["steps"][0]["prompt"] == "# retained note\n"


def test_text_validation_error_indent_and_hint():
    result = {
        "path": "t.yaml",
        "already_v2": False,
        "notes": [],
        "written": [],
        "backup": None,
        "issues": [
            {
                "level": "error",
                "path": "steps[0].type",
                "message": "unknown type",
                "hint": "use agent",
            },
            {"level": "error", "path": "name", "message": "required"},
        ],
    }
    assert format_migration(result).endswith(
        "  result still has 2 validation error(s):\n  ERROR steps[0].type: unknown type\n    -> use agent\n  ERROR name: required"
    )
