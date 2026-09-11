from __future__ import annotations

from typing import Any

from .constants import EFFORTS

MODEL_CATALOG: dict[str, Any] = {
    "claude": [
        {
            "id": "claude-fable-5-1",
            "label": "Fable 5.1",
            "description": "latest Fable",
            "efforts": ["low", "medium", "high"],
        },
        {
            "id": "claude-opus-5",
            "label": "Opus 5",
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
    ],
    "codex": [
        {
            "id": "gpt-6-astra",
            "label": "GPT-6 Astra",
            "description": "OpenAI flagship",
            "efforts": ["low", "medium", "high"],
        },
        {
            "id": "gpt-5.6-sol",
            "label": "GPT-5.6 Sol",
            "description": "5.6 top tier",
            "efforts": ["low", "medium", "high"],
        },
        {
            "id": "gpt-5.6-luna",
            "label": "GPT-5.6 Luna",
            "description": "5.6 cheap tier",
            "efforts": ["low", "medium", "high"],
        },
        {
            "id": "gpt-5.5",
            "label": "GPT-5.5",
            "description": "previous generation",
            "efforts": ["low", "medium", "high"],
        },
    ],
    "cursor": [
        {
            "id": "grok-4.6",
            "label": "Grok 4.6",
            "description": "Cursor's frontier model for complex agentic work",
            "efforts": [],
        },
        {
            "id": "composer-2.5",
            "label": "Composer 2.5",
            "description": "Cursor's fast, cost-efficient coding model",
            "efforts": [],
        },
    ],
    "grok": [
        {"id": "grok-4.6", "label": "Grok 4.6", "description": "xAI current model", "efforts": []}
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

CLAUDE_ALIASES: dict[str, Any] = {
    "fable": "claude-fable-5-1",
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}


def catalog_for(harness: str) -> list[dict[str, Any]]:
    return MODEL_CATALOG[harness]


def catalog_model(harness: str, model_id: str | None) -> dict[str, Any] | None:
    if model_id is None:
        return None
    key = model_id.strip().lower()
    if harness == "claude":
        key = CLAUDE_ALIASES.get(key, key)
    return next((model for model in catalog_for(harness) if model["id"] == key), None)


def default_model(harness: str, pinned: str | None = None) -> dict[str, Any]:
    return catalog_model(harness, pinned) or catalog_for(harness)[0]


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
