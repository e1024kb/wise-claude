import pytest

from wise_engine.constants import EFFORTS, HARNESSES
from wise_engine.resolve import (
    LOW_PROFILE_OPUS_MODEL,
    MODEL_EFFORT_SUPPORT,
    RETIRED_MODELS,
    ResolveError,
    cap_effort,
    cmd_list_agents,
    cmd_resolve_model,
    cmd_resolve_team,
    downmap_effort,
    effort_ceiling,
    effort_ceilings,
    effort_for,
    is_snapshot_of,
    model_family,
    normalize_member,
    parse_frontmatter,
    resolve_model_dict,
    resolve_team,
    roster_agents,
    roster_names,
)


def resolve(model, effort="", profile="", **opts):
    return resolve_model_dict(model, effort, profile, {"env": {}, **opts})


@pytest.mark.parametrize(
    "model,effort,expected",
    [
        ("opus", "xhigh", "high"),
        ("opus", "max", "high"),
        ("opus", "high", "high"),
        ("opus", "low", "low"),
        ("claude-opus-5", "xhigh", "high"),
        ("claude-opus-5-20260401", "xhigh", "high"),
        ("claude-opus-50-20270101", "xhigh", "xhigh"),
        ("claude-opus-5-1", "xhigh", "xhigh"),
        ("claude-opus-5-1-20270101", "xhigh", "xhigh"),
        ("claude-opus-5-2026040", "xhigh", "xhigh"),
        ("claude-opus-4-8", "xhigh", "xhigh"),
        ("claude-opus-4-8", "max", "xhigh"),
        ("claude-opus-4-7", "xhigh", "xhigh"),
        ("sonnet", "xhigh", "xhigh"),
        ("fable", "max", "max"),
        ("inherit", "xhigh", "xhigh"),
        ("opus", "bogus", "bogus"),
        ("opus", "", ""),
        ("haiku", "xhigh", ""),
    ],
)
def test_ceiling_table(model, effort, expected):
    assert resolve(model, effort)["effort"] == expected


def test_clamp_reasons_and_snapshot_rules():
    assert is_snapshot_of("claude-opus-5-20260401", "claude-opus-5")
    for value in ("claude-opus-50-20270101", "claude-opus-5-1", "claude-opus-5"):
        assert not is_snapshot_of(value, "claude-opus-5")
    assert "reason" not in resolve("opus", "high")
    assert "policy ceiling" in resolve("opus", "xhigh")["reason"]
    assert "no effort control" in resolve("haiku", "xhigh")["reason"]
    cap = resolve(
        "sonnet",
        "xhigh",
        effort_support={**MODEL_EFFORT_SUPPORT, "sonnet": {"low", "medium", "high"}},
    )
    assert cap["effort"] == "high" and "capability ceiling" in cap["reason"]
    assert downmap_effort("other", " HIGH ", {}) == ("high", False)
    assert downmap_effort("sonnet", "low", {"sonnet": {"high"}}) == ("", True)
    assert cap_effort("opus", " LOW ", {}) == (" LOW ", False)


@pytest.mark.parametrize(
    "setting,model,expected",
    [
        ("off", "opus", "max"),
        ("opus=xhigh", "opus", "xhigh"),
        ("opus=xhigh", "claude-opus-5", "high"),
        ("claude-opus-5=medium", "claude-opus-5", "medium"),
        ("opus=off", "opus", "max"),
        ("opus=none", "opus", "max"),
        ("opus=", "opus", "max"),
        ("opus=off", "claude-opus-5", "high"),
        ("sonnet=medium", "sonnet", "medium"),
        ("junk", "opus", "high"),
        ("opus=turbo", "opus", "high"),
        ("=low", "opus", "high"),
    ],
)
def test_env_override(setting, model, expected):
    assert resolve(model, "max", env={"WISE_EFFORT_CEILING": setting})["effort"] == expected


def test_env_table_fresh_and_longest_snapshot():
    table = effort_ceilings({})
    table.clear()
    assert effort_ceilings({})["opus"] == "high"
    assert (
        effort_ceiling("claude-opus-5-1-20260401", {"WISE_EFFORT_CEILING": "claude-opus-5-1=low"})
        == "low"
    )
    assert resolve("opus", "max", env={"WISE_EFFORT_CEILING": "off"})["effort"] == "max"
    assert resolve("opus", "max")["effort"] == "high"


@pytest.mark.parametrize(
    "pin",
    [
        "opus",
        "claude-opus-5",
        "claude-opus-5-20260401",
        "claude-opus-4-7",
        "claude-opus-4-20250514",
    ],
)
def test_low_profile_swaps_opus(pin):
    result = resolve(pin, "high", "LOW")
    assert result["model"] == LOW_PROFILE_OPUS_MODEL == "claude-opus-4-8"
    assert result["effort"] == "high" and result["next_fallback"] == "sonnet"
    assert "low profile:" in result["reason"]


@pytest.mark.parametrize(
    "pin",
    ["claude-opus-4-8", "claude-opus-4-8-20260401", "sonnet", "fable", "gpt-5", "inherit", ""],
)
def test_low_profile_leaves_other_pins(pin):
    result = resolve(pin, "high", "low")
    assert result["model"] == (pin or "inherit")
    assert "reason" not in result


def test_profile_resolution_and_retired_substitution():
    for profile in ("medium", "max", ""):
        result = resolve("opus", "max", profile)
        assert result["model"] == "opus" and result["effort"] == "high"
    assert resolve("opus", "xhigh", "low")["effort"] == "xhigh"
    assert resolve("opus", "max", "low")["effort"] == "xhigh"
    for pin, (replacement, state) in RETIRED_MODELS.items():
        result = resolve(pin, "max")
        assert result["model"] == replacement and result["fell_back"]
        assert state in result["reason"]
    combined = resolve("claude-opus-4-20250514", "high", "low")
    assert "deprecated" in combined["reason"] and "low profile:" in combined["reason"]
    assert cmd_resolve_model("opus", "xhigh", "low", {"env": {}})["model"] == LOW_PROFILE_OPUS_MODEL
    with pytest.raises(ResolveError) as exc:
        cmd_resolve_model("opus", profile="turbo")
    assert exc.value.exit_code == 2 and str(exc.value) == "INVALID:profile-level:turbo"


def panel():
    return {
        "agent": [{"role": "architect", "lead": True, "model": "opus"}, {"role": "qa-engineer"}],
        "model": "opus",
        "effort": "xhigh",
    }


def test_team_tuning_and_profiles():
    for overrides, expected in [
        ({}, ("opus", "high")),
        ({"model_override": "sonnet", "effort_override": "high"}, ("sonnet", "high")),
        ({"model_override": "haiku"}, ("haiku", "")),
        ({"profile": "low"}, (LOW_PROFILE_OPUS_MODEL, "xhigh")),
    ]:
        result = resolve_team(panel(), {"env": {}, **overrides})
        assert result["mode"] == "team" and not result["errors"]
        for member in result["members"]:
            assert (member["model"], member["effort"]) == expected
            assert ("run tuning override" in member.get("reason", "")) == (
                "model_override" in overrides
            )
    step = panel()
    step["agent"][1]["model"] = "sonnet"
    assert resolve_team(step, {"profile": "low"})["members"][1]["model"] == "sonnet"
    result = resolve_team(panel(), {"env": {}, "profile": "turbo"})
    assert "--profile" in result["errors"][0] and result["members"][0]["model"] == "opus"


def test_solo_collapse_and_errors():
    result = resolve_team(
        panel(), {"env": {}, "team_mode": "solo", "model_override": "opus", "profile": "low"}
    )
    assert result["mode"] == "single" and result["lead"] == "architect"
    assert result["collapsed"] == {"from": 2, "dropped": ["qa-engineer"]}
    assert all(
        text in result["members"][0]["reason"]
        for text in ("run tuning override", "low profile:", "solo mode")
    )
    step = {"agent": ["architect", "qa-engineer", "product-manager"]}
    result = resolve_team(step, {"team_mode": "solo"})
    assert result["lead"] is None and result["collapsed"]["dropped"] == [
        "qa-engineer",
        "product-manager",
    ]
    assert "no declared lead" in result["members"][0]["reason"]
    result = resolve_team({"agent": ["architect", "not-a-role"]}, {"team_mode": "solo"})
    assert "not-a-role" in result["errors"][0]
    assert result["collapsed"]["dropped"] == ["not-a-role"]
    for agent, mode in [("architect", "single"), ("auto", "auto"), (None, "unset"), (False, "off")]:
        result = resolve_team({"agent": agent}, {"team_mode": "solo"})
        assert result["mode"] == mode and "collapsed" not in result
    result = resolve_team(panel(), {"team_mode": "wrong"})
    assert result["mode"] == "team" and "collapsed" not in result
    assert "--team-mode" in result["errors"][0]
    assert set(resolve_team(panel())) == {"mode", "lead", "members", "errors"}


@pytest.mark.parametrize(
    "agent,error",
    [
        (True, "not valid"),
        (42, "unexpected type number"),
        ({}, "unexpected type object"),
        ([{}], "missing a role"),
        (["auto"], "policy keyword"),
        (
            [{"role": "architect", "lead": True}, {"role": "qa-engineer", "lead": True}],
            "multiple leads",
        ),
    ],
)
def test_team_authoring_errors(agent, error):
    assert any(error in message for message in resolve_team({"agent": agent})["errors"])


def test_roster_frontmatter_and_file_entry(tmp_path):
    roster = roster_agents()
    assert len(roster) >= 13 and cmd_list_agents() == roster
    architect = next(item for item in roster if item["name"] == "architect")
    assert "Write" in architect["tools"] and architect["model"] == "inherit"
    assert architect["effort"] == "high" and architect["description"]
    assert "qa-engineer" in roster_names()
    assert roster_agents(tmp_path / "missing") == []
    assert parse_frontmatter(tmp_path / "missing") == {}
    (tmp_path / "plain.md").write_text("plain")
    (tmp_path / "custom.md").write_text(
        "---\nname: person\ntools: [Read, Write]\neffort: high\n---\n"
    )
    assert parse_frontmatter(tmp_path / "plain.md") == {}
    assert [item["name"] for item in roster_agents(tmp_path)] == ["person", "plain"]
    assert (
        resolve_team({"agent": ["unknown"]}, {"roster_dir": tmp_path / "missing"})["errors"] == []
    )
    definition = tmp_path / "workflow.yaml"
    definition.write_text(
        "steps:\n  - id: solo\n    agent: architect\n    model: opus\n    effort: max\n"
    )
    assert cmd_resolve_team(definition, "solo", {"env": {}})["members"][0]["effort"] == "high"
    assert cmd_resolve_team(definition, "absent")["mode"] == "unset"


def test_harness_family_and_member_truthiness():
    for harness in HARNESSES:
        assert resolve("opus", harness=harness)["harness"] == harness
        for effort in EFFORTS:
            assert effort_for(harness, effort) == (
                None if harness in ("cursor", "gemini") else effort
            )
    for pin, family in [
        ("", "inherit"),
        ("inherit", "inherit"),
        ("Opus", "opus"),
        ("claude-sonnet-4-6", "sonnet"),
        ("haiku-latest", "haiku"),
        ("gpt-5", ""),
    ]:
        assert model_family(pin) == family
    assert normalize_member({"role": " architect ", "lead": []})["lead"] is True
    assert normalize_member({"role": False})["role"] == ""


def test_yaml_core_frontmatter(tmp_path):
    file = tmp_path / "agent.md"
    file.write_text("---\nname: on\neffort: 2026-09-11\ntools: [off, true, 1.0]\n---\n")
    assert parse_frontmatter(file) == {
        "name": "on",
        "effort": "2026-09-11",
        "tools": ["off", True, 1.0],
    }
    assert roster_agents(tmp_path)[0]["tools"] == ["off", "true", "1"]
    file.write_text("---\n[broken\n---\n")
    assert parse_frontmatter(file) == {}
