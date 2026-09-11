from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass
from typing import Any

from ..adapter_types import AgentHandle
from ..ledger import append_raw_log, write_log
from ..paths import ENGINE_ROOT, PLUGIN_ROOT
from ..render import _json
from ..resolve import effort_for
from ..scheduler import JS_WHITESPACE, number_text

DEFAULT_STEP_TIMEOUT_MS = 30 * 60 * 1000
VERDICT_MAX = 200
LOG_HEAD_BYTES = 2048
Json = dict[str, Any]
_SPACE = re.compile(f"[{re.escape(JS_WHITESPACE)}]+")


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def utf16_slice(text: str, start: int = 0, end: int | None = None) -> str:
    encoded = text.encode("utf-16-le", "surrogatepass")
    return encoded[start * 2 : None if end is None else end * 2].decode(
        "utf-16-le", "surrogatepass"
    )


def headline(text: str, maximum: int = VERDICT_MAX) -> str:
    line = next((s.strip(JS_WHITESPACE) for s in text.split("\n") if s.strip(JS_WHITESPACE)), "")
    flat = _SPACE.sub(" ", line)
    return utf16_slice(flat, 0, maximum - 1) + "…" if utf16_length(flat) > maximum else flat


def child_mcp_config(channel: Json, token: str) -> Json:
    engine_root = str(channel.get("engine_root", ENGINE_ROOT))
    return {
        "mcpServers": {
            "wise-engine": {
                "command": sys.executable,
                "args": ["-m", "wise_engine", "unit-mcp"],
                "env": {
                    "PYTHONPATH": engine_root,
                    "WISE_STEP_TOKEN": token,
                    "WISE_ENGINE_SOCKET": channel["socket_path"],
                    "WISE_DATA_ROOT": channel["data_root"],
                },
            }
        }
    }


def build_run_req(params: Json) -> Json:
    step, resolved = params["step"], params["resolved"]
    req = dict(
        prompt=step["prompt"],
        model=resolved["model"],
        cwd=params["cwd"],
        mode=step.get("mode", "auto"),
        timeout_ms=step["timeout"] * 1000
        if "timeout" in step
        else params.get("default_timeout_ms", DEFAULT_STEP_TIMEOUT_MS),
        auth=step.get("auth", "subscription"),
        step_token=params["step_token"],
        add_dirs=[str(params["run_dir"]), str(PLUGIN_ROOT), *params.get("add_dirs", [])],
    )
    for source, target in [
        ("allowed_tools", "allowed_tools"),
        ("mcp", "mcp_policy"),
        ("schema", "schema"),
        ("max_turns", "max_turns"),
    ]:
        if source in step:
            req[target] = step[source]
    if (
        resolved.get("effort", "") != ""
        and effort_for(resolved["harness"], resolved["effort"]) is not None
    ):
        req["effort"] = resolved["effort"]
    if step.get("resume") == "unit" and "cursor" in params:
        req["resume"] = params["cursor"]
    if "channel" in params:
        req["mcp_config"] = child_mcp_config(params["channel"], params["step_token"])
    return req


def _tool_names(parsed: Any) -> list[str]:
    if not isinstance(parsed, dict) or parsed.get("type") != "assistant":
        return []
    message = parsed.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return []
    return [
        block["name"] if isinstance(block.get("name"), str) else "?"
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]


def _excerpt(text: str) -> str:
    length = utf16_length(text)
    if length <= 2 * LOG_HEAD_BYTES:
        return text
    return f"{utf16_slice(text, 0, LOG_HEAD_BYTES)}\n\n[... {length - 2 * LOG_HEAD_BYTES} chars elided ...]\n\n{utf16_slice(text, -LOG_HEAD_BYTES)}"


def human_log(params: Json, req: Json, res: Json, tools: list[str]) -> str:
    usage = res["usage"]
    lines = [
        f"step: {params['step']['id']} ({params['step_run_id']})",
        f"harness: {params['resolved']['harness']} model: {req['model']} effort: {req.get('effort', '-')} mode: {req['mode']}",
        f"exit: {res['exit']}" + (f" error: {res['error']}" if res.get("error") else ""),
        f"usage: in={number_text(usage['input'])} out={number_text(usage['output'])} cache_read={number_text(usage['cache_read'])} cache_write={number_text(usage['cache_write'])}"
        + (f" cost_usd={number_text(usage['cost_usd'])}" if "cost_usd" in usage else "")
        + f" pool={usage['pool']}",
        f"tools: {', '.join(tools) if tools else '-'}",
    ]
    if "cursor" in res:
        lines.append(f"cursor: {_json(res['cursor'])}")
    if res.get("warnings"):
        lines.append(f"warnings: {'; '.join(res['warnings'])}")
    lines.extend(["", "--- text ---", _excerpt(res["text"])])
    if "json" in res:
        lines.extend(["", "--- json ---", _excerpt(_json(res["json"]))])
    return "\n".join(lines) + "\n"


def extract_outputs(names: list[str] | None, value: Any) -> Json:
    outputs: Json = {}
    for name in names or []:
        if not isinstance(value, dict) or name not in value:
            return dict(outputs=outputs, missing=name)
        outputs[name] = value[name]
    return {"outputs": outputs}


def outcome_of(step: Json, res: Json) -> Json:
    base = {"usage": res["usage"], "warnings": res.get("warnings", [])}
    for key in ("cursor", "json"):
        if key in res:
            base[key] = res[key]
    if res["exit"] != "ok":
        error = res.get("error", res["exit"])
        return {
            **base,
            "exit": res["exit"],
            "ok": False,
            "outputs": {},
            "verdict": headline(f"{res['exit']}: {error}"),
            "error": error,
        }
    extracted = extract_outputs(step.get("outputs"), res.get("json"))
    if "missing" in extracted:
        error = f"schema result lacks {extracted['missing']}"
        return {
            **base,
            "exit": "missing_output",
            "ok": False,
            "outputs": {},
            "verdict": headline(error),
            "error": error,
        }
    verdict = headline(res["text"]) or (headline(_json(res["json"])) if "json" in res else "ok")
    return {**base, "exit": "ok", "ok": True, "outputs": extracted["outputs"], "verdict": verdict}


@dataclass
class StartedAgent:
    handle: AgentHandle
    req: Json
    outcome: asyncio.Task[Json]


async def start_agent_step(params: Json) -> StartedAgent:
    req = build_run_req(params)
    tools = []

    def on_event(event: Json) -> None:
        tools.extend(_tool_names(event.get("parsed")))
        try:
            if params.get("on_event"):
                params["on_event"](event)
        except Exception:
            pass
        try:
            append_raw_log(params["run_dir"], params["step"]["id"], params["step_run_id"], event)
        except Exception:
            pass

    handle = await params["starter"](params["resolved"]["harness"], req, on_event)

    async def finish() -> Json:
        result = await handle.done
        try:
            write_log(
                params["run_dir"],
                params["step"]["id"],
                params["step_run_id"],
                human_log(params, req, result, tools),
            )
        except Exception:
            pass
        return outcome_of(params["step"], result)

    return StartedAgent(handle, req, asyncio.create_task(finish()))
