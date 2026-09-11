from __future__ import annotations

from typing import Any

BOT_REVIEW_STATES = ["resolved", "open", "stuck", "pending"]

CI_STATES = ["green", "red", "pending"]

MODEL_PHASES = ["plan", "implement", "review", "fix", "watch"]

PHASE_SCHEMAS = {
    "plan": {
        "type": "object",
        "properties": {
            "plan_path": {"type": "string"},
            "status": {"type": "string", "enum": ["ready", "insufficient-context", "no-access"]},
            "blueprint_path": {"type": "string"},
        },
        "required": ["plan_path", "status"],
        "additionalProperties": False,
    },
    "implement": {
        "type": "object",
        "properties": {
            "waves": {"type": "integer", "minimum": 0},
            "tasks": {"type": "integer", "minimum": 0},
            "done": {"type": "integer", "minimum": 0},
            "failed": {"type": "integer", "minimum": 0},
            "commits": {"type": "integer", "minimum": 0},
        },
        "required": ["waves", "tasks", "done", "failed", "commits"],
        "additionalProperties": False,
    },
    "review": {
        "type": "object",
        "properties": {
            "findings": {"type": "integer", "minimum": 0},
            "blocking": {"type": "integer", "minimum": 0},
            "verdict": {"type": "string", "enum": ["approve", "changes-requested"]},
        },
        "required": ["findings", "blocking", "verdict"],
        "additionalProperties": False,
    },
    "fix": {
        "type": "object",
        "properties": {
            "fixed": {"type": "integer", "minimum": 0},
            "skipped": {"type": "integer", "minimum": 0},
            "commits": {"type": "integer", "minimum": 0},
        },
        "required": ["fixed", "skipped", "commits"],
        "additionalProperties": False,
    },
    "watch": {
        "type": "object",
        "properties": {
            "ci": {"type": "string", "enum": ["green", "red", "pending"]},
            "bot_reviews": {"type": "string", "enum": ["resolved", "open", "stuck", "pending"]},
            "human_comment": {"type": "boolean"},
            "merged": {"type": "boolean"},
            "verdict": {
                "type": "string",
                "enum": ["ready", "wait", "fix", "blocked", "needs-human"],
            },
        },
        "required": ["ci", "bot_reviews", "human_comment", "merged", "verdict"],
        "additionalProperties": False,
    },
}

PLAN_STATUSES = ["ready", "insufficient-context", "no-access"]

REVIEW_VERDICTS = ["approve", "changes-requested"]

WATCH_VERDICTS = ["ready", "wait", "fix", "blocked", "needs-human"]


def is_model_phase(phase: str) -> bool:
    return phase in MODEL_PHASES


def phase_key(step_id: str, phase: str) -> str:
    return step_id + "." + phase


def _integer(value: Any) -> bool:
    return type(value) in (int, float) and value >= 0 and float(value).is_integer()


def parse_plan(value: Any) -> dict[str, Any] | None:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("plan_path"), str)
        or value.get("status") not in PLAN_STATUSES
    ):
        return None
    out = {"plan_path": value["plan_path"], "status": value["status"]}
    if isinstance(value.get("blueprint_path"), str) and value["blueprint_path"]:
        out["blueprint_path"] = value["blueprint_path"]
    return out


def _counts(value: Any, keys: tuple[str, ...]) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not all(_integer(value.get(k)) for k in keys):
        return None
    return {k: value[k] for k in keys}


def parse_implement(value: Any) -> dict[str, Any] | None:
    return _counts(value, ("waves", "tasks", "done", "failed", "commits"))


def parse_fix(value: Any) -> dict[str, Any] | None:
    return _counts(value, ("fixed", "skipped", "commits"))


def parse_review(value: Any) -> dict[str, Any] | None:
    out = _counts(value, ("findings", "blocking"))
    if out is None or value.get("verdict") not in REVIEW_VERDICTS:
        return None
    return {**out, "verdict": value["verdict"]}


def parse_watch(value: Any) -> dict[str, Any] | None:
    if (
        not isinstance(value, dict)
        or value.get("ci") not in CI_STATES
        or value.get("bot_reviews") not in BOT_REVIEW_STATES
        or value.get("verdict") not in WATCH_VERDICTS
        or type(value.get("human_comment")) is not bool
        or type(value.get("merged")) is not bool
    ):
        return None
    return {k: value[k] for k in ("ci", "bot_reviews", "human_comment", "merged", "verdict")}
