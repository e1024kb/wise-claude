from __future__ import annotations

import math
import re
from typing import Any

from .models import CLAUDE_ALIASES

PRICES: dict[str, Any] = {
    "claude-fable-5-1": {"input": 10, "output": 50, "cache_read": 0.25, "cache_write": 12.5},
    "claude-fable-5": {"input": 10, "output": 50, "cache_read": 1, "cache_write": 12.5},
    "claude-opus-5": {"input": 5, "output": 25, "cache_read": 0.5, "cache_write": 6.25},
    "claude-opus-4-8": {"input": 5, "output": 25, "cache_read": 0.5, "cache_write": 6.25},
    "claude-opus-4-7": {"input": 5, "output": 25, "cache_read": 0.5, "cache_write": 6.25},
    "claude-opus-4-6": {"input": 5, "output": 25, "cache_read": 0.5, "cache_write": 6.25},
    "claude-sonnet-5": {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5},
    "claude-sonnet-4-6": {
        "input": 3,
        "output": 15,
        "cache_read": 0.30000000000000004,
        "cache_write": 3.75,
    },
    "claude-haiku-4-5": {"input": 1, "output": 5, "cache_read": 0.1, "cache_write": 1.25},
    "gpt-5": {
        "input": 1.25,
        "output": 10,
        "cache_read": 0.125,
        "cache_write": 0,
        "cached_in_input": True,
    },
    "gpt-5-mini": {
        "input": 0.25,
        "output": 2,
        "cache_read": 0.025,
        "cache_write": 0,
        "cached_in_input": True,
    },
    "gpt-5-nano": {
        "input": 0.05,
        "output": 0.4,
        "cache_read": 0.005,
        "cache_write": 0,
        "cached_in_input": True,
    },
    "gpt-5-codex": {
        "input": 1.25,
        "output": 10,
        "cache_read": 0.125,
        "cache_write": 0,
        "cached_in_input": True,
    },
    "gpt-5.1": {
        "input": 1.25,
        "output": 10,
        "cache_read": 0.125,
        "cache_write": 0,
        "cached_in_input": True,
    },
    "gpt-5.1-codex": {
        "input": 1.25,
        "output": 10,
        "cache_read": 0.125,
        "cache_write": 0,
        "cached_in_input": True,
    },
    "gpt-5.2": {
        "input": 1.75,
        "output": 14,
        "cache_read": 0.175,
        "cache_write": 0,
        "cached_in_input": True,
    },
    "gpt-5.2-codex": {
        "input": 1.75,
        "output": 14,
        "cache_read": 0.175,
        "cache_write": 0,
        "cached_in_input": True,
    },
    "gpt-5.3-codex": {
        "input": 1.75,
        "output": 14,
        "cache_read": 0.175,
        "cache_write": 0,
        "cached_in_input": True,
    },
    "gpt-5.4": {
        "input": 2.5,
        "output": 15,
        "cache_read": 0.25,
        "cache_write": 0,
        "cached_in_input": True,
    },
    "gpt-5.5": {
        "input": 5,
        "output": 30,
        "cache_read": 0.5,
        "cache_write": 0,
        "cached_in_input": True,
    },
    "gpt-5.6-sol": {
        "input": 4,
        "output": 20,
        "cache_read": 0.4,
        "cache_write": 0,
        "cached_in_input": True,
    },
    "gpt-5.6-terra": {
        "input": 2,
        "output": 12,
        "cache_read": 0.2,
        "cache_write": 0,
        "cached_in_input": True,
    },
    "gpt-5.6-luna": {
        "input": 0.2,
        "output": 1.2,
        "cache_read": 0.02,
        "cache_write": 0,
        "cached_in_input": True,
    },
    "gpt-6-astra": {
        "input": 10,
        "output": 50,
        "cache_read": 1,
        "cache_write": 0,
        "cached_in_input": True,
    },
    "grok-4.6": {"input": 2, "output": 6, "cache_read": 0.5, "cache_write": 0},
    "grok-4.5": {"input": 2, "output": 6, "cache_read": 0.3, "cache_write": 0},
    "grok-4.3": {"input": 1.25, "output": 2.5, "cache_read": 0.2, "cache_write": 0},
    "grok-build-0.1": {"input": 1, "output": 2, "cache_read": 0.2, "cache_write": 0},
}


def canonical_model(harness: str, model: str) -> str:
    value = model.strip().lower()
    if not value or value == "inherit":
        return ""
    if harness == "claude" and value in CLAUDE_ALIASES:
        return CLAUDE_ALIASES[value]
    return re.sub(r"-latest$", "", re.sub(r"-\d{8}$", "", value))


def price_for(harness: str, model: str) -> dict[str, Any] | None:
    return PRICES.get(canonical_model(harness, model))


def cost_of(usage: dict[str, Any], price: dict[str, Any]) -> float:
    fresh = (
        max(0, usage["input"] - usage["cache_read"])
        if price.get("cached_in_input")
        else usage["input"]
    )
    total = (
        fresh * price["input"]
        + usage["cache_read"] * price["cache_read"]
        + usage["cache_write"] * price["cache_write"]
        + usage["output"] * price["output"]
    )
    return math.floor((total / 1_000_000) * 1_000_000 + 0.5) / 1_000_000


def price_usage(usage: dict[str, Any], harness: str, model: str) -> dict[str, Any]:
    if "cost_source" in usage:
        return {"usage": usage}
    result = dict(usage)
    source = "none"
    if "cost_usd" in usage:
        source = "reported"
    elif usage["pool"] == "api-key":
        price = price_for(harness, model)
        if price is None:
            result["cost_source"] = "none"
            return {"usage": result, "unknownModel": model.strip() or "inherit"}
        result["cost_usd"] = cost_of(usage, price)
        source = "priced"
    result["cost_source"] = source
    return {"usage": result}
