from __future__ import annotations

from typing import Any

from ..render import _json
from ..rpc import RpcError
from ..scheduler import JS_WHITESPACE
from .agent import headline

APPROVAL_OPTIONS = (
    {"value": "approve", "label": "Approve"},
    {"value": "reject", "label": "Reject"},
)
Json = dict[str, Any]


def is_gate_step(step: Json) -> bool:
    return step["type"] in ("approval", "ask")


def build_gate(step: Json, gate_id: str) -> Json:
    gate = dict(
        gate_id=gate_id,
        step=step["id"],
        kind=step["type"],
        message=step["message"].strip(JS_WHITESPACE),
    )
    if step["type"] == "approval":
        gate["options"] = [dict(option) for option in APPROVAL_OPTIONS]
    else:
        if step.get("options"):
            gate["options"] = [dict(value=option, label=option) for option in step["options"]]
        if "allow_text" in step:
            gate["allow_text"] = step["allow_text"]
    return gate


def decide_gate(step: Json, value: str | list[str]) -> Json:
    text = (", ".join(value) if isinstance(value, list) else value).strip(JS_WHITESPACE)
    if step["type"] == "approval":
        if text == "approve":
            return dict(status="completed", verdict="approved")
        if text == "reject":
            return dict(status="failed", verdict="rejected")
        raise RpcError(
            -32602, f'answer: approval {step["id"]} takes "approve" or "reject", got {_json(text)}'
        )
    options = step.get("options", [])
    allow_text = step.get("allow_text", len(options) == 0)
    if text == "" or (not allow_text and text not in options):
        raise RpcError(
            -32602, f"answer: ask {step['id']} takes one of {_json(options)}, got {_json(text)}"
        )
    name = step.get("output", step["id"])
    return dict(
        status="completed", verdict=headline(f"{name}={text}"), output={"name": name, "value": text}
    )
