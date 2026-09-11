import copy
import json
from pathlib import Path

import pytest

from wise_engine import preflight as p
from wise_engine.defs import load_and_validate, validate_input
from wise_engine.mcp_server import _accepted_answer, question_form_schema

ROOT = Path(__file__).parents[2]
FIXTURES = Path(__file__).parents[1] / "test/fixtures/defs"
OPTIONAL = ["analyze-design", "analyze-related", "research-context", "gap-analysis"]
INPUTS = [
    "input.ticket_id",
    "input.gap_mode",
    "input.review_mode",
    "input.branch_mode",
    "input.implement_mode",
]
MODES = {"input.review_mode": "ask", "input.implement_mode": "now"}
AUTO = {"permissions.claude": "auto"}


def definition():
    result = load_and_validate({"path": str(ROOT / "workflows/ticket-plan/workflow.yaml")})
    assert "def" in result, result
    return result["def"]


def groups(defn):
    return [g["id"] for g in defn["tuning"]["groups"] if not g.get("locked")]


def ids(result):
    return [q["id"] for q in result["questions"]]


def extended():
    defn = definition()
    defn["tuning"]["groups"].append(
        dict(
            id="presentation",
            label="Presentation and summaries",
            default=dict(harness="claude", model="sonnet", effort="low"),
            locked=True,
        )
    )
    defn["profiles"] = {"medium": {"caps": {"max_refine_passes": 2}}}
    defn["inputs"].append(
        {
            "name": "config_prompt",
            "prompt": "Extra guidance for the run?",
            "optional": True,
            "from-context": "guidance",
        }
    )
    return defn


def test_bundled_snapshot():
    expected = json.loads((FIXTURES / "ticket-plan.v2.questionary.json").read_text())
    assert p.build_questionary(definition()) == expected


def test_stage_order_and_explicit_answers():
    defn = definition()
    ready = {"harnesses": ["claude", "codex", "cursor", "grok", "gemini"]}
    assert ids(p.build_questionary(defn, ready)) == ["step-select", *INPUTS]
    answer = {"step-select": OPTIONAL, **MODES}
    stage = p.build_questionary(defn, ready, answer)
    assert [i for i in ids(stage) if not i.startswith("input.")] == [
        f"harness.{g}" for g in groups(defn)
    ]
    assert stage["defaults"]["harness.analyze-design"] == "claude"
    assert (
        next(q for q in stage["questions"] if q["id"] == "harness.analyze-design")["label"]
        == "Which CLI runs: Design spec?"
    )
    # Defaults describe recommendations; building the next stage requires submitted answers.
    assert ids(p.build_questionary(defn, ready, answer)) == ids(stage)
    answer.update(
        {f"harness.{g}": "codex" if g == "analyze-design" else "claude" for g in groups(defn)}
    )
    stage = p.build_questionary(defn, ready, answer)
    assert [i for i in ids(stage) if not i.startswith("input.")] == [
        "permissions.claude",
        "permissions.codex",
    ]
    permission = next(q for q in stage["questions"] if q["id"] == "permissions.codex")
    assert permission["default"] == "auto"
    assert [o["value"] for o in permission["options"]] == [
        "auto",
        "approval-required",
        "full-access",
    ]
    answer.update({"permissions.codex": "auto", **AUTO})
    stage = p.build_questionary(defn, ready, answer)
    assert [i for i in ids(stage) if not i.startswith("input.")] == [
        f"model.{g}" for g in groups(defn)
    ]
    codex = next(q for q in stage["questions"] if q["id"] == "model.analyze-design")
    assert codex["default"] == "gpt-6-astra"
    assert [o["value"] for o in codex["options"]] == [
        "gpt-6-astra",
        "gpt-5.6-sol",
        "gpt-5.6-luna",
        "gpt-5.5",
    ]
    answer.update({f"model.{g}": "claude-haiku-4-5" for g in groups(defn)})
    answer["model.analyze-design"] = "gpt-5.6-luna"
    stage = p.build_questionary(defn, ready, answer)
    assert [i for i in ids(stage) if not i.startswith("input.")] == ["effort.analyze-design"]
    eq = next(q for q in stage["questions"] if q["id"] == "effort.analyze-design")
    assert eq["default"] == "high" and eq["label"] == "Effort for GPT-5.6 Luna: Design spec?"
    answer["effort.analyze-design"] = "medium"
    assert ids(p.build_questionary(defn, ready, answer)) == [i for i in INPUTS if i not in MODES]


@pytest.mark.parametrize("ctx", [{}, {"harnesses": ["claude"]}, {"harnesses": []}])
def test_single_harness_skips_question(ctx):
    defn = definition()
    result = p.build_questionary(defn, ctx, {"step-select": OPTIONAL, **MODES, **AUTO})
    assert [i for i in ids(result) if not i.startswith("input.")] == [
        f"model.{g}" for g in groups(defn)
    ]


def test_known_inputs_filter_groups():
    defn = definition()
    base = {"step-select": OPTIONAL, **AUTO}
    assert p.known_inputs(defn, {}, None) == dict(
        gap_mode="defaults", review_mode="auto", branch_mode="auto", implement_mode="plan-only"
    )
    assert p.known_inputs(defn, {}, {"ticket": [{"ref": "TEST-1"}]})["ticket_id"] == "TEST-1"
    for mode, active in [("ask", True), ("auto", False)]:
        assert (
            "model.refine-plan"
            in ids(p.build_questionary(defn, {}, {**base, "input.review_mode": mode}))
        ) == active
    for mode, active in [("plan-only", False), ("now", True), ("ask", True)]:
        assert (
            "model.implement"
            in ids(p.build_questionary(defn, {}, {**base, "input.implement_mode": mode}))
        ) == active
    enabled = p.enabled_step_ids(defn, OPTIONAL)
    assert p.active_group_ids(defn, enabled, {"inputs": {}, "answers": {}}) == set(groups(defn))
    next(s for s in defn["steps"] if s["id"] == "implement")["when"] = "implement_mode =="
    assert "implement" in p.active_group_ids(
        defn, enabled, {"inputs": {"implement_mode": "plan-only"}, "answers": {}}
    )


def test_deselected_locked_and_unbound_groups():
    defn = extended()
    stage = p.build_questionary(defn, {}, {"step-select": ["analyze-related"], **MODES, **AUTO})
    assert [i for i in ids(stage) if not i.startswith("input.")] == [
        "model.codebase-audit",
        "model.build-plan",
        "model.refine-plan",
        "model.implement",
    ]
    assert not any(i.endswith(".presentation") for i in ids(stage))
    applied = p.apply_answers(
        defn,
        {"step-select": [], "model.presentation": "claude-opus-5", "harness.presentation": "codex"},
    )
    assert applied["tuning"]["presentation"] == dict(harness="claude", model="sonnet", effort="low")
    assert "analyze-design" not in applied["enabled_steps"]
    assert applied["tuning"]["analyze-design"] == dict(
        harness="claude", model="claude-opus-5", effort="high"
    )
    plain = {
        "steps": [{"id": "a", "type": "agent", "prompt": "x", "group": "g"}],
        "tuning": {"groups": [{"id": "g", "default": {"model": "opus"}}]},
    }
    assert ids(p.build_questionary(plain, {}, AUTO)) == ["model.g"]
    plain["steps"][0].pop("group")
    assert ids(p.build_questionary(plain, {}, AUTO)) == ["model.g"]


def test_context_and_optional_inputs():
    context = {
        "ticket": [{"ref": "TEST-1", "title": "T"}, {"ref": "TEST-2"}],
        "guidance": "  keep it small  ",
        "links": ["https://a", "https://b"],
        "decisions": {"db": "sqlite"},
    }
    defn = extended()
    result = p.build_questionary(defn, {"context": context})
    assert result["defaults"]["input.ticket_id"] == "TEST-1, TEST-2"
    assert result["defaults"]["input.config_prompt"] == "keep it small"
    for path, value in [
        ("ticket[].title", "T"),
        ("links[]", "https://a\nhttps://b"),
        ("decisions.db", "sqlite"),
        ("decisions.missing", None),
    ]:
        assert p.resolve_from_context(path, context) == value
    assert p.resolve_from_context("guidance") is None
    result = p.build_questionary(defn)
    assert (
        result["defaults"]["input.config_prompt"] == ""
        and "input.ticket_id" not in result["defaults"]
    )
    assert next(q for q in result["questions"] if q["id"] == "input.config_prompt")["optional"]


def test_plain_alternation_inputs_are_choices_and_keep_validation():
    defn = {
        "steps": [],
        "inputs": [
            {"name": "mode", "prompt": "Mode?", "default": "auto", "validate": "^(auto|ask)$"},
            {"name": "path", "prompt": "Path?", "validate": r"^.+\.yaml$"},
            {"name": "escaped", "prompt": "Escaped?", "validate": r"^(a\.b|c)$"},
            {"name": "optional", "prompt": "Optional?", "validate": "^(yes|no)?$"},
            {
                "name": "extracted",
                "prompt": "Extracted?",
                "extract": "^mode:(.*)$",
                "validate": "^(yes|no)$",
            },
        ],
    }

    result = p.build_questionary(defn)
    mode = next(q for q in result["questions"] if q["id"] == "input.mode")
    assert mode == {
        "id": "input.mode",
        "kind": "choice",
        "label": "Mode?",
        "options": [{"value": "auto", "label": "auto"}, {"value": "ask", "label": "ask"}],
        "default": "auto",
    }
    assert all(
        next(q for q in result["questions"] if q["id"] == f"input.{name}")["kind"] == "text"
        for name in ("path", "escaped", "optional", "extracted")
    )
    assert validate_input("auto", validate="^(auto|ask)$") == {"ok": True, "value": "auto"}
    assert validate_input("other", validate="^(auto|ask)$") == {
        "ok": False,
        "reason": "validate",
        "message": "INVALID:validate",
    }


@pytest.mark.parametrize("default,expected_default", [(None, ""), ("yes", "yes"), ("invalid", "")])
def test_optional_plain_alternation_has_clickable_empty_choice(default, expected_default):
    item = {
        "name": "mode",
        "prompt": "Mode?",
        "optional": True,
        "validate": "^(yes|no)$",
    }
    if default is not None:
        item["default"] = default

    question = p.build_questionary({"steps": [], "inputs": [item]})["questions"][0]
    assert question["kind"] == "choice"
    assert question["default"] == expected_default
    assert question["options"] == [
        {"value": "yes", "label": "yes"},
        {"value": "no", "label": "no"},
        {"value": "", "label": "Leave unset"},
    ]
    assert question_form_schema(question)["properties"]["input.mode"] == {
        "type": "string",
        "title": "Mode?",
        "oneOf": [
            {"const": "yes", "title": "yes"},
            {"const": "no", "title": "no"},
            {"const": "", "title": "Leave unset"},
        ],
        "default": expected_default,
    }
    assert _accepted_answer(question, {"input.mode": ""}) == ""
    assert p.fill_answers([question], {}) == {
        "answers": {"input.mode": expected_default},
        "inputs": {"mode": expected_default},
        "missing": [],
    }


def test_choice_defaults_only_use_selectable_values():
    defn = {
        "steps": [],
        "inputs": [
            {
                "name": "required",
                "prompt": "Required?",
                "default": "invalid",
                "from-context": "decisions.required",
                "validate": "^(auto|ask)$",
            },
            {
                "name": "optional",
                "prompt": "Optional?",
                "optional": True,
                "default": "auto",
                "from-context": "decisions.optional",
                "validate": "^(auto|ask)$",
            },
        ],
    }

    result = p.build_questionary(
        defn, {"context": {"decisions": {"required": "invalid", "optional": "invalid"}}}
    )
    required, optional = result["questions"]
    assert "default" not in required
    assert optional["default"] == "auto"
    assert all(
        "default" not in question
        or question["default"] in {option["value"] for option in question["options"]}
        for question in result["questions"]
    )


def test_all_bundled_enum_inputs_are_choices():
    workflows = [
        ROOT / "workflows/ticket-plan/workflow.yaml",
        ROOT / "workflows/code-review/workflow.yaml",
    ]
    questions = []
    for workflow in workflows:
        result = load_and_validate({"path": str(workflow)})
        assert "def" in result, result
        questions.extend(p.build_questionary(result["def"])["questions"])

    choices = {
        q["id"]: q for q in questions if q["id"].startswith("input.") and q["kind"] == "choice"
    }
    assert set(choices) == {
        "input.gap_mode",
        "input.review_mode",
        "input.branch_mode",
        "input.implement_mode",
        "input.mode",
    }
    assert [option["value"] for option in choices["input.branch_mode"]["options"]] == [
        "auto",
        "current",
        "ask",
    ]


@pytest.mark.parametrize(
    "answer,expected",
    [
        ({}, dict(harness="claude", model="claude-opus-5", effort="high")),
        (
            {"harness.analyze-design": "codex"},
            dict(harness="codex", model="gpt-6-astra", effort="high"),
        ),
        (
            {"harness.analyze-design": "bard"},
            dict(harness="claude", model="claude-opus-5", effort="high"),
        ),
        ({"harness.analyze-design": "grok"}, dict(harness="grok", model="grok-4.6")),
        ({"harness.analyze-design": "gemini"}, dict(harness="gemini", model="gemini-3.8-flash")),
        (
            {"model.analyze-design": "claude-sonnet-5"},
            dict(harness="claude", model="claude-sonnet-5", effort="medium"),
        ),
        (
            {"model.analyze-design": "gpt-5.5", "effort.analyze-design": "ultra"},
            dict(harness="claude", model="claude-opus-5", effort="high"),
        ),
        (
            {"model.analyze-design": "haiku"},
            dict(harness="claude", model="claude-haiku-4-5", effort="medium"),
        ),
    ],
)
def test_apply_catalog_answers(answer, expected):
    applied = p.apply_answers(extended(), answer)
    assert applied["tuning"]["analyze-design"] == expected
    assert applied["profile"] == "medium" and applied["caps"] == {"max_refine_passes": 2}


def test_complete_answers_and_selection():
    defn = extended()
    ctx = {"harnesses": ["claude", "codex"]}
    done = p.complete_answers(defn, ctx, {})
    assert done["missing"] == ["input.ticket_id"]
    assert done["answers"]["step-select"] == OPTIONAL
    assert "effort.build-plan" in [q["id"] for q in done["questions"]]
    assert "model.implement" not in [q["id"] for q in done["questions"]]
    full = p.complete_answers(defn, ctx, MODES)
    assert all(full["answers"][f"effort.{g}"] == "high" for g in groups(defn))
    steered = p.complete_answers(defn, ctx, {"harness.analyze-design": "codex"})
    assert steered["answers"]["model.analyze-design"] == "gpt-6-astra"
    assert steered["answers"]["effort.analyze-design"] == "high"
    defn = definition()
    assert len(p.apply_answers(defn, {})["enabled_steps"]) == len(defn["steps"])
    assert (
        len(p.apply_answers(defn, {"step-select": []})["enabled_steps"]) == len(defn["steps"]) - 4
    )
    enabled = p.apply_answers(defn, {"step-select": "analyze-design, gap-analysis"})[
        "enabled_steps"
    ]
    assert {
        "gap-analysis",
        "analyze-design",
        "build-plan",
        "resolve-gaps",
    } <= enabled and "analyze-related" not in enabled
    assert p.apply_answers(defn, {"input.ticket_id": "TEST-1", "input.gap_mode": "ask"})[
        "inputs"
    ] == dict(
        ticket_id="TEST-1",
        gap_mode="ask",
        review_mode="auto",
        branch_mode="auto",
        implement_mode="plan-only",
    )


def test_harness_options_and_login_hint():
    defn = extended()
    result = p.build_questionary(
        defn,
        {"harnesses": ["codex", "grok"], "logged_out": ["grok"]},
        {"step-select": OPTIONAL, **MODES},
    )
    question = next(q for q in result["questions"] if q["id"] == "harness.analyze-design")
    assert [o["value"] for o in question["options"]] == ["claude", "codex", "grok"]
    assert [o["description"] for o in question["options"]] == [
        "the workflow's default",
        "run these steps on codex",
        "run these steps on grok; not logged in, run `grok login` first",
    ]


def test_permissions_and_fallbacks():
    defn = {
        "steps": [
            dict(id="a", type="agent", group="g"),
            dict(id="u", type="units", groups={"review": "r"}, fallback=["gemini"]),
        ],
        "tuning": {
            "groups": [
                dict(id="g", default={"harness": "codex"}, fallback=["claude"]),
                dict(id="r", default={"harness": "cursor"}, fallback=["grok"]),
            ]
        },
    }
    assert p.active_harnesses(defn, {"a", "u"}, {"g", "r"}, {}) == [
        "codex",
        "claude",
        "cursor",
        "grok",
        "gemini",
    ]
    permissions = p.provider_permissions({"permissions": "full", "permissions.codex": "auto"})
    assert permissions["codex"] == "auto" and permissions["claude"] == "full-access"
    assert p.provider_permissions({"permissions": ["full"]}) == {}
    assert p.invalid_provider_permission_answers(
        {"permissions.codex": ["auto"], "permissions.claude": "invalid"}
    ) == ["permissions.claude", "permissions.codex"]
    assert p.permission_default({"preflight": {"permissions": "allowlist"}}) == "approval-required"
    assert p.permission_default({"preflight": {"permissions": "full"}}) == "full-access"
    for override in [[], ["cursor"]]:
        changed = copy.deepcopy(defn)
        changed["steps"][0]["fallback"] = override
        assert ("claude" in p.active_harnesses(changed, {"a"}, {"g"}, {})) is False


def test_fill_answers_preserves_explicit_and_missing():
    questions = [
        dict(id="choice", kind="choice", default="x"),
        dict(id="input.required", kind="text"),
        dict(id="optional", kind="text", optional=True),
        dict(id="locked", kind="choice", locked=True),
    ]
    result = p.fill_answers(questions, {"choice": "y"})
    assert result == {"answers": {"choice": "y"}, "inputs": {}, "missing": ["input.required"]}
    assert p.describe_tuning({}) == "inherit"
    assert (
        p.describe_tuning(dict(harness="codex", model="gpt-6-astra", effort="high"))
        == "codex / gpt-6-astra / high"
    )


def test_context_empty_values_and_javascript_whitespace():
    assert p.resolve_from_context("links[]", {"links": [""]}) == ""
    assert p.resolve_from_context("guidance", {"guidance": "\ufefftext\ufeff"}) == "text"
    assert p.resolve_from_context("guidance", {"guidance": "\x85text\x85"}) == "\x85text\x85"
    defn = definition()
    assert (
        "analyze-design"
        in p.apply_answers(defn, {"step-select": "\ufeffanalyze-design\ufeff"})["enabled_steps"]
    )
    assert (
        "analyze-design"
        not in p.apply_answers(defn, {"step-select": "\x85analyze-design\x85"})["enabled_steps"]
    )
    assert p.resolve_from_context("decisions.x\ny", {"decisions": {"x\ny": "value"}}) is None
    defn = {
        "steps": [],
        "inputs": [{"name": "link", "from-context": "links[]", "default": "fallback"}],
    }
    assert p.known_inputs(defn, {}, {"links": [""]}) == {"link": ""}
