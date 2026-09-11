from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..adapter_types import AgentHandle, EventCallback, Json
from ..permissions import decide_permission
from ..spawn import SpawnExit, SpawnOptions, clean_env, spawn_clean
from ._common import (
    Parser,
    ProviderAdapter,
    arm_task,
    clip,
    dumps,
    exit_detail,
    finish_process,
    loads,
    number,
    probe_process,
    rec,
    string,
    token_usage,
)

CLAUDE_BIN = "claude"
CLAUDE_KEY_VAR = "ANTHROPIC_API_KEY"
CLAUDE_CONFIG_VAR = "CLAUDE_CONFIG_DIR"
MODE_MAP = {
    "approval-required": "default",
    "auto": "acceptEdits",
    "full-access": "bypassPermissions",
}
EMPTY_MCP_CONFIG: Json = {"mcpServers": {}}
INIT_REQUEST_ID = "wise-init"
RATE_LIMIT_RE = re.compile(r"rate.?limit|429|overloaded", re.I)
AUTH_RE = re.compile(r"Failed to authenticate|OAuth|not logged in|login", re.I)


def build_argv(req: Json) -> list[str]:
    argv = ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    if req.get("model") and req["model"] != "inherit":
        argv += ["--model", req["model"]]
    if "effort" in req:
        argv += ["--effort", effort_map(req["effort"])]
    if "schema" in req:
        argv += ["--json-schema", dumps(req["schema"])]
    if "max_turns" in req:
        argv += ["--max-turns", str(req["max_turns"])]
    if isinstance(req.get("resume"), str) and req["resume"]:
        argv += ["--resume", req["resume"]]
    argv += ["--permission-mode", MODE_MAP[req["mode"]]]
    for directory in req.get("add_dirs", []):
        argv += ["--add-dir", directory]
    allowed = [
        f"mcp__{name}" for name in (req.get("mcp_config") or {}).get("mcpServers", {})
    ] + req.get("allowed_tools", [])
    if allowed:
        argv += ["--allowedTools", ",".join(allowed)]
    argv += ["--permission-prompt-tool", "stdio"]
    if req.get("mcp_policy") == "engine-only":
        argv += ["--strict-mcp-config"]
    argv += ["--mcp-config", dumps(req.get("mcp_config") or EMPTY_MCP_CONFIG)]
    if "system" in req:
        argv += ["--append-system-prompt", req["system"]]
    return argv


def initialize_message() -> str:
    return (
        dumps(
            dict(
                type="control_request",
                request_id=INIT_REQUEST_ID,
                request=dict(subtype="initialize", hooks={}),
            )
        )
        + "\n"
    )


def permission_request_of(parsed: Any) -> Json | None:
    if not isinstance(parsed, dict) or parsed.get("type") != "control_request":
        return None
    request = rec(parsed.get("request"))
    request_id, tool_name = string(parsed.get("request_id")), string(request.get("tool_name"))
    if request.get("subtype") != "can_use_tool" or not request_id or not tool_name:
        return None
    result = dict(request_id=request_id, tool_name=tool_name)
    if "input" in request:
        result["input"] = request["input"]
    return result


def permission_response(request_id: str, decision: Json) -> str:
    return (
        dumps(
            dict(
                type="control_response",
                response=dict(subtype="success", request_id=request_id, response=decision),
            )
        )
        + "\n"
    )


def user_message(text: str) -> str:
    return (
        dumps(dict(type="user", message=dict(role="user", content=[dict(type="text", text=text)])))
        + "\n"
    )


def child_env(req: Json, parent: Mapping[str, str | None] | None = None) -> dict[str, str]:
    return clean_env(
        parent=parent,
        keep=[CLAUDE_CONFIG_VAR],
        secrets=[CLAUDE_KEY_VAR] if req["auth"] == "api-key" else [],
        extra=req.get("env"),
    )


def effort_map(effort: str) -> str:
    return effort


class StreamParser(Parser):
    harness = "claude"

    def __init__(
        self,
        *,
        pool: str,
        mode: str = "approval-required",
        workspace_roots: list[str] | tuple[str, ...] = (),
        on_result: Callable[[Json], None] | None = None,
        on_permission: Callable[[Json, Json], None] | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(pool=pool, now=now)
        self.mode, self.workspace_roots = mode, workspace_roots
        self.on_result, self.on_permission = on_result, on_permission
        self.snap = dict(tools=0, turns=0, tool_uses=[], results=0, denials=[], permissions=[])
        self.last_assistant_text = ""
        self.result: Json | None = None

    def accept(self, parsed: Json) -> None:
        kind = parsed.get("type")
        if kind == "system" and parsed.get("subtype") == "init":
            for name in ("session_id", "model"):
                if isinstance(parsed.get(name), str):
                    self.snap[name] = parsed[name]
            if isinstance(parsed.get("tools"), list):
                self.snap["tools"] = len(parsed["tools"])
        elif kind == "assistant":
            self.snap["turns"] += 1
            parts = rec(parsed.get("message")).get("content", [])
            parts = (
                [part for part in parts if isinstance(part, dict)]
                if isinstance(parts, list)
                else []
            )
            self.snap["tool_uses"] += [
                string(part.get("name")) or "?" for part in parts if part.get("type") == "tool_use"
            ]
            text = "".join(
                string(part.get("text")) or "" for part in parts if part.get("type") == "text"
            )
            if text:
                self.last_assistant_text = text
        elif kind == "result":
            self.result = parsed
            self.snap["results"] += 1
            denials = parsed.get("permission_denials", [])
            self.snap["denials"] = (
                [
                    f"{string(item.get('tool_name')) or 'tool'}({dumps(item['tool_input']) if 'tool_input' in item else ''})"
                    for item in denials
                    if isinstance(item, dict)
                ]
                if isinstance(denials, list)
                else []
            )
            if self.on_result:
                self.on_result(parsed)
        elif kind == "control_request":
            request = permission_request_of(parsed)
            if request is not None:
                decision = decide_permission(
                    request["tool_name"],
                    request.get("input"),
                    mode=self.mode,
                    workspace_roots=self.workspace_roots,
                )
                self.snap["permissions"].append(f"{decision['behavior']}:{request['tool_name']}")
                if self.on_permission:
                    self.on_permission(request, decision)

    def classify(self, exit: SpawnExit) -> Json:
        if exit.timed_out:
            return dict(exit="timeout", error=clip(exit.stderr) or "timed out")
        if self.result is not None:
            if self.result.get("subtype") == "error_max_turns":
                return dict(exit="max_turns", error="max turns reached")
            if self.result.get("is_error") is False:
                return dict(exit="ok")
            text = string(self.result.get("result"))
            if text is None:
                text = string(self.result.get("error")) or ""
            haystack = text + "\n" + exit.stderr
            error = clip(
                text or exit.stderr or f"result subtype {self.result.get('subtype', 'undefined')}"
            )
        else:
            haystack = exit.stderr
            error = (
                exit.error
                if exit.error is not None
                else (
                    clip(exit.stderr)
                    if exit.stderr.strip()
                    else exit_detail(exit, "no result event")
                )
            )
        kind = (
            "rate_limited"
            if RATE_LIMIT_RE.search(haystack)
            else "auth"
            if AUTH_RE.search(haystack)
            else "error"
        )
        return dict(
            exit=kind, error=clip(exit.stderr) if self.result is None and kind != "error" else error
        )

    def finish(self, exit: SpawnExit) -> Json:
        self.flush()
        result = rec(self.result)
        text = string(result.get("result"))
        if text is None:
            text = self.last_assistant_text
        if not text and self.snap["denials"]:
            text = "permission denied: " + ", ".join(self.snap["denials"])
        res = dict(text=text, usage=token_usage(self.result, self.pool), **self.classify(exit))
        if "structured_output" in result:
            res["json"] = result["structured_output"]
        cursor = string(result.get("session_id"))
        if cursor is None:
            cursor = self.snap.get("session_id")
        if cursor is not None:
            res["cursor"] = cursor
        if self.snap["denials"]:
            res["warnings"] = [
                f"{len(self.snap['denials'])} permission denial(s): {', '.join(self.snap['denials'][:3])}"
            ]
        return res


def create_stream_parser(**opts: Any) -> StreamParser:
    return StreamParser(**opts)


async def start_claude(
    req: Json,
    on_event: EventCallback,
    *,
    bin: str = CLAUDE_BIN,
    parent_env: Mapping[str, str | None] | None = None,
) -> AgentHandle:
    proc = await spawn_clean(
        bin,
        build_argv(req),
        SpawnOptions(cwd=req["cwd"], env=child_env(req, parent_env), timeout_ms=req["timeout_ms"]),
    )
    outstanding: int | float = 0
    stdin_open = True

    def on_result(result: Json) -> None:
        nonlocal outstanding, stdin_open
        queued = number(result.get("queued_turn_count"))
        outstanding = max(0, queued) if queued is not None else max(0, outstanding - 1)
        if outstanding == 0 and stdin_open:
            stdin_open = False
            proc.stdin.end()

    def on_permission(request: Json, decision: Json) -> None:
        if stdin_open:
            proc.stdin.write(permission_response(request["request_id"], decision))

    def send(text: str) -> None:
        nonlocal outstanding
        if not stdin_open:
            raise RuntimeError("claude stdin is closed")
        outstanding += 1
        proc.stdin.write(user_message(text))

    parser = StreamParser(
        pool=req["auth"],
        mode=req["mode"],
        workspace_roots=[
            req["cwd"],
            *[
                str((Path(req["cwd"]) / directory).resolve())
                for directory in req.get("add_dirs", [])
            ],
        ],
        on_result=on_result,
        on_permission=on_permission,
    )

    async def finish() -> Json:
        nonlocal stdin_open
        try:
            return await finish_process(proc, parser, on_event)
        finally:
            stdin_open = False

    if proc.pid > 0:
        proc.stdin.write(initialize_message())
        send(req["prompt"])
    done = await arm_task(finish(), proc)
    return AgentHandle(
        done=done, pid=proc.pid, nudge=send, kill=proc.kill, snapshot=parser.snapshot
    )


async def probe_auth(
    auth: str, *, bin: str = CLAUDE_BIN, parent_env: Mapping[str, str | None] | None = None
) -> Json:
    if auth == "api-key":
        return dict(
            ok=bool((os.environ if parent_env is None else parent_env).get(CLAUDE_KEY_VAR)),
            login_cmd=f"export {CLAUDE_KEY_VAR}=...",
        )
    _, stdout = await probe_process(bin, ["auth", "status"], child_env(dict(auth=auth), parent_env))
    try:
        ok = rec(loads(stdout)).get("loggedIn") is True
    except ValueError:
        ok = False
    return dict(ok=ok, login_cmd="claude auth login")


claude_adapter = ProviderAdapter("claude", CLAUDE_BIN, start_claude, probe_auth, effort_map)
