from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .constants import EFFORTS

SOURCE_CATALOG = "catalog"
SOURCE_HARNESS = "harness"

MODEL_CATALOG: dict[str, Any] = {
    "claude": [
        {
            "id": "claude-fable-5-1",
            "label": "Fable 5.1",
            "description": "latest Fable",
            "efforts": ["low", "medium", "high"],
        },
        {
            "id": "claude-opus-5-5",
            "label": "Opus 5.5",
            "description": "current Opus",
            "efforts": ["low", "medium", "high"],
        },
        {
            "id": "claude-opus-4-8",
            "label": "Opus 4.8",
            "description": "previous Opus",
            "efforts": ["low", "medium", "high"],
        },
        {
            "id": "claude-sonnet-5",
            "label": "Sonnet 5",
            "description": "latest Sonnet",
            "efforts": ["low", "medium"],
        },
        {
            "id": "claude-haiku-4-5",
            "label": "Haiku 4.5",
            "description": "cheap tier for simple steps",
            "efforts": ["medium"],
        },
        {
            "id": "claude-fable-5",
            "label": "Fable 5",
            "description": "previous Fable",
            "efforts": ["low", "medium", "high"],
        },
    ],
    "codex": [
        {
            "id": "gpt-6-astra",
            "label": "GPT-6 Astra",
            "description": "OpenAI flagship",
            "efforts": ["low", "medium", "high"],
        },
        {
            "id": "gpt-6-sol",
            "label": "GPT-6 Sol",
            "description": "workhorse for coding",
            "efforts": ["low", "medium", "high"],
        },
        {
            "id": "gpt-6-luna",
            "label": "GPT-6 Luna",
            "description": "cheap tier",
            "efforts": ["low", "medium", "high"],
        },
    ],
    "cursor": [
        {
            "id": "grok-4.7-high-fast",
            "label": "Grok 4.7 High Fast",
            "description": "SpaceXAI's most powerful model, fast mode",
            "efforts": [],
        },
        {
            "id": "grok-4.7-high",
            "label": "Grok 4.7 High",
            "description": "SpaceXAI's most powerful model",
            "efforts": [],
        },
        {
            "id": "cursor-grok-4.6-high-fast",
            "label": "Cursor Grok 4.6 High Fast",
            "description": "previous Cursor Grok, fast mode",
            "efforts": [],
        },
        {
            "id": "cursor-grok-4.6-high",
            "label": "Cursor Grok 4.6 High",
            "description": "previous Cursor Grok",
            "efforts": [],
        },
    ],
    "grok": [
        {"id": "grok-4.6", "label": "Grok 4.6", "description": "xAI current model", "efforts": []},
        {"id": "grok-4.5", "label": "Grok 4.5", "description": "previous xAI model", "efforts": []},
    ],
    "gemini": [
        {
            "id": "gemini-3.8-flash",
            "label": "Gemini 3.8 Flash",
            "description": "latest Flash",
            "efforts": [],
        },
        {
            "id": "gemini-3.5-flash-lite",
            "label": "Gemini 3.5 Flash-Lite",
            "description": "cheapest Gemini",
            "efforts": [],
        },
    ],
}

# Accepted when typed as a model answer or pin, never listed as a picker option.
TYPED_ONLY_CATALOG: dict[str, Any] = {
    "claude": [
        {
            "id": "claude-opus-5",
            "label": "Opus 5",
            "description": "previous Opus, accepted when typed",
            "efforts": ["low", "medium", "high"],
        },
    ],
    "codex": [
        {
            "id": "gpt-5.6-sol",
            "label": "GPT-5.6 Sol",
            "description": "previous Sol, accepted when typed",
            "efforts": ["low", "medium", "high"],
        },
        {
            "id": "gpt-5.6-luna",
            "label": "GPT-5.6 Luna",
            "description": "previous Luna, accepted when typed",
            "efforts": ["low", "medium", "high"],
        },
        {
            "id": "gpt-5.5",
            "label": "GPT-5.5",
            "description": "legacy model, accepted when typed",
            "efforts": ["low", "medium", "high"],
        },
    ],
}

CLAUDE_ALIASES: dict[str, Any] = {
    "fable": "claude-fable-5-1",
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}


Discovered = dict[str, list[dict[str, Any]]]

MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
CURSOR_LINE_RE = re.compile(r"^(?P<id>\S+)\s+-\s+(?P<label>.+?)\s*$")
GROK_LINE_RE = re.compile(r"^\s*[*-]\s+(?P<id>\S+)(?:\s+\((?P<note>[^)]*)\))?\s*$")


def catalog_for(harness: str) -> list[dict[str, Any]]:
    return MODEL_CATALOG[harness]


def discovered_model(harness: str, model_id: str, label: str | None = None) -> dict[str, Any]:
    return {
        "id": model_id,
        "label": label or model_id,
        "description": f"reported by the {harness} harness",
        "efforts": [],
    }


def parse_cursor_models(harness: str, text: str) -> list[dict[str, Any]]:
    rows = []
    for line in text.splitlines():
        match = CURSOR_LINE_RE.match(line.strip())
        if match is None or not MODEL_ID_RE.match(match["id"]):
            continue
        rows.append(discovered_model(harness, match["id"], match["label"]))
    return rows


def parse_grok_models(harness: str, text: str) -> list[dict[str, Any]]:
    rows = []
    listing = False
    for line in text.splitlines():
        if not listing:
            listing = line.strip().lower().startswith("available models")
            continue
        match = GROK_LINE_RE.match(line)
        if match is None or not MODEL_ID_RE.match(match["id"]):
            continue
        rows.append(discovered_model(harness, match["id"]))
    return rows


MODEL_PARSERS: dict[str, Callable[[str, str], list[dict[str, Any]]]] = {
    "cursor": parse_cursor_models,
    "grok": parse_grok_models,
}


def parse_model_listing(harness: str, text: str) -> list[dict[str, Any]]:
    parser = MODEL_PARSERS.get(harness)
    return parser(harness, text) if parser else []


def merged_catalog(
    harness: str, discovered: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    rows = [{**model, "source": SOURCE_CATALOG} for model in catalog_for(harness)]
    known = {model["id"].casefold() for model in rows}
    extra: dict[str, dict[str, Any]] = {}
    for model in discovered or []:
        key = model["id"].casefold()
        if key in known or key in extra:
            continue
        extra[key] = {**model, "source": SOURCE_HARNESS}
    # Harness-reported extras follow the catalog in the order the harness
    # listed them; the catalog itself is the hand-ordered part.
    rows.extend(extra.values())
    return rows


def catalog_model(
    harness: str, model_id: str | None, discovered: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    if model_id is None:
        return None
    key = model_id.strip().lower()
    if harness == "claude":
        key = CLAUDE_ALIASES.get(key, key)
    catalog = merged_catalog(harness, discovered) if discovered else catalog_for(harness)
    catalog = [*catalog, *TYPED_ONLY_CATALOG.get(harness, [])]
    return next((model for model in catalog if model["id"].lower() == key), None)


def default_model(
    harness: str, pinned: str | None = None, discovered: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return catalog_model(harness, pinned, discovered) or catalog_for(harness)[0]


async def discover_models(
    harnesses: list[str], lookup: Callable[[str], Any], cache: Discovered | None = None
) -> Discovered:
    """List each harness's models once; `cache` carries the outcome across calls.

    A harness already in the cache is not probed again, whether its listing
    returned rows, nothing, or failed: the predefined catalog covers the
    negative cases and a later failure never evicts rows already discovered.
    """
    result: Discovered = {}
    for harness in dict.fromkeys(harnesses):
        if cache is not None and harness in cache:
            if cache[harness]:
                result[harness] = cache[harness]
            continue
        adapter = lookup(harness)
        probe = getattr(adapter, "list_models", None)
        if adapter is None or probe is None:
            continue
        try:
            rows = await probe()
        except Exception:
            rows = []
        if cache is not None:
            cache[harness] = rows
        if rows:
            result[harness] = rows
    return result


def default_effort(model: dict[str, Any], wanted: str | None = None) -> str | None:
    efforts = model["efforts"]
    if not efforts:
        return None
    want = (wanted or "").strip().lower()
    if want in efforts:
        return want
    if want in EFFORTS:
        for candidate in reversed(EFFORTS[: EFFORTS.index(want)]):
            if candidate in efforts:
                return candidate
    return efforts[0]
