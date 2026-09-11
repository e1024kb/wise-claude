from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

from .paths import PLUGIN_ROOT
from .scheduler import UNDEFINED, js_string, number_text


def _entries(value: Mapping[str, Any]) -> list[tuple[str, Any]]:
    indices = sorted(
        (key for key in value if re.fullmatch(r"0|[1-9][0-9]*", key) and int(key) < 2**32 - 1),
        key=int,
    )
    return [(key, value[key]) for key in indices] + [
        (key, item) for key, item in value.items() if key not in indices
    ]


def _json(value: Any, indent: int | None = None, level: int = 0) -> str:
    if value is None or value is UNDEFINED:
        return "null"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return number_text(value) if math.isfinite(value) else "null"
    items = []
    if isinstance(value, Mapping):
        opening, closing = "{", "}"
        for key, item in _entries(value):
            if item is not UNDEFINED:
                items.append(
                    f"{json.dumps(key, ensure_ascii=False)}:{' ' if indent else ''}{_json(item, indent, level + 1)}"
                )
    else:
        opening, closing = "[", "]"
        items = [_json(item, indent, level + 1) for item in value]
    if not items:
        return opening + closing
    if indent is None:
        return opening + ",".join(items) + closing
    padding = " " * indent
    return (
        opening
        + "\n"
        + ",\n".join(padding * (level + 1) + item for item in items)
        + "\n"
        + padding * level
        + closing
    )


def _stringify(value: Any) -> str:
    if value is None or value is UNDEFINED:
        return ""
    if isinstance(value, (dict, list)):
        return _json(value)
    return js_string(value)


def _replace_all(template: str, search: str, replacement: str) -> str:
    # JavaScript expands these dollar tokens even when the search is a plain string.
    pieces = []
    cursor = 0
    while (position := template.find(search, cursor)) >= 0:
        pieces.append(template[cursor:position])
        tokens = {
            "$": "$",
            "&": search,
            "`": template[:position],
            "'": template[position + len(search) :],
        }
        expanded = []
        index = 0
        while index < len(replacement):
            if (
                replacement[index] == "$"
                and index + 1 < len(replacement)
                and replacement[index + 1] in tokens
            ):
                expanded.append(tokens[replacement[index + 1]])
                index += 2
            else:
                expanded.append(replacement[index])
                index += 1
        pieces.append("".join(expanded))
        cursor = position + len(search)
    pieces.append(template[cursor:])
    return "".join(pieces)


def usage_json(state: Mapping[str, Any]) -> str:
    from .ledger import usage_total

    usage = state["usage"]
    return _json(
        {
            "total": usage_total(usage),
            "by_pool": {"subscription": usage["subscription"], "api-key": usage["api-key"]},
            "by_harness": usage["by_harness"],
            "by_step": usage.get("by_step") or {},
        },
        indent=2,
    )


def render(
    template: str, state: Mapping[str, Any], workflow_dir: str, run_dir: str | None = None
) -> str:
    output = _replace_all(template, "${CLAUDE_PLUGIN_ROOT}", str(PLUGIN_ROOT))
    output = _replace_all(output, "{{workflow.dir}}", workflow_dir)
    if run_dir is not None:
        output = _replace_all(output, "{{run.dir}}", run_dir)
    output = _replace_all(output, "{{run.id}}", state["run_id"])
    for key, value in _entries(state.get("project") or {}):
        output = _replace_all(output, "{{project." + key + "}}", _stringify(value))
    named = {**(state.get("inputs") or {}), **(state.get("outputs") or {})}
    for key, value in _entries(named):
        output = _replace_all(output, "{{" + key + "}}", _stringify(value))
    if "{{usage}}" in output:
        output = _replace_all(output, "{{usage}}", usage_json(state))
    return output


def render_step(
    step: Any, state: Mapping[str, Any], workflow_dir: str, run_dir: str | None = None
) -> Any:
    if isinstance(step, str):
        return render(step, state, workflow_dir, run_dir)
    if isinstance(step, list):
        return [render_step(item, state, workflow_dir, run_dir) for item in step]
    if isinstance(step, dict):
        return {
            key: render_step(value, state, workflow_dir, run_dir) for key, value in _entries(step)
        }
    return step


def render_vars(template: str, variables: Mapping[str, Any]) -> str:
    output = template
    for key, value in _entries(variables):
        output = _replace_all(output, "{{" + key + "}}", _stringify(value))
    return output


def unresolved_placeholders(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"\{\{([^{}]+)\}\}", text)))
