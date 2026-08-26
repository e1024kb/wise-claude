"""Pins the run-tuning surface added for the pre-flight questionaries:

- `get-preflight` — the two opt-in keys (`tuning`, `step-select`) default
  to `skip` (never a surprise prompt for pre-existing workflows) and
  accept only `prompt`/`skip`.
- `get-tuning` / `get-step-select` — schema parsing + loud `INVALID:`
  failures on authoring errors.
- `list-inputs` choice inputs — options/default passthrough and the
  derived membership `validate:` regex.
- `resolve-team --model/--effort` — the run-level override wins over
  member- and step-level pins and is surfaced via `reason`.

(The `none-failed` trigger-rule truth table lives with the other rules
in `test_scheduler.py`.)
"""

from __future__ import annotations

import json
import re

import pytest


def _write_def(workflows_module, tmp_path, extra: dict, steps: list | None = None):
    doc = {
        "version": 1,
        "name": "t",
        "description": "d",
        "steps": steps if steps is not None else [
            {"id": "a", "type": "prompt", "prompt": "x", "model": "opus",
             "effort": "xhigh", "depends_on": []},
            {"id": "b", "type": "prompt", "prompt": "y", "depends_on": ["a"]},
            {"id": "c", "type": "bash", "command": "true", "depends_on": ["a"]},
        ],
    }
    doc.update(extra)
    p = tmp_path / "workflow.yaml"
    workflows_module.save_yaml(p, doc)
    return str(p)


def _kv_out(capsys) -> dict:
    """Parse a KEY=VALUE-lines stdout capture into a dict."""
    return dict(line.split("=", 1) for line in
                capsys.readouterr().out.strip().splitlines())


# ---- get-preflight: new opt-in keys ----------------------------------------

def test_preflight_new_keys_default_skip(workflows_module, tmp_path, capsys):
    path = _write_def(workflows_module, tmp_path, {})
    assert workflows_module.cmd_get_preflight(path) == 0
    out = _kv_out(capsys)
    assert out["TUNING"] == "skip"
    assert out["STEP_SELECT"] == "skip"
    # the original three still default to prompt
    assert out["CONTROL_MODE"] == "prompt"


def test_preflight_new_keys_accept_prompt(workflows_module, tmp_path, capsys):
    path = _write_def(workflows_module, tmp_path,
                      {"preflight": {"tuning": "prompt",
                                     "step-select": "prompt"}})
    assert workflows_module.cmd_get_preflight(path) == 0
    out = _kv_out(capsys)
    assert out["TUNING"] == "prompt"
    assert out["STEP_SELECT"] == "prompt"


def test_preflight_invalid_value_falls_back_to_key_default(
        workflows_module, tmp_path, capsys):
    path = _write_def(workflows_module, tmp_path,
                      {"preflight": {"tuning": "bogus",
                                     "control-mode": "bogus"}})
    assert workflows_module.cmd_get_preflight(path) == 0
    out = _kv_out(capsys)
    assert out["TUNING"] == "skip"           # opt-in key → its default
    assert out["CONTROL_MODE"] == "prompt"   # original key → prompt


def test_preflight_invalid_value_warns(workflows_module, tmp_path, capsys):
    path = _write_def(workflows_module, tmp_path,
                      {"preflight": {"tuning": "bogus"}})
    assert workflows_module.cmd_get_preflight(path) == 0
    assert "WARN:" in capsys.readouterr().err


# ---- get-tuning -------------------------------------------------------------

def test_get_tuning_empty_when_absent(workflows_module, tmp_path, capsys):
    path = _write_def(workflows_module, tmp_path, {})
    assert workflows_module.cmd_get_tuning(path) == 0
    assert json.loads(capsys.readouterr().out) == {"groups": []}


def test_get_tuning_emits_step_defaults(workflows_module, tmp_path, capsys):
    path = _write_def(workflows_module, tmp_path, {"tuning": {"groups": [
        {"id": "authoring", "label": "Plan authoring", "steps": ["a", "b"]},
        {"id": "plan", "label": "Plan phase", "default": "opus / xhigh"},
    ]}})
    assert workflows_module.cmd_get_tuning(path) == 0
    data = json.loads(capsys.readouterr().out)
    g0, g1 = data["groups"]
    assert g0["steps"] == [
        {"id": "a", "model": "opus", "effort": "xhigh"},
        {"id": "b", "model": "inherit", "effort": None},
    ]
    assert g1["steps"] == [] and g1["default"] == "opus / xhigh"


@pytest.mark.parametrize("group,marker", [
    ({"id": "g", "steps": ["nope"]}, "tuning-unknown-step"),
    ({"id": "g", "steps": ["c"]}, "tuning-non-prompt-step"),   # c is bash
    ({"id": "g"}, "tuning-group-empty"),                       # no steps, no default
    ({"id": "Bad_Id", "steps": ["a"]}, "tuning-group-id"),
    # steps and default are exclusive modes — both at once is undefined
    ({"id": "g", "steps": ["a"], "default": "opus / high"},
     "tuning-group-steps-and-default"),
    # a scalar `steps:` would iterate characters — must be a list
    ({"id": "g", "steps": "a"}, "tuning-steps-not-list"),
])
def test_get_tuning_invalid(workflows_module, tmp_path, capsys, group, marker):
    path = _write_def(workflows_module, tmp_path, {"tuning": {"groups": [group]}})
    assert workflows_module.cmd_get_tuning(path) == 2
    assert marker in capsys.readouterr().err


def test_get_tuning_duplicate_group_id(workflows_module, tmp_path, capsys):
    path = _write_def(workflows_module, tmp_path, {"tuning": {"groups": [
        {"id": "g", "steps": ["a"]}, {"id": "g", "steps": ["b"]}]}})
    assert workflows_module.cmd_get_tuning(path) == 2
    assert "duplicate-tuning-group" in capsys.readouterr().err


# ---- get-step-select --------------------------------------------------------

def test_get_step_select_empty_when_absent(workflows_module, tmp_path, capsys):
    path = _write_def(workflows_module, tmp_path, {})
    assert workflows_module.cmd_get_step_select(path) == 0
    assert json.loads(capsys.readouterr().out) == {"optional": [], "presets": []}


def test_get_step_select_full_shape(workflows_module, tmp_path, capsys):
    path = _write_def(workflows_module, tmp_path, {"step-select": {
        "optional": [
            {"id": "a", "label": "Stage A"},                       # id IS a step id → steps defaults
            {"id": "pair", "label": "Both", "steps": ["b", "c"],
             "ask-group": "Flow"},
        ],
        "presets": [
            {"id": "minimal", "label": "Minimal",
             "description": "skip both", "skip": ["a", "pair"]},
        ],
    }})
    assert workflows_module.cmd_get_step_select(path) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["optional"][0]["steps"] == ["a"]
    assert data["optional"][1]["steps"] == ["b", "c"]
    assert data["optional"][1]["ask-group"] == "Flow"
    assert data["presets"][0]["skip"] == ["a", "pair"]


@pytest.mark.parametrize("block,marker", [
    ({"optional": [{"id": "x"}]}, "step-select-no-steps"),
    ({"optional": [{"id": "x", "steps": ["nope"]}]}, "step-select-unknown-step"),
    ({"optional": [{"id": "a"}, {"id": "a"}]}, "duplicate-step-select-id"),
    ({"optional": [{"id": "a"}],
      "presets": [{"id": "p", "skip": ["nope"]}]}, "preset-unknown-optional"),
    # scalar list-fields would iterate characters — must be lists
    ({"optional": [{"id": "x", "steps": "b"}]}, "step-select-steps-not-list"),
    ({"optional": [{"id": "a"}],
      "presets": [{"id": "p", "skip": "a"}]}, "preset-skip-not-list"),
    # duplicate preset ids get their own marker, distinct from bad-slug
    ({"optional": [{"id": "a"}],
      "presets": [{"id": "p", "skip": []}, {"id": "p", "skip": []}]},
     "duplicate-step-select-preset"),
])
def test_get_step_select_invalid(workflows_module, tmp_path, capsys, block, marker):
    path = _write_def(workflows_module, tmp_path, {"step-select": block})
    assert workflows_module.cmd_get_step_select(path) == 2
    assert marker in capsys.readouterr().err


# ---- list-inputs: choice inputs (options / default) -------------------------

def test_list_inputs_options_passthrough(workflows_module, tmp_path, capsys):
    path = _write_def(workflows_module, tmp_path, {"inputs": [
        {"name": "ticket_id", "prompt": "Which ticket?"},
        {"name": "gap_mode", "prompt": "Gap handling?",
         "options": [
             {"value": "defaults", "label": "Proceed on defaults",
              "description": "no pause"},
             "ask",
         ],
         "default": "defaults"},
    ]})
    assert workflows_module.cmd_list_inputs(path) == 0
    data = json.loads(capsys.readouterr().out)
    assert "options" not in data[0]
    gap = data[1]
    assert gap["options"] == [
        {"value": "defaults", "label": "Proceed on defaults",
         "description": "no pause"},
        {"value": "ask"},
    ]
    assert gap["default"] == "defaults"


def test_list_inputs_derives_membership_validate(
        workflows_module, tmp_path, capsys):
    """No explicit validate: → one derived from the option values."""
    path = _write_def(workflows_module, tmp_path, {"inputs": [
        {"name": "mode", "prompt": "?", "options": ["plan-only", "now"]},
    ]})
    assert workflows_module.cmd_list_inputs(path) == 0
    derived = json.loads(capsys.readouterr().out)[0]["validate"]
    assert re.fullmatch(derived, "plan-only")
    assert re.fullmatch(derived, "now")
    assert not re.fullmatch(derived, "later")


def test_list_inputs_explicit_validate_wins(workflows_module, tmp_path, capsys):
    path = _write_def(workflows_module, tmp_path, {"inputs": [
        {"name": "mode", "prompt": "?", "options": ["a", "b"],
         "validate": "^.*$"},
    ]})
    assert workflows_module.cmd_list_inputs(path) == 0
    assert json.loads(capsys.readouterr().out)[0]["validate"] == "^.*$"


def test_list_inputs_default_must_be_an_option(workflows_module, tmp_path, capsys):
    path = _write_def(workflows_module, tmp_path, {"inputs": [
        {"name": "mode", "prompt": "?", "options": ["a", "b"], "default": "c"},
    ]})
    assert workflows_module.cmd_list_inputs(path) == 2
    assert "INVALID:input-default" in capsys.readouterr().err


def test_list_inputs_malformed_option_rejected(workflows_module, tmp_path, capsys):
    path = _write_def(workflows_module, tmp_path, {"inputs": [
        {"name": "mode", "prompt": "?", "options": [{"label": "no value"}]},
    ]})
    assert workflows_module.cmd_list_inputs(path) == 2
    assert "INVALID:input-option" in capsys.readouterr().err


def test_list_inputs_scalar_options_rejected(workflows_module, tmp_path, capsys):
    """`options: fast` must not become options f/a/s/t."""
    path = _write_def(workflows_module, tmp_path, {"inputs": [
        {"name": "mode", "prompt": "?", "options": "fast"},
    ]})
    assert workflows_module.cmd_list_inputs(path) == 2
    assert "INVALID:input-options-not-list" in capsys.readouterr().err


# ---- resolve-team overrides -------------------------------------------------

def _team_def(workflows_module, tmp_path):
    return _write_def(workflows_module, tmp_path, {}, steps=[
        {"id": "solo", "type": "prompt", "prompt": "x",
         "agent": "architect", "model": "opus", "effort": "xhigh",
         "depends_on": []},
        {"id": "panel", "type": "prompt", "prompt": "y",
         "agent": [
             {"role": "architect", "lead": True, "model": "opus"},
             {"role": "qa-engineer"},
         ],
         "model": "opus", "effort": "high", "depends_on": []},
    ])


def test_resolve_team_no_override_uses_step_pin(workflows_module, tmp_path, capsys):
    """No run override → the step's own model/effort pins flow through, with
    the model's policy ceiling applied (opus → Opus 5 → `high`; the ceiling
    table itself is pinned in `test_effort_ceiling.py`)."""
    path = _team_def(workflows_module, tmp_path)
    assert workflows_module.cmd_resolve_team(path, "solo") == 0
    data = json.loads(capsys.readouterr().out)
    m = data["members"][0]
    assert (m["model"], m["effort"]) == ("opus", "high")
    assert "policy ceiling" in m["reason"]
    assert "run tuning override" not in m["reason"]


def test_resolve_team_override_wins_over_step_pin(
        workflows_module, tmp_path, capsys):
    path = _team_def(workflows_module, tmp_path)
    assert workflows_module.cmd_resolve_team(
        path, "solo", "sonnet", "high") == 0
    m = json.loads(capsys.readouterr().out)["members"][0]
    assert (m["model"], m["effort"]) == ("sonnet", "high")
    assert "run tuning override" in m["reason"]


def test_resolve_team_override_wins_over_member_pin(
        workflows_module, tmp_path, capsys):
    path = _team_def(workflows_module, tmp_path)
    assert workflows_module.cmd_resolve_team(
        path, "panel", "sonnet", "high") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "team"
    for m in data["members"]:
        assert (m["model"], m["effort"]) == ("sonnet", "high")
        assert "run tuning override" in m["reason"]


def test_resolve_team_override_still_clamped(workflows_module, tmp_path, capsys):
    """An override goes through the same resolution — haiku drops effort."""
    path = _team_def(workflows_module, tmp_path)
    assert workflows_module.cmd_resolve_team(
        path, "solo", "haiku", "xhigh") == 0
    m = json.loads(capsys.readouterr().out)["members"][0]
    assert m["model"] == "haiku"
    assert m["effort"] is None
    assert "no effort control" in m["reason"]


# ---- get-profiles ----------------------------------------------------------

def _profiles_def(workflows_module, tmp_path, profiles: dict):
    return _write_def(workflows_module, tmp_path, {
        "tuning": {"groups": [
            {"id": "authoring", "label": "Authoring", "steps": ["a", "b"]},
            {"id": "plan", "label": "Plan", "default": "opus / high"},
        ]},
        "step-select": {
            "optional": [{"id": "a", "label": "Step A"}],
            "presets": [{"id": "minimal", "label": "Minimal", "skip": ["a"]}],
        },
        "profiles": profiles,
    })


def test_get_profiles_empty_when_absent(workflows_module, tmp_path, capsys):
    path = _write_def(workflows_module, tmp_path, {})
    assert workflows_module.cmd_get_profiles(path) == 0
    assert json.loads(capsys.readouterr().out) == {"profiles": {}}


def test_get_profiles_full_shape(workflows_module, tmp_path, capsys):
    path = _profiles_def(workflows_module, tmp_path, {
        "low": {
            "tuning": {"authoring": "sonnet / medium", "plan": "default"},
            "step-preset": "minimal",
            "team-mode": "solo",
            "caps": {"max_review_cycles": 2},
        },
        "medium": {},
        "max": {"step-preset": "full", "team-mode": "full"},
    })
    assert workflows_module.cmd_get_profiles(path) == 0
    data = json.loads(capsys.readouterr().out)["profiles"]
    low = data["low"]
    assert low["tuning"]["authoring"] == {
        "model": "sonnet", "effort": "medium", "reason": None}
    assert low["tuning"]["plan"] == "default"
    assert low["step-preset"] == "minimal"
    assert low["team-mode"] == "solo"
    assert low["caps"] == {"max_review_cycles": 2}
    assert data["medium"] == {"tuning": {}, "skip": [], "caps": {}}
    assert data["max"]["step-preset"] == "full"


def test_get_profiles_model_only_tuning_value(workflows_module, tmp_path, capsys):
    path = _profiles_def(workflows_module, tmp_path, {
        "low": {"tuning": {"authoring": "sonnet"}},
    })
    assert workflows_module.cmd_get_profiles(path) == 0
    data = json.loads(capsys.readouterr().out)["profiles"]
    assert data["low"]["tuning"]["authoring"]["model"] == "sonnet"
    assert data["low"]["tuning"]["authoring"]["effort"] is None


@pytest.mark.parametrize("profiles, marker", [
    ({"turbo": {}}, "INVALID:profile-level:turbo"),
    ({"low": []}, "INVALID:profile-entry:expected-mapping:low"),
    ({"low": {"tuning": {"nope": "sonnet"}}},
     "INVALID:profile-tuning-unknown-group:low:nope"),
    ({"low": {"tuning": {"authoring": "sonnet / high / extra"}}},
     "INVALID:profile-tuning-bad-value:low:authoring"),
    ({"low": {"step-preset": "nope"}},
     "INVALID:profile-step-preset-unknown:low:nope"),
    ({"low": {"step-preset": "minimal", "skip": ["a"]}},
     "INVALID:profile-step-preset-and-skip:low"),
    ({"low": {"skip": ["nope"]}},
     "INVALID:profile-skip-unknown-optional:low:nope"),
    ({"low": {"team-mode": "duo"}},
     "INVALID:profile-team-mode:low:duo"),
    ({"low": {"caps": {"Bad-Name": 3}}},
     "INVALID:profile-cap-name:low:Bad-Name"),
    ({"low": {"caps": {"max_fix_attempts": 0}}},
     "INVALID:profile-cap-not-positive-int:low:max_fix_attempts"),
    ({"low": {"caps": {"max_fix_attempts": "three"}}},
     "INVALID:profile-cap-not-positive-int:low:max_fix_attempts"),
])
def test_get_profiles_invalid(workflows_module, tmp_path, capsys, profiles, marker):
    path = _profiles_def(workflows_module, tmp_path, profiles)
    assert workflows_module.cmd_get_profiles(path) == 2
    assert marker in capsys.readouterr().err


def test_get_profiles_block_not_mapping(workflows_module, tmp_path, capsys):
    path = _write_def(workflows_module, tmp_path, {"profiles": ["low"]})
    assert workflows_module.cmd_get_profiles(path) == 2
    assert "INVALID:profiles-block:expected-mapping" in capsys.readouterr().err


# ---- resolve-team --team-mode ----------------------------------------------

def test_resolve_team_solo_collapses_to_lead(workflows_module, tmp_path, capsys):
    path = _team_def(workflows_module, tmp_path)
    assert workflows_module.cmd_resolve_team(
        path, "panel", team_mode="solo") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "single"
    assert len(data["members"]) == 1
    assert data["members"][0]["role"] == "architect"
    assert data["collapsed"] == {"from": 2, "dropped": ["qa-engineer"]}
    assert "team collapsed to lead (solo mode)" in data["members"][0]["reason"]


def test_resolve_team_solo_no_lead_keeps_first_member(
        workflows_module, tmp_path, capsys):
    path = _write_def(workflows_module, tmp_path, {}, steps=[
        {"id": "panel", "type": "prompt", "prompt": "y",
         "agent": ["architect", "qa-engineer", "product-manager"],
         "model": "opus", "effort": "high", "depends_on": []},
    ])
    assert workflows_module.cmd_resolve_team(
        path, "panel", team_mode="solo") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "single"
    assert data["members"][0]["role"] == "architect"
    assert data["collapsed"]["from"] == 3
    assert data["collapsed"]["dropped"] == ["qa-engineer", "product-manager"]
    assert "no declared lead" in data["members"][0]["reason"]


def test_resolve_team_solo_noop_on_single(workflows_module, tmp_path, capsys):
    path = _team_def(workflows_module, tmp_path)
    assert workflows_module.cmd_resolve_team(
        path, "solo", team_mode="solo") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "single"
    assert "collapsed" not in data


def test_resolve_team_solo_composes_with_override(
        workflows_module, tmp_path, capsys):
    path = _team_def(workflows_module, tmp_path)
    assert workflows_module.cmd_resolve_team(
        path, "panel", "sonnet", "high", team_mode="solo") == 0
    data = json.loads(capsys.readouterr().out)
    m = data["members"][0]
    assert (m["model"], m["effort"]) == ("sonnet", "high")
    assert "run tuning override" in m["reason"]
    assert data["collapsed"]["from"] == 2


def test_resolve_team_full_mode_shape_unchanged(
        workflows_module, tmp_path, capsys):
    """Additive-shape guard: full mode must not grow a `collapsed` key."""
    path = _team_def(workflows_module, tmp_path)
    assert workflows_module.cmd_resolve_team(path, "panel") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "team"
    assert "collapsed" not in data
    assert set(data) == {"mode", "lead", "members", "errors"}
