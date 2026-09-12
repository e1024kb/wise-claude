import pytest

from wise_engine.constants import EFFORTS, HARNESSES
from wise_engine.models import catalog_for, catalog_model, default_effort, default_model


@pytest.mark.parametrize("harness", HARNESSES)
def test_catalog_defaults_and_lookup(harness):
    catalog = catalog_for(harness)
    assert default_model(harness) == catalog[0]
    assert default_model(harness, "unknown") == catalog[0]
    assert catalog_model(harness, None) is None
    for model in catalog:
        assert catalog_model(harness, " " + model["id"].upper() + " ") == model
        assert default_model(harness, model["id"]) == model
        for wanted in (*EFFORTS, "", "bogus"):
            result = default_effort(model, wanted)
            if not model["efforts"]:
                assert result is None
            elif wanted in model["efforts"]:
                assert result == wanted
            else:
                assert result in model["efforts"]


def test_alias_and_effort_boundaries():
    assert catalog_model("claude", " Opus ")["id"] == "claude-opus-5"
    assert catalog_model("codex", "opus") is None
    assert default_effort({"efforts": ["medium", "xhigh"]}, "high") == "medium"
    assert default_effort({"efforts": ["medium", "xhigh"]}, "low") == "medium"
    assert default_effort({"efforts": ["medium", "xhigh"]}, "bogus") == "medium"
    assert [entry["id"] for entry in catalog_for("cursor")] == [
        "cursor-grok-4.6-high",
        "composer-2.5",
    ]
    assert catalog_model("cursor", "grok-4.6") is None
    assert catalog_model("grok", "grok-4.6")["id"] == "grok-4.6"
