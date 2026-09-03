"""Pins the low-profile Opus rule (plugins/wise/scripts/workflows.py):

    LOW_PROFILE_OPUS_MODEL / _low_profile_model / _resolve_model_dict(profile=)
    cmd_resolve_model --profile / cmd_resolve_team --profile / cmd_get_profiles

Contract under test — a `low` run NEVER dispatches Opus 5:
- under profile `low`, every Opus-family pin (the `opus` alias, a
  `claude-opus-5*` id, a retired id that substitutes to `opus`) resolves
  to `claude-opus-4-8`, with the swap surfaced in `reason`;
- a pin already on Opus 4.8 (or a dated snapshot of it), and every
  non-Opus family (`sonnet`, `haiku`, `fable`, `inherit`) are untouched;
- `medium` / `max` / no profile leave the pre-rule behaviour intact;
- the effort clamp runs on the SUBSTITUTED model (Opus 4.8 keeps `xhigh`);
- `get-profiles` applies the rule to the `low` level's tuning values and
  the bundled `low` profiles resolve to Opus 4.8 wherever they pin Opus;
- `resolve-team --profile low` applies it to members and overrides alike,
  and an unknown `--profile` is an authoring error, not a silent default.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
BUNDLED = REPO / "plugins" / "wise" / "workflows"


def test_constant_is_opus_4_8(workflows_module):
    assert workflows_module.LOW_PROFILE_OPUS_MODEL == "claude-opus-4-8"


@pytest.mark.parametrize("pinned", [
    "opus",
    "claude-opus-5",
    "claude-opus-5-20260401",
    "claude-opus-5-1",
    "claude-opus-4-1-20250805",   # retired → opus → rule applies
])
def test_low_profile_swaps_every_opus_pin(workflows_module, pinned):
    out = workflows_module._resolve_model_dict(pinned, "high", "low")
    assert out["model"] == "claude-opus-4-8"
    assert out["effort"] == "high"
    assert "low profile:" in out["reason"]
    assert "claude-opus-4-8" in out["reason"]
    assert out["next_fallback"] == "sonnet"


@pytest.mark.parametrize("pinned", [
    "claude-opus-4-8",
    "claude-opus-4-8-20260101",   # dated snapshot of 4.8
])
def test_low_profile_leaves_opus_4_8_alone(workflows_module, pinned):
    out = workflows_module._resolve_model_dict(pinned, "high", "low")
    assert out["model"] == pinned
    assert out["reason"] is None


@pytest.mark.parametrize("pinned, effort", [
    ("sonnet", "high"),
    ("haiku", ""),
    ("fable", "high"),
    ("inherit", "high"),
    ("", "high"),
])
def test_low_profile_ignores_non_opus_families(workflows_module, pinned, effort):
    out = workflows_module._resolve_model_dict(pinned, effort, "low")
    assert out["model"] == (pinned or "inherit")
    assert not (out["reason"] or "").startswith("low profile")


@pytest.mark.parametrize("profile", ["", "medium", "max", "MEDIUM"])
def test_other_profiles_keep_opus_5(workflows_module, profile):
    out = workflows_module._resolve_model_dict("opus", "xhigh", profile)
    assert out["model"] == "opus"
    assert out["effort"] == "high"          # Opus 5 policy ceiling still applies
    assert "low profile" not in out["reason"]


def test_low_profile_effort_clamps_on_substituted_model(workflows_module):
    """Opus 4.8's ceiling is `xhigh`, so an authored xhigh survives the swap
    (it would have stepped down to `high` on Opus 5)."""
    out = workflows_module._resolve_model_dict("opus", "xhigh", "low")
    assert (out["model"], out["effort"]) == ("claude-opus-4-8", "xhigh")
    assert "policy ceiling" not in out["reason"]
    out = workflows_module._resolve_model_dict("opus", "max", "low")
    assert (out["model"], out["effort"]) == ("claude-opus-4-8", "xhigh")


def test_low_profile_case_insensitive(workflows_module):
    out = workflows_module._resolve_model_dict("opus", "high", "LOW")
    assert out["model"] == "claude-opus-4-8"


def test_retired_id_reason_composes_with_rule(workflows_module):
    out = workflows_module._resolve_model_dict(
        "claude-opus-4-1-20250805", "high", "low")
    assert out["fell_back"] is True
    assert "deprecated" in out["reason"]
    assert "low profile:" in out["reason"]


# ---- resolve-model CLI -------------------------------------------------------

def test_cmd_resolve_model_profile_flag(workflows_module, capsys):
    assert workflows_module.cmd_resolve_model("opus", "high", "low") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["model"] == "claude-opus-4-8"


def test_cmd_resolve_model_rejects_unknown_profile(workflows_module, capsys):
    assert workflows_module.cmd_resolve_model("opus", "high", "turbo") == 2
    assert "INVALID:profile-level:turbo" in capsys.readouterr().err


# ---- resolve-team --profile -------------------------------------------------

def _team_def(workflows_module, tmp_path):
    doc = {
        "version": 1, "name": "t", "description": "d",
        "steps": [
            {"id": "solo", "type": "prompt", "prompt": "x",
             "agent": "architect", "model": "opus", "effort": "xhigh",
             "depends_on": []},
            {"id": "panel", "type": "prompt", "prompt": "y",
             "agent": [
                 {"role": "architect", "lead": True, "model": "opus"},
                 {"role": "qa-engineer", "model": "sonnet"},
             ],
             "model": "opus", "effort": "high", "depends_on": []},
        ],
    }
    p = tmp_path / "workflow.yaml"
    workflows_module.save_yaml(p, doc)
    return str(p)


def test_resolve_team_low_swaps_step_pin(workflows_module, tmp_path, capsys):
    path = _team_def(workflows_module, tmp_path)
    assert workflows_module.cmd_resolve_team(
        path, "solo", "", "", "full", "low") == 0
    m = json.loads(capsys.readouterr().out)["members"][0]
    assert (m["model"], m["effort"]) == ("claude-opus-4-8", "xhigh")
    assert "low profile:" in m["reason"]


def test_resolve_team_low_swaps_only_opus_members(workflows_module, tmp_path, capsys):
    path = _team_def(workflows_module, tmp_path)
    assert workflows_module.cmd_resolve_team(
        path, "panel", "", "", "full", "low") == 0
    data = json.loads(capsys.readouterr().out)
    by_role = {m["role"]: m for m in data["members"]}
    assert by_role["architect"]["model"] == "claude-opus-4-8"
    assert by_role["qa-engineer"]["model"] == "sonnet"
    assert by_role["qa-engineer"]["reason"] is None


def test_resolve_team_low_swaps_override_too(workflows_module, tmp_path, capsys):
    """A run tuning override naming opus still lands on Opus 4.8 under low."""
    path = _team_def(workflows_module, tmp_path)
    assert workflows_module.cmd_resolve_team(
        path, "panel", "opus", "high", "solo", "low") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "single"
    m = data["members"][0]
    assert m["model"] == "claude-opus-4-8"
    assert "run tuning override" in m["reason"]
    assert "low profile:" in m["reason"]
    assert "solo mode" in m["reason"]


def test_resolve_team_medium_keeps_opus_5(workflows_module, tmp_path, capsys):
    path = _team_def(workflows_module, tmp_path)
    assert workflows_module.cmd_resolve_team(
        path, "solo", "", "", "full", "medium") == 0
    m = json.loads(capsys.readouterr().out)["members"][0]
    assert (m["model"], m["effort"]) == ("opus", "high")


def test_resolve_team_unknown_profile_is_an_error(workflows_module, tmp_path, capsys):
    path = _team_def(workflows_module, tmp_path)
    assert workflows_module.cmd_resolve_team(
        path, "solo", "", "", "full", "turbo") == 0
    data = json.loads(capsys.readouterr().out)
    assert any("--profile" in e for e in data["errors"])
    assert data["members"][0]["model"] == "opus"   # rule NOT applied on junk


# ---- get-profiles ------------------------------------------------------------

def _profiles_def(workflows_module, tmp_path, profiles):
    doc = {
        "version": 1, "name": "t", "description": "d",
        "steps": [{"id": "a", "type": "prompt", "prompt": "x", "depends_on": []}],
        "tuning": {"groups": [
            {"id": "plan", "label": "Plan", "default": "opus / high"},
            {"id": "impl", "label": "Impl", "default": "opus / high"},
        ]},
        "profiles": profiles,
    }
    p = tmp_path / "workflow.yaml"
    workflows_module.save_yaml(p, doc)
    return str(p)


def test_get_profiles_low_applies_rule(workflows_module, tmp_path, capsys):
    path = _profiles_def(workflows_module, tmp_path, {
        "low": {"tuning": {"plan": "opus / high", "impl": "sonnet"}},
        "medium": {},
        "max": {"tuning": {"plan": "opus / high"}},
    })
    assert workflows_module.cmd_get_profiles(path) == 0
    data = json.loads(capsys.readouterr().out)["profiles"]
    assert data["low"]["tuning"]["plan"]["model"] == "claude-opus-4-8"
    assert data["low"]["tuning"]["plan"]["effort"] == "high"
    assert "low profile:" in data["low"]["tuning"]["plan"]["reason"]
    assert data["low"]["tuning"]["impl"]["model"] == "sonnet"
    assert data["max"]["tuning"]["plan"]["model"] == "opus"
    assert data["max"]["tuning"]["plan"]["reason"] is None


def test_get_profiles_low_explicit_4_8_is_a_noop(workflows_module, tmp_path, capsys):
    path = _profiles_def(workflows_module, tmp_path, {
        "low": {"tuning": {"plan": "claude-opus-4-8 / high"}},
    })
    assert workflows_module.cmd_get_profiles(path) == 0
    data = json.loads(capsys.readouterr().out)["profiles"]
    assert data["low"]["tuning"]["plan"] == {
        "model": "claude-opus-4-8", "effort": "high", "reason": None}


@pytest.mark.parametrize("name", ["ticket-auto", "ticket-plan"])
def test_bundled_low_profiles_never_resolve_to_opus_5(workflows_module, name, capsys):
    """Every bundled `low` tuning value that pins Opus lands on Opus 4.8."""
    path = BUNDLED / name / "workflow.yaml"
    assert workflows_module.cmd_get_profiles(str(path)) == 0
    data = json.loads(capsys.readouterr().out)["profiles"]
    low = data["low"]["tuning"]
    assert low, f"{name}: low profile declares no tuning"
    opus_entries = [
        v for v in low.values()
        if isinstance(v, dict) and workflows_module._model_family(v["model"]) == "opus"
    ]
    assert opus_entries, f"{name}: expected at least one Opus-tier low entry"
    for v in opus_entries:
        assert v["model"] == "claude-opus-4-8", v
