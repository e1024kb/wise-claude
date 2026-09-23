import asyncio
import copy
import json
import subprocess
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
    "input.base_branch",
]
MODES = {"input.review_mode": "ask", "input.implement_mode": "now"}
AUTO = {"worktree": "current", "permissions.claude": "auto"}


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
    assert ids(p.build_questionary(defn, ready)) == [
        "worktree",
        "step-select",
        *(i for i in INPUTS if i != "input.gap_mode"),
    ]
    answer = {"worktree": "current", "step-select": OPTIONAL, **MODES}
    stage = p.build_questionary(defn, ready, answer)
    assert "input.gap_mode" in ids(stage)
    assert [i for i in ids(stage) if not i.startswith("input.")] == [
        f"harness.{g}" for g in groups(defn)
    ]
    assert stage["defaults"]["harness.analyze-design"] == "claude"
    harness_q = next(q for q in stage["questions"] if q["id"] == "harness.analyze-design")
    assert harness_q["label"] == "Which harness runs: Design spec?"
    # default first, then the canonical picker order, not the caller's order
    shuffled = {"harnesses": ["gemini", "grok", "cursor", "codex", "claude"]}
    for ctx in (ready, shuffled):
        q = next(
            q
            for q in p.build_questionary(defn, ctx, answer)["questions"]
            if q["id"] == "harness.analyze-design"
        )
        assert [o["value"] for o in q["options"]] == ["claude", "codex", "cursor", "grok", "gemini"]
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
        "full-access",
        "auto",
        "approval-required",
    ]
    assert [o["label"] for o in permission["options"]] == [
        "Bypass permissions",
        "Auto (default)",
        "Approval required",
    ]
    answer.update({"permissions.codex": "auto", **AUTO})
    # Model and effort are a chain: the first page asks the first group's
    # model alone, every next page the previous effort plus the next model.
    stage = p.build_questionary(defn, ready, answer)
    assert [i for i in ids(stage) if not i.startswith("input.")] == ["model.analyze-design"]
    codex = next(q for q in stage["questions"] if q["id"] == "model.analyze-design")
    assert codex["default"] == "gpt-6-astra"
    assert [o["value"] for o in codex["options"]] == [
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
    ]
    assert codex["options"][0]["label"] == "GPT-6 Astra (default)"
    answer["model.analyze-design"] = "gpt-6-luna"
    stage = p.build_questionary(defn, ready, answer)
    assert [i for i in ids(stage) if not i.startswith("input.")] == [
        "effort.analyze-design",
        "model.research-context",
    ]
    assert stage["pages"][-1] == ["effort.analyze-design", "model.research-context"]
    eq = next(q for q in stage["questions"] if q["id"] == "effort.analyze-design")
    assert eq["default"] == "high" and eq["label"] == "Effort for GPT-6 Luna: Design spec?"
    assert [o["value"] for o in eq["options"]] == ["medium", "high", "low"]
    assert eq["options"][1]["label"] == "high (default)"
    answer["effort.analyze-design"] = "medium"
    answer.update({f"model.{g}": "claude-haiku-4-5" for g in groups(defn)[1:]})
    stage = p.build_questionary(defn, ready, answer)
    assert [i for i in ids(stage) if not i.startswith("input.")] == []
    assert ids(stage) == [i for i in INPUTS if i not in MODES]


def test_model_options_carry_source_and_accept_harness_reported_models():
    defn = definition()
    base = {"worktree": "current", "step-select": OPTIONAL, **MODES, **AUTO}
    reported = [
        dict(id="grok-5", label="grok-5", description="reported", efforts=[]),
        dict(id="grok-4.6", label="dup", description="reported", efforts=[]),
    ]
    ctx = {"harnesses": ["grok"], "models": {"grok": reported}}
    answer = {**base, **{f"harness.{g}": "grok" for g in groups(defn)}, "permissions.grok": "auto"}
    stage = p.build_questionary(defn, ctx, answer)
    question = next(q for q in stage["questions"] if q["id"] == "model.analyze-design")
    assert [(o["value"], o["source"]) for o in question["options"]] == [
        ("grok-4.6", "catalog"),
        ("grok-4.5", "catalog"),
        ("grok-5", "harness"),
    ]
    assert question["default"] == "grok-4.6"
    # without the discovery the two-entry grok catalog still asks
    silent = p.build_questionary(defn, {"harnesses": ["grok"]}, answer)
    assert [o["value"] for o in silent["questions"][-1]["options"]] == ["grok-4.6", "grok-4.5"]
    answer.update({f"model.{g}": "grok-5" for g in groups(defn)})
    assert not any(
        q["id"].startswith(("model.", "effort."))
        for q in p.build_questionary(defn, ctx, answer)["questions"]
    )
    applied = p.apply_answers(defn, answer, ctx)
    assert applied["tuning"]["analyze-design"] == dict(harness="grok", model="grok-5")
    # the same answer without the discovery context is not a known model
    assert p.apply_answers(defn, answer)["tuning"]["analyze-design"] == dict(
        harness="grok", model="grok-4.6"
    )
    claude = p.build_questionary(
        defn, {"harnesses": ["grok"]}, {**base, **{f"harness.{g}": "claude" for g in groups(defn)}}
    )
    question = next(q for q in claude["questions"] if q["id"] == "model.analyze-design")
    assert [o["value"] for o in question["options"]] == [
        "claude-fable-5-1",
        "claude-opus-5-5",
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "claude-fable-5",
    ]
    assert all(o["source"] == "catalog" for o in question["options"])


def test_questionary_with_auth_discovers_models_once():
    class Adapter:
        def __init__(self, identifier, rows=None):
            self.id = identifier
            self.rows = rows
            self.listed = 0

        async def probe_auth(self, auth):
            return {"ok": True}

        async def list_models(self):
            self.listed += 1
            return self.rows or []

    defn = definition()
    adapters = {
        "claude": Adapter("claude"),
        "grok": Adapter(
            "grok", [dict(id="grok-4.5", label="grok-4.5", description="", efforts=[])]
        ),
    }
    answer = {
        "worktree": "current",
        "step-select": OPTIONAL,
        **MODES,
        **AUTO,
        **{f"harness.{g}": "grok" for g in groups(defn)},
        "permissions.grok": "auto",
    }

    async def run():
        asked = await p.build_questionary_with_auth(
            defn, {"harnesses": ["grok"]}, answer, adapters.get
        )
        question = next(q for q in asked["questions"] if q["id"] == "model.analyze-design")
        assert [o["value"] for o in question["options"]] == ["grok-4.6", "grok-4.5"]
        assert (adapters["grok"].listed, adapters["claude"].listed) == (1, 0)
        ctx = await p.with_discovered_models(defn, {"harnesses": ["grok"]}, answer, adapters.get)
        assert ctx["models"] == {"grok": adapters["grok"].rows}
        assert await p.with_discovered_models(defn, ctx, answer, adapters.get) is ctx
        assert adapters["grok"].listed == 2
        # earlier stages never probe; a model answer under validation always does
        early = {"worktree": "current"}
        assert "models" not in await p.with_discovered_models(
            defn, {"harnesses": ["grok"]}, early, adapters.get
        )
        assert adapters["grok"].listed == 2
        validating = {"model.analyze-design": "grok-4.5", "harness.analyze-design": "grok"}
        ctx = await p.with_discovered_models(
            defn, {"harnesses": ["grok"]}, validating, adapters.get
        )
        assert ctx["models"] == {"grok": adapters["grok"].rows}
        assert (adapters["grok"].listed, adapters["claude"].listed) == (3, 1)

    asyncio.run(run())


@pytest.mark.parametrize("ctx", [{}, {"harnesses": ["claude"]}, {"harnesses": []}])
def test_single_harness_skips_question(ctx):
    defn = definition()
    result = p.build_questionary(defn, ctx, {"step-select": OPTIONAL, **MODES, **AUTO})
    assert [i for i in ids(result) if not i.startswith("input.")] == ["model.analyze-design"]


def test_known_inputs_filter_groups():
    defn = definition()
    base = {"step-select": OPTIONAL, **AUTO}
    assert p.known_inputs(defn, {}, None) == dict(
        gap_mode="defaults",
        review_mode="auto",
        worktree_mode="current",
        branch_mode="auto",
        implement_mode="plan-only",
        concurrency="2",
        on_child_failure="continue",
        repo_paths="",
    )
    assert p.known_inputs(defn, {}, {"ticket": [{"ref": "TEST-1"}]})["ticket_id"] == "TEST-1"
    settled = {f"model.{g}": "claude-opus-5-5" for g in groups(defn)}
    for mode, active in [("ask", True), ("auto", False)]:
        answers = {**base, **settled, "input.review_mode": mode}
        answers.pop("model.refine-plan")
        assert ("model.refine-plan" in ids(p.build_questionary(defn, {}, answers))) == active
    for mode, active in [("plan-only", False), ("now", True), ("ask", True)]:
        answers = {**base, **settled, "input.implement_mode": mode}
        answers.pop("model.implement")
        assert ("model.implement" in ids(p.build_questionary(defn, {}, answers))) == active
    enabled = p.enabled_step_ids(defn, OPTIONAL)
    assert p.active_group_ids(defn, enabled, {"inputs": {}, "answers": {}}) == set(groups(defn))
    next(s for s in defn["steps"] if s["id"] == "implement")["when"] = "implement_mode =="
    assert "implement" in p.active_group_ids(
        defn, enabled, {"inputs": {"implement_mode": "plan-only"}, "answers": {}}
    )


def test_deselected_locked_and_unbound_groups():
    defn = extended()
    answers = {"step-select": ["analyze-related"], **MODES, **AUTO}
    asked = []
    for _ in range(8):
        stage = p.build_questionary(defn, {}, answers)
        tuning = [i for i in ids(stage) if i.startswith(("model.", "effort."))]
        if not tuning:
            break
        asked.append(tuning)
        for q in stage["questions"]:
            if q["id"] in tuning:
                answers[q["id"]] = q["default"]
    assert asked == [
        ["model.codebase-audit"],
        ["effort.codebase-audit", "model.build-plan"],
        ["effort.build-plan", "model.refine-plan"],
        ["effort.refine-plan", "model.implement"],
        ["effort.implement", "model.support"],
        ["effort.support"],
    ]
    assert not any(i.endswith(".presentation") for i in ids(stage))
    applied = p.apply_answers(
        defn,
        {
            "step-select": [],
            "model.presentation": "claude-opus-5-5",
            "harness.presentation": "codex",
        },
    )
    assert applied["tuning"]["presentation"] == dict(harness="claude", model="sonnet", effort="low")
    assert "analyze-design" not in applied["enabled_steps"]
    assert applied["tuning"]["analyze-design"] == dict(
        harness="claude", model="claude-opus-5-5", effort="high"
    )
    plain = {
        "steps": [{"id": "a", "type": "agent", "prompt": "x", "group": "g"}],
        "tuning": {"groups": [{"id": "g", "default": {"model": "opus"}}]},
    }
    assert ids(p.build_questionary(plain, {}, AUTO)) == ["model.g"]
    plain["steps"][0].pop("group")
    assert ids(p.build_questionary(plain, {}, AUTO)) == ["model.g"]


def test_epic_children_never_prefill_ticket_inputs():
    context = {
        "ticket": [
            {"ref": "ENG-100", "title": "Epic", "children": ["ENG-101", "ENG-102"]},
            {"ref": "ENG-101", "title": "A", "parent": "ENG-100"},
            {"ref": "ENG-102", "title": "B", "parent": "ENG-100", "state": "Done"},
        ]
    }
    result = p.build_questionary(extended(), {"context": context})
    assert result["defaults"]["input.ticket_id"] == "ENG-100"
    assert p.resolve_from_context("ticket[].title", context) == "Epic"


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
        "options": [
            {"value": "auto", "label": "auto (default)"},
            {"value": "ask", "label": "ask"},
        ],
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

    question = next(
        question
        for question in p.build_questionary({"steps": [], "inputs": [item]})["questions"]
        if question["id"] == "input.mode"
    )
    assert question["kind"] == "choice"
    assert question["default"] == expected_default
    mark = " (default)"
    assert question["options"] == [
        {"value": "yes", "label": "yes" + (mark if expected_default == "yes" else "")},
        {"value": "no", "label": "no"},
        {"value": "", "label": "Leave unset" + (mark if expected_default == "" else "")},
    ]
    assert question_form_schema(question)["properties"]["input.mode"] == {
        "type": "string",
        "title": "Mode?",
        "oneOf": [
            {"const": "yes", "title": "yes" + (mark if expected_default == "yes" else "")},
            {"const": "no", "title": "no"},
            {"const": "", "title": "Leave unset" + (mark if expected_default == "" else "")},
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
    required, optional = [
        question for question in result["questions"] if question["id"].startswith("input.")
    ]
    assert "default" not in required
    assert optional["default"] == "auto"
    assert all(
        "default" not in question
        or question["default"] in {option["value"] for option in question["options"]}
        for question in result["questions"]
    )


def test_multi_form_uses_boolean_fields_for_broad_host_support():
    question = {
        "id": "step-select",
        "kind": "multi",
        "label": "Which passes should run?",
        "options": [
            {"value": "review", "label": "Review", "description": "Check the change"},
            {"value": "verify", "label": "Verify"},
        ],
        "default": ["verify"],
    }

    assert question_form_schema(question) == {
        "type": "object",
        "properties": {
            "step-select.0": {
                "type": "boolean",
                "title": "Review",
                "description": "Check the change",
                "default": False,
            },
            "step-select.1": {
                "type": "boolean",
                "title": "Verify",
                "default": True,
            },
        },
        "required": ["step-select.0", "step-select.1"],
    }
    assert _accepted_answer(
        question,
        {"step-select.0": True, "step-select.1": False},
    ) == ["review"]
    assert _accepted_answer(question, {"step-select.0": True}) is None


def test_needs_steps_input_follows_step_select():
    defn = definition()
    assert "input.gap_mode" not in ids(p.build_questionary(defn))
    others = [s for s in OPTIONAL if s != "gap-analysis"]
    skipped = p.build_questionary(defn, {}, {"step-select": others})
    assert "input.gap_mode" not in ids(skipped)
    asked = p.build_questionary(defn, {}, {"step-select": ["gap-analysis"]})
    assert "input.gap_mode" in ids(asked)
    done = p.complete_answers(defn, {}, {"step-select": others, **AUTO})
    assert "input.gap_mode" not in done["answers"]
    assert p.apply_answers(defn, done["answers"])["inputs"]["gap_mode"] == "defaults"
    stale = {**done["answers"], "input.gap_mode": "ask"}
    assert p.apply_answers(defn, stale)["inputs"]["gap_mode"] == "defaults"


def test_needs_steps_must_name_optional_steps_and_have_default(tmp_path):
    source = (ROOT / "workflows/ticket-plan/workflow.yaml").read_text()
    bad_step = tmp_path / "bad-step.yaml"
    bad_step.write_text(source.replace("needs-steps: [gap-analysis]", "needs-steps: [build-plan]"))
    issues = load_and_validate({"path": str(bad_step)})["issues"]
    assert any(i["path"] == "inputs[1].needs-steps" for i in issues)
    no_default = tmp_path / "no-default.yaml"
    no_default.write_text(source.replace("    default: defaults\n", "", 1))
    issues = load_and_validate({"path": str(no_default)})["issues"]
    assert any(i["path"] == "inputs[1].needs-steps" for i in issues)
    empty = tmp_path / "empty-optional.yaml"
    empty.write_text(
        source.replace(
            "optional: [analyze-design, analyze-related, research-context, gap-analysis]",
            "optional: []",
        )
    )
    issues = load_and_validate({"path": str(empty)})["issues"]
    assert any(i["path"] == "inputs[1].needs-steps" for i in issues)


def test_all_bundled_enum_inputs_are_choices():
    workflows = [
        ROOT / "workflows/ticket-plan/workflow.yaml",
        ROOT / "workflows/code-review/workflow.yaml",
    ]
    questions = []
    for workflow in workflows:
        result = load_and_validate({"path": str(workflow)})
        assert "def" in result, result
        answers = {"step-select": p.optional_step_ids(result["def"])}
        questions.extend(p.build_questionary(result["def"], {}, answers)["questions"])

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
        ({}, dict(harness="claude", model="claude-opus-5-5", effort="high")),
        (
            {"harness.analyze-design": "codex"},
            dict(harness="codex", model="gpt-6-astra", effort="high"),
        ),
        (
            {"harness.analyze-design": "bard"},
            dict(harness="claude", model="claude-opus-5-5", effort="high"),
        ),
        ({"harness.analyze-design": "grok"}, dict(harness="grok", model="grok-4.6")),
        (
            {"harness.analyze-design": "cursor"},
            dict(harness="cursor", model="grok-4.7-high-fast"),
        ),
        ({"harness.analyze-design": "gemini"}, dict(harness="gemini", model="gemini-3.8-flash")),
        (
            {"model.analyze-design": "claude-sonnet-5"},
            dict(harness="claude", model="claude-sonnet-5", effort="medium"),
        ),
        (
            {"model.analyze-design": "gpt-6-sol", "effort.analyze-design": "ultra"},
            dict(harness="claude", model="claude-opus-5-5", effort="high"),
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
    assert done["missing"] == ["input.ticket_id", "input.base_branch"]
    assert done["answers"]["step-select"] == OPTIONAL
    assert "effort.build-plan" in [q["id"] for q in done["questions"]]
    assert "model.implement" not in [q["id"] for q in done["questions"]]
    full = p.complete_answers(defn, ctx, MODES)
    assert all(
        full["answers"][f"effort.{g}"] == ("medium" if g == "support" else "high")
        for g in groups(defn)
    )
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
        worktree_mode="current",
        branch_mode="auto",
        implement_mode="plan-only",
        concurrency="2",
        on_child_failure="continue",
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
    # every provider a running step may use, in picker order, not step order
    assert p.active_harnesses(defn, {"a", "u"}, {"g", "r"}, {}) == [
        "claude",
        "codex",
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


@pytest.mark.parametrize(
    "workflow,default",
    [
        ("code-review", "current"),
        ("example-workflow", "current"),
        ("impl-plan-auto", "current"),
        ("ticket-auto", "new"),
        ("ticket-plan", "current"),
    ],
)
def test_every_bundled_workflow_asks_worktree_first(workflow, default):
    result = load_and_validate({"path": str(ROOT / f"workflows/{workflow}/workflow.yaml")})
    defn = result["def"]
    questions = p.build_questionary(defn)["questions"]
    assert questions[0]["id"] == "worktree"
    assert questions[0]["kind"] == "choice"
    assert questions[0]["default"] == default
    assert {o["value"] for o in questions[0]["options"]} == {"current", "new"}
    for mode in ("current", "new"):
        answers = {"worktree": mode}
        assert "worktree" not in ids(p.build_questionary(defn, answers=answers))
        assert p.apply_answers(defn, answers)["inputs"]["worktree_mode"] == mode


def test_legacy_worktree_input_answer_remains_accepted():
    defn = definition()
    answers = {"input.worktree_mode": "new"}
    assert "worktree" not in ids(p.build_questionary(defn, answers=answers))
    assert p.apply_answers(defn, answers)["worktree"] == "new"
    assert p.known_inputs(defn, {"worktree": "new"})["worktree_mode"] == "new"


@pytest.mark.parametrize(
    "value, invalid",
    [("release/1.2", []), ("main; id", ["input.base_branch"]), ("", ["input.base_branch"])],
)
def test_branch_inputs_must_be_git_branch_names(value, invalid):
    spec = {"inputs": [{"name": "base_branch", "prompt": "Base?", "options-from": "branches"}]}
    assert p.invalid_choice_input_ids(spec, {"base_branch": value}) == invalid


@pytest.mark.parametrize(
    "value, invalid", [("", []), ("7", []), ("0", ["input.minutes"]), ("1441", ["input.minutes"])]
)
def test_text_inputs_are_checked_against_their_validate_regex(value, invalid):
    spec = {
        "inputs": [
            {
                "name": "minutes",
                "prompt": "Minutes?",
                "optional": True,
                "validate": "^([1-9][0-9]{0,2}|1[0-3][0-9]{2}|14[0-3][0-9]|1440)?$",
            }
        ]
    }
    assert p.invalid_choice_input_ids(spec, {"minutes": value}) == invalid


@pytest.mark.parametrize("key", ["worktree", "input.worktree_mode"])
def test_invalid_worktree_answer_is_replaced_with_question(key):
    result = p.build_questionary(definition(), answers={key: "invalid"})
    assert result["questions"][0]["id"] == "worktree"
    assert result["defaults"]["worktree"] == "current"


@pytest.mark.parametrize("mode,expected", [("current", 1), ("new", 0)])
@pytest.mark.parametrize("dirty_kind", ["staged", "untracked"])
def test_ticket_auto_preflight_allows_dirty_source_only_for_new_tree(
    tmp_path, mode, expected, dirty_kind
):
    definition = load_and_validate({"path": str(ROOT / "workflows/ticket-auto/workflow.yaml")})[
        "def"
    ]
    script = next(step["run"] for step in definition["steps"] if step["id"] == "preflight-checks")
    script = script.replace("{{worktree_mode}}", mode)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "tracked").write_text("change")
    if dirty_kind == "staged":
        subprocess.run(["git", "add", "tracked"], cwd=tmp_path, check=True)
    subprocess.run(["git", "remote", "add", "origin", "/unused"], cwd=tmp_path, check=True)
    result = subprocess.run(
        ["bash", "-c", "gh() { return 0; }\n" + script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected
    if mode == "new":
        # `/unused` is a local-path (non-GitHub) origin: no gh auth needed.
        assert "PREFLIGHT: ok" in result.stdout and "not GitHub" in result.stdout
        assert subprocess.check_output(["git", "status", "--porcelain"], cwd=tmp_path)
        local_unauth = subprocess.run(
            ["bash", "-c", "gh() { return 1; }\n" + script],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert local_unauth.returncode == 0 and "PREFLIGHT: ok" in local_unauth.stdout
        # A GitHub origin requires gh auth.
        subprocess.run(
            ["git", "remote", "set-url", "origin", "https://github.com/a/b.git"],
            cwd=tmp_path,
            check=True,
        )
        gh_unauth = subprocess.run(
            ["bash", "-c", "gh() { return 1; }\n" + script],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert gh_unauth.returncode == 1 and "not authenticated" in gh_unauth.stderr
        gh_auth = subprocess.run(
            ["bash", "-c", "gh() { return 0; }\n" + script],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert gh_auth.returncode == 0 and "REMOTE: GitHub" in gh_auth.stdout
        # No origin: units commit locally, no gh needed.
        subprocess.run(["git", "remote", "remove", "origin"], cwd=tmp_path, check=True)
        no_origin = subprocess.run(
            ["bash", "-c", "gh() { return 0; }\n" + script],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert no_origin.returncode == 0 and "REMOTE: none" in no_origin.stdout
    else:
        assert "uncommitted or untracked changes" in result.stderr


def test_branch_input_is_a_choice_from_the_checkout_and_text_without_one():
    defn = definition()
    ready = {"harnesses": ["claude"]}
    plain = next(
        q for q in p.build_questionary(defn, ready)["questions"] if q["id"] == "input.base_branch"
    )
    assert plain["kind"] == "text" and "default" not in plain
    branches = {
        "current": "release-26-9-0",
        "default": "release-26-9-0",
        "options": [
            {"value": "release-26-9-0", "label": "release-26-9-0", "description": "checked out"},
            {"value": "main", "label": "main", "description": "default"},
        ],
    }
    stage = p.build_questionary(defn, {**ready, "branches": branches})
    question = next(q for q in stage["questions"] if q["id"] == "input.base_branch")
    assert question["kind"] == "choice" and question["allow_text"] is True
    assert [o["value"] for o in question["options"]] == ["release-26-9-0", "main"]
    assert question["default"] == "release-26-9-0"
    assert stage["defaults"]["input.base_branch"] == "release-26-9-0"
    answered = p.build_questionary(
        defn, {**ready, "branches": branches}, {"input.base_branch": "x"}
    )
    assert "input.base_branch" not in ids(answered)


def test_branch_choice_accepts_free_text_in_forms_and_answers():
    question = {
        "id": "input.base_branch",
        "kind": "choice",
        "label": "Base?",
        "options": [{"value": "main", "label": "main"}],
        "allow_text": True,
        "default": "main",
    }
    schema = question_form_schema(question)["properties"]["input.base_branch"]
    assert "oneOf" not in schema and schema["examples"] == ["main"] and schema["minLength"] == 1
    assert _accepted_answer(question, {"input.base_branch": "main"}) == "main"
    assert _accepted_answer(question, {"input.base_branch": "release-26-9-0"}) == "release-26-9-0"
    assert _accepted_answer(question, {"input.base_branch": "  "}) is None
    strict = {**question, "allow_text": False}
    assert _accepted_answer(strict, {"input.base_branch": "release-26-9-0"}) is None


@pytest.mark.parametrize("workflow", ["pr-watch", "impl-plan"])
def test_lock_worktree_skips_the_worktree_question(workflow):
    defn = load_and_validate({"path": str(ROOT / f"workflows/{workflow}/workflow.yaml")})["def"]
    assert p.worktree_locked(defn)
    assert "worktree" not in ids(p.build_questionary(defn))
    assert p.build_questionary(defn)["questions"][0]["id"].startswith("input.")
    assert p.apply_answers(defn, {"worktree": "new"})["worktree"] == "current"
    assert p.apply_answers(defn, {"worktree": "new"})["inputs"]["worktree_mode"] == "current"


def test_invalid_model_answer_ids_rejects_unbacked_explicit_models():
    defn = definition()
    group = next(iter(groups(defn)))
    discovered = {"grok": [{"id": "grok-5", "label": "grok-5", "efforts": []}]}
    answers = {f"harness.{group}": "grok", f"model.{group}": "grok-5"}
    assert p.invalid_model_answer_ids(defn, answers, discovered) == []
    # the same answer with the listing gone is invalid, never defaulted
    assert p.invalid_model_answer_ids(defn, answers, {}) == [f"model.{group}"]
    assert p.invalid_model_answer_ids(defn, answers, None) == [f"model.{group}"]
    assert p.invalid_model_answer_ids(defn, {f"model.{group}": "opus"}, None) == []
    # a typed-only model is accepted though no picker lists it
    assert p.invalid_model_answer_ids(defn, {f"model.{group}": "claude-opus-5"}, None) == []
    assert p.invalid_model_answer_ids(defn, {f"model.{group}": ""}, None) == []


def test_retry_questions_collects_every_invalid_model_question():
    defn = definition()
    first, second = groups(defn)[:2]
    ctx = {"harnesses": ["claude"], "models": {}}
    answered = p.complete_answers(defn, ctx, {"worktree": "current"})["answers"]
    given = {**answered, f"model.{first}": "bogus-1", f"model.{second}": "bogus-2"}
    invalid = p.invalid_model_answer_ids(defn, given, None)
    assert invalid == [f"model.{first}", f"model.{second}"]
    retry = {key: value for key, value in given.items() if key not in invalid}
    # A single rebuild stops at the first model page; the retry path must
    # carry a question for every invalid id, not only the first.
    single = [key for key in ids(p.build_questionary(defn, ctx, retry)) if key.startswith("model.")]
    assert single == [f"model.{first}"]
    assert [q["id"] for q in p.retry_questions(defn, ctx, retry, invalid)] == invalid


def test_discover_models_cache_reuses_rows_and_survives_a_failed_listing():
    from wise_engine.models import discover_models

    class Adapter:
        def __init__(self, rows, fail=False):
            self.rows, self.fail, self.listed = rows, fail, 0

        async def list_models(self):
            self.listed += 1
            if self.fail:
                raise RuntimeError("listing timed out")
            return self.rows

    rows = [dict(id="grok-4.5", label="grok-4.5", description="", efforts=[])]
    adapter = Adapter(rows)
    cache = {}

    async def run():
        assert await discover_models(["grok"], lambda _h: adapter, cache) == {"grok": rows}
        assert await discover_models(["grok"], lambda _h: adapter, cache) == {"grok": rows}
        assert adapter.listed == 1
        # a later transient failure keeps the rows the first page discovered
        adapter.fail = True
        assert await discover_models(["grok"], lambda _h: adapter, cache) == {"grok": rows}
        assert await discover_models(["grok"], lambda _h: adapter) == {}
        assert adapter.listed == 2
        # a failed or empty first listing is cached too: one probe per daemon
        for first in (Adapter(rows, fail=True), Adapter([])):
            negative = {}
            for _ in range(2):
                assert await discover_models(["grok"], lambda _h, a=first: a, negative) == {}
            assert first.listed == 1 and negative == {"grok": []}

    asyncio.run(run())


BUNDLED = sorted(path.parent.name for path in (ROOT / "workflows").glob("*/workflow.yaml"))


@pytest.mark.parametrize("workflow", BUNDLED)
def test_bundled_workflows_declare_the_canonical_question_order(workflow):
    """The YAML order is the asked order: shared groups and inputs sit last, in
    the fixed picker order, so the same page holds the same questions across
    workflows and runs."""
    defn = load_and_validate({"path": str(ROOT / f"workflows/{workflow}/workflow.yaml")})["def"]
    declared_groups = defn.get("tuning", {}).get("groups", [])
    assert [g["id"] for g in declared_groups] == [
        g["id"] for g in p.canonical_groups(declared_groups)
    ]
    declared_inputs = defn.get("inputs", [])
    assert [i["name"] for i in declared_inputs] == [
        i["name"] for i in p.canonical_inputs(declared_inputs)
    ]


def test_pages_hold_at_most_four_questions_and_never_straddle_a_stage():
    defn = definition()
    ready = {"harnesses": ["claude", "codex", "cursor", "grok", "gemini"]}
    first = p.build_questionary(defn, ready)
    assert first["pages"] == [
        ["worktree", "step-select", "input.ticket_id", "input.review_mode"],
        ["input.branch_mode", "input.implement_mode", "input.base_branch"],
    ]
    answers = {"worktree": "current", "step-select": OPTIONAL, **MODES}
    stage = p.build_questionary(defn, ready, answers)
    harness = [i for i in ids(stage) if i.startswith("harness.")]
    inputs = [i for i in ids(stage) if i.startswith("input.")]
    chunks = lambda items: [items[i : i + 4] for i in range(0, len(items), 4)]  # noqa: E731
    assert len(harness) == 8 and stage["pages"] == chunks(inputs) + chunks(harness)
    assert all(len(page) <= p.PAGE_SIZE for page in stage["pages"])
