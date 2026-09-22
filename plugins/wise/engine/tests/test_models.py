import asyncio

import pytest

from wise_engine.constants import EFFORTS, HARNESSES
from wise_engine.models import (
    catalog_for,
    catalog_model,
    default_effort,
    default_model,
    discover_models,
    merged_catalog,
    parse_model_listing,
)


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
    assert catalog_model("claude", " Opus ")["id"] == "claude-opus-5-5"
    assert catalog_model("codex", "opus") is None
    assert catalog_model("claude", "claude-opus-5")["label"] == "Opus 5"
    assert "claude-opus-5" not in [entry["id"] for entry in merged_catalog("claude")]
    assert default_effort({"efforts": ["medium", "xhigh"]}, "high") == "medium"
    assert default_effort({"efforts": ["medium", "xhigh"]}, "low") == "medium"
    assert default_effort({"efforts": ["medium", "xhigh"]}, "bogus") == "medium"
    assert [entry["id"] for entry in catalog_for("cursor")] == [
        "grok-4.7-high-fast",
        "grok-4.7-high",
        "cursor-grok-4.6-high-fast",
        "cursor-grok-4.6-high",
    ]
    assert catalog_model("cursor", "grok-4.6") is None
    assert catalog_model("grok", "grok-4.6")["id"] == "grok-4.6"
    assert [entry["id"] for entry in catalog_for("claude")] == [
        "claude-fable-5-1",
        "claude-opus-5-5",
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "claude-fable-5",
    ]


CURSOR_LISTING = """Available models

auto - Auto (default)
gpt-5.3-codex-low - Codex 5.3 Low
cursor-grok-4.6-high - Cursor Grok 4.6
claude-opus-5-5-thinking-high - Claude Opus 5.5 1M Thinking
not a model line
bad id! - Broken
"""

GROK_LISTING = """You are logged in with grok.com.

Default model: grok-4.6

Available models:
  * grok-4.6 (default)
  - grok-4.5
  - bad id!
"""


def test_parse_model_listings():
    cursor = parse_model_listing("cursor", CURSOR_LISTING)
    assert [(m["id"], m["label"]) for m in cursor] == [
        ("auto", "Auto (default)"),
        ("gpt-5.3-codex-low", "Codex 5.3 Low"),
        ("cursor-grok-4.6-high", "Cursor Grok 4.6"),
        ("claude-opus-5-5-thinking-high", "Claude Opus 5.5 1M Thinking"),
    ]
    assert all(m["efforts"] == [] and "cursor" in m["description"] for m in cursor)
    grok = parse_model_listing("grok", GROK_LISTING)
    assert [(m["id"], m["label"]) for m in grok] == [
        ("grok-4.6", "grok-4.6"),
        ("grok-4.5", "grok-4.5"),
    ]
    assert parse_model_listing("grok", "Default model: grok-4.6\n") == []
    assert parse_model_listing("claude", CURSOR_LISTING) == []
    assert parse_model_listing("codex", GROK_LISTING) == []


def test_merged_catalog_keeps_catalog_first_then_reported_unique_additions():
    discovered = parse_model_listing("cursor", CURSOR_LISTING)
    merged = merged_catalog("cursor", discovered)
    assert [(m["id"], m["source"]) for m in merged] == [
        ("grok-4.7-high-fast", "catalog"),
        ("grok-4.7-high", "catalog"),
        ("cursor-grok-4.6-high-fast", "catalog"),
        ("cursor-grok-4.6-high", "catalog"),
        ("auto", "harness"),
        ("gpt-5.3-codex-low", "harness"),
        ("claude-opus-5-5-thinking-high", "harness"),
    ]
    # extras keep the harness's own order; a repeated id keeps its first slot
    assert merged == merged_catalog("cursor", discovered + list(reversed(discovered)))
    assert [m["source"] for m in merged_catalog("cursor")] == ["catalog"] * 4
    shouted = merged_catalog(
        "cursor", [{"id": "CURSOR-GROK-4.6-HIGH"}, {"id": "Auto"}, {"id": "auto"}]
    )
    assert [(m["id"], m["source"]) for m in shouted[4:]] == [("Auto", "harness")]
    assert catalog_model("cursor", "auto") is None
    assert catalog_model("cursor", " AUTO ", discovered)["source"] == "harness"
    assert catalog_model("cursor", "cursor-grok-4.6-high", discovered)["source"] == "catalog"
    assert default_model("cursor", "auto", discovered)["id"] == "auto"
    assert default_model("cursor", "missing", discovered)["id"] == "grok-4.7-high-fast"


def test_discover_models_skips_missing_adapters_and_failures():
    class Adapter:
        def __init__(self, rows=None, error=None):
            self.rows, self.error = rows, error

        async def list_models(self):
            if self.error:
                raise self.error
            return self.rows

    class Bare:
        pass

    adapters = {
        "cursor": Adapter([dict(id="auto", label="Auto", description="", efforts=[])]),
        "grok": Adapter([]),
        "codex": Adapter(error=OSError("no binary")),
        "claude": Bare(),
    }

    result = asyncio.run(
        discover_models(["cursor", "cursor", "grok", "codex", "claude", "gemini"], adapters.get)
    )
    assert result == {"cursor": adapters["cursor"].rows}
