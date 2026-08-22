"""Pins the per-model policy effort ceiling (`MODEL_EFFORT_CEILING`).

Capability clamping (`MODEL_EFFORT_SUPPORT`) answers "what does this model
accept"; the ceiling answers "what is worth asking it for". They are keyed
differently on purpose — capability by family, policy per model version —
because Opus 5 gets its signal at `high` where Opus 4.8 still needs `xhigh`.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    """Every test starts from the shipped table, not the developer's env."""
    monkeypatch.delenv("WISE_EFFORT_CEILING", raising=False)


# ---- shipped table ---------------------------------------------------------

@pytest.mark.parametrize("model,effort,expected", [
    ("opus", "xhigh", "high"),              # alias → latest Opus → Opus 5
    ("opus", "max", "high"),
    ("opus", "high", "high"),               # at the ceiling → untouched
    ("opus", "low", "low"),
    ("claude-opus-5", "xhigh", "high"),
    ("claude-opus-5-20260401", "xhigh", "high"),   # dated snapshot inherits
    ("claude-opus-50-20270101", "xhigh", "xhigh"),  # near-miss id, NOT a snapshot
    ("claude-opus-5-1", "xhigh", "xhigh"),          # version bump, ceiling is its own call
    ("claude-opus-5-1-20270101", "xhigh", "xhigh"),  # snapshot of the version bump
    ("claude-opus-5-2026040", "xhigh", "xhigh"),    # malformed date, no inherit
    ("claude-opus-4-8", "xhigh", "xhigh"),  # 4.8 keeps xhigh
    ("claude-opus-4-8", "max", "xhigh"),    # …but not max
    ("claude-opus-4-7", "xhigh", "xhigh"),  # untabled model → no ceiling
    ("sonnet", "xhigh", "xhigh"),
    ("fable", "max", "max"),
    ("inherit", "xhigh", "xhigh"),          # unknown session model → hands off
])
def test_ceiling_table(workflows_module, model, effort, expected):
    assert workflows_module._resolve_model_dict(model, effort)["effort"] == expected


def test_snapshot_match_requires_a_date_suffix(workflows_module):
    """The suffix rule is what keeps a neighbouring model id out."""
    assert workflows_module._is_snapshot_of("claude-opus-5-20260401", "claude-opus-5")
    assert not workflows_module._is_snapshot_of("claude-opus-50-20270101", "claude-opus-5")
    assert not workflows_module._is_snapshot_of("claude-opus-5-1", "claude-opus-5")
    assert not workflows_module._is_snapshot_of("claude-opus-5", "claude-opus-5")


def test_ceiling_reason_is_surfaced(workflows_module):
    reason = workflows_module._resolve_model_dict("opus", "xhigh")["reason"]
    assert "xhigh" in reason and "high" in reason and "policy ceiling" in reason


def test_at_ceiling_has_no_reason(workflows_module):
    assert workflows_module._resolve_model_dict("opus", "high")["reason"] is None


def test_non_standard_effort_untouched(workflows_module):
    """A value outside EFFORT_ORDER is passed through, not forced to a level."""
    assert workflows_module._resolve_model_dict("opus", "bogus")["effort"] == "bogus"


def test_no_effort_no_ceiling(workflows_module):
    assert workflows_module._resolve_model_dict("opus", "")["effort"] is None


def test_haiku_still_drops_effort(workflows_module):
    """Capability clamping runs first; the ceiling never resurrects an effort."""
    out = workflows_module._resolve_model_dict("haiku", "xhigh")
    assert out["effort"] is None and "no effort control" in out["reason"]


def test_ceiling_applies_after_retired_substitution(workflows_module):
    """A deprecated id becomes `opus`, so it inherits Opus 5's ceiling."""
    out = workflows_module._resolve_model_dict("claude-opus-4-1-20250805", "xhigh")
    assert (out["model"], out["effort"]) == ("opus", "high")
    assert "deprecated" in out["reason"] and "policy ceiling" in out["reason"]


# ---- WISE_EFFORT_CEILING override ------------------------------------------

def test_env_off_disables_every_ceiling(workflows_module, monkeypatch):
    monkeypatch.setenv("WISE_EFFORT_CEILING", "off")
    assert workflows_module._resolve_model_dict("opus", "max")["effort"] == "max"


def test_env_raises_one_model(workflows_module, monkeypatch):
    monkeypatch.setenv("WISE_EFFORT_CEILING", "opus=xhigh")
    assert workflows_module._resolve_model_dict("opus", "max")["effort"] == "xhigh"
    # other entries survive the merge
    assert workflows_module._resolve_model_dict(
        "claude-opus-5", "xhigh")["effort"] == "high"


def test_env_lowers_one_model(workflows_module, monkeypatch):
    monkeypatch.setenv("WISE_EFFORT_CEILING", "claude-opus-5=medium")
    assert workflows_module._resolve_model_dict(
        "claude-opus-5", "xhigh")["effort"] == "medium"


def test_env_drops_one_entry(workflows_module, monkeypatch):
    monkeypatch.setenv("WISE_EFFORT_CEILING", "opus=off")
    assert workflows_module._resolve_model_dict("opus", "max")["effort"] == "max"
    assert workflows_module._resolve_model_dict(
        "claude-opus-5", "xhigh")["effort"] == "high"


@pytest.mark.parametrize("value", ["junk", "opus", "=high", "opus=nonsense", " , "])
def test_env_junk_is_ignored(workflows_module, monkeypatch, value):
    """A typo must not kill a run — the shipped table stays in force."""
    monkeypatch.setenv("WISE_EFFORT_CEILING", value)
    assert workflows_module._resolve_model_dict("opus", "xhigh")["effort"] == "high"


def test_env_adds_an_untabled_model(workflows_module, monkeypatch):
    monkeypatch.setenv("WISE_EFFORT_CEILING", "sonnet=medium")
    assert workflows_module._resolve_model_dict("sonnet", "xhigh")["effort"] == "medium"


# ---- reaches the dispatch surfaces -----------------------------------------

def test_resolve_model_cli_emits_capped_effort(workflows_module, capsys):
    assert workflows_module.cmd_resolve_model("opus", "xhigh") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["effort"] == "high" and "policy ceiling" in out["reason"]
