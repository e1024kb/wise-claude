from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import uuid
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from ..adapter_types import AgentHandle, EventCallback, Json
from ..permissions import decide_permission
from ..spawn import SpawnOptions, spawn_clean
from ._common import arm_task, dumps, loads, rec, string
from .gemini import compose_prompt


def prepare_resume(req: Json, env: Mapping[str, str]) -> str | None:
    resume = req.get("resume")
    if not isinstance(resume, str) or not resume:
        return None
    try:
        uuid.UUID(resume)
    except ValueError:
        return resume
    root = Path(
        env.get("CURSOR_CONFIG_DIR")
        or (
            str(Path(env["XDG_CONFIG_HOME"]) / "cursor")
            if env.get("XDG_CONFIG_HOME")
            else str(Path(env.get("HOME", str(Path.home()))) / ".cursor")
        )
    )
    if (root / "acp-sessions" / resume / "store.db").exists():
        return resume
    cwd_hash = hashlib.md5(
        str(Path(req["cwd"]).resolve()).encode(), usedforsecurity=False
    ).hexdigest()
    source = root / "chats" / cwd_hash / resume / "store.db"
    if not source.is_file():
        return resume
    identifier = str(uuid.uuid4())
    destination = root / "acp-sessions" / identifier
    destination.mkdir(parents=True, mode=0o700)
    try:
        path = destination / "store.db"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as original:
            with sqlite3.connect(path) as target:
                original.backup(target)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return identifier


def prepare_servers(req: Json, directory: Path) -> tuple[list[Json], list[Path]]:
    if req.get("mcp_policy") == "engine-only":
        raise ValueError("Cursor ACP cannot isolate inherited MCP servers; select mcp: inherit")
    config = req["mcp_config"]
    if not isinstance(config, dict) or not isinstance(config.get("mcpServers"), dict):
        raise ValueError("Cursor MCP configuration must contain mcpServers")
    servers, markers = [], []
    for index, (name, server) in enumerate(config["mcpServers"].items()):
        if not isinstance(server, dict) or not isinstance(server.get("command"), str):
            raise ValueError("Cursor child MCP injection requires stdio servers")
        values = server.get("env", {})
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in values.items()
        ):
            raise ValueError("Cursor child MCP environment must contain string values")
        path, ready = directory / f"{index}.json", directory / f"{index}.ready"
        placeholders = {
            key: "${WISE_CURSOR_MCP_" + secrets.token_hex(16).upper() + "_" + str(index) + "}"
            for key in values
        }
        wrapper_server = {**server, "env": placeholders}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(dumps(dict(server=wrapper_server, ready=str(ready), cwd=req["cwd"])))
        env = [
            dict(name=placeholder[2:-1], value=values[key])
            for key, placeholder in placeholders.items()
        ]
        servers.append(
            dict(
                name=f"{name}-{secrets.token_hex(8)}",
                command=sys.executable,
                args=[str(Path(__file__).with_name("cursor_mcp.py")), str(path)],
                env=env,
            )
        )
        markers.append(ready)
    return servers, markers


def permission_result(params: Json, req: Json, calls: dict[str, Json]) -> Json:
    call = {
        **calls.get(str(rec(params.get("toolCall")).get("toolCallId", "")), {}),
        **rec(params.get("toolCall")),
    }
    title, kind = str(call.get("title", "")), call.get("kind")
    data = rec(call.get("rawInput"))
    name = "Unknown"
    if kind == "execute":
        name = "Bash"
        if not data and title.startswith("`") and title.endswith("`"):
            data = dict(command=title[1:-1].replace(r"\`", "`"))
    elif kind == "edit":
        diffs = [item for item in call.get("content", []) if item.get("type") == "diff"]
        if diffs:
            name, data = "Write", dict(file_path=diffs[0].get("path", ""))
    elif kind in ("read", "search"):
        name = "Read" if kind == "read" else "Grep"
    elif match := re.fullmatch(r"([\w.-]+): ([\w.-]+)", title):
        name = f"mcp__{match[1]}__{match[2]}"
        if match[1] in rec(req.get("mcp_config")).get("mcpServers", {}) and match[2] in {
            "wise_report",
            "wise_ask",
            "wise_context",
            "wise_checkpoint",
        }:
            name = "Read"
    decision = decide_permission(
        name, data, mode=req["mode"], workspace_roots=[req["cwd"], *req.get("add_dirs", [])]
    )
    wanted = "allow_once" if decision["behavior"] == "allow" else "reject_once"
    option = next(
        (item.get("optionId") for item in params.get("options", []) if item.get("kind") == wanted),
        None,
    )
    return dict(
        outcome=dict(outcome="selected", optionId=option) if option else dict(outcome="cancelled")
    )


async def start_cursor_acp(
    req: Json,
    on_event: EventCallback,
    *,
    bin: str,
    parent_env: Mapping[str, str | None] | None = None,
) -> AgentHandle:
    from .cursor import AUTH_RE, RATE_LIMIT_RE, StreamParser, child_env

    directory = Path(tempfile.mkdtemp(prefix="wise-cursor-mcp-"))

    def cleanup() -> None:
        shutil.rmtree(directory, ignore_errors=True)

    try:
        servers, markers = prepare_servers(req, directory)
        env = child_env(req, parent_env)
        resume_cursor = prepare_resume(req, env)
        argv = ["--trust", "--sandbox", "disabled" if req["mode"] == "full-access" else "enabled"]
        if req.get("model") and req["model"] != "inherit":
            argv += ["--model", req["model"]]
        for path in req.get("add_dirs", []):
            argv += ["--add-dir", path]
        proc = await spawn_clean(
            bin,
            [*argv, "acp"],
            SpawnOptions(cwd=req["cwd"], env=env, timeout_ms=req["timeout_ms"]),
        )
    except BaseException:
        cleanup()
        raise
    parser = StreamParser(pool=req["auth"], expect_json="schema" in req)
    calls: dict[str, Json] = {}
    sequence = 0
    session: str | None = None
    prompting = False
    permission_req = {
        **req,
        "mcp_config": {"mcpServers": {server["name"]: {} for server in servers}},
    }

    async def send(message: Json) -> None:
        proc.stdin.write(dumps(dict(jsonrpc="2.0", **message)) + "\n")
        await proc.stdin.drain()

    async def request(method: str, params: Json) -> Json:
        nonlocal sequence
        sequence += 1
        identifier = sequence
        await send(dict(id=identifier, method=method, params=params))
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise RuntimeError("Cursor ACP closed before completing " + method)
            message = loads(line.decode())
            if not isinstance(message, dict):
                raise ValueError("Invalid Cursor ACP response")
            normalized = message
            update = rec(rec(message.get("params")).get("update"))
            kind = update.get("sessionUpdate")
            if prompting and message.get("method") == "session/update":
                if (
                    kind == "agent_message_chunk"
                    and rec(update.get("content")).get("type") == "text"
                ):
                    normalized = dict(type="assistant", message=dict(content=[update["content"]]))
                    parser.accept(normalized)
                elif kind in ("tool_call", "tool_call_update"):
                    key = update.get("toolCallId")
                    if isinstance(key, str):
                        calls[key] = {**calls.get(key, {}), **update}
                        call = calls[key]
                        normalized = dict(
                            type="assistant",
                            message=dict(
                                content=[
                                    dict(
                                        type="tool_use",
                                        id=key,
                                        name=call.get("title", "tool"),
                                        input=rec(call.get("rawInput")),
                                    )
                                ]
                            ),
                        )
                    if kind == "tool_call":
                        parser.snap["tool_calls"] += 1
            on_event(
                dict(
                    ts=parser.now(),
                    harness="cursor",
                    line=line.decode().rstrip(),
                    parsed=normalized,
                )
            )
            if message.get("id") == identifier and "method" not in message:
                if "error" in message:
                    raise RuntimeError(
                        str(rec(message["error"]).get("message", "Cursor ACP error"))
                    )
                if not isinstance(message.get("result"), dict):
                    raise ValueError("Invalid Cursor ACP result")
                return message["result"]
            if "method" in message and "id" in message:
                if message["method"] == "session/request_permission":
                    result = permission_result(rec(message.get("params")), permission_req, calls)
                    await send(dict(id=message["id"], result=result))
                else:
                    await send(
                        dict(
                            id=message["id"],
                            error=dict(code=-32601, message="Unsupported client method"),
                        )
                    )

    async def finish() -> Json:
        nonlocal session, prompting
        error: str | None = None
        try:
            await request(
                "initialize",
                dict(
                    protocolVersion=1,
                    clientCapabilities={},
                    clientInfo=dict(name="wise-engine", version="1"),
                ),
            )
            params = dict(cwd=req["cwd"], mcpServers=servers)
            resume = resume_cursor
            if isinstance(resume, str) and resume:
                session = resume
                result = await request("session/load", dict(**params, sessionId=resume))
            else:
                result = await request("session/new", params)
                session = string(result.get("sessionId"))
            if not session:
                raise ValueError("Cursor ACP returned no session ID")
            parser.snap["session_id"] = session
            model = string(rec(result.get("models")).get("currentModelId"))
            if model:
                parser.snap["model"] = model
            if not all(marker.is_file() for marker in markers):
                raise RuntimeError("Cursor failed to initialize the injected child MCP server")
            await request(
                "session/set_mode",
                dict(
                    sessionId=session,
                    modeId="ask" if req["mode"] == "approval-required" else "agent",
                ),
            )
            prompting = True
            result = await request(
                "session/prompt",
                dict(sessionId=session, prompt=[dict(type="text", text=compose_prompt(req))]),
            )
            if result.get("stopReason") != "end_turn":
                raise RuntimeError(
                    "Cursor ACP stopped: " + str(result.get("stopReason", "unknown"))
                )
            parser.accept(dict(type="result", subtype="success", result=parser.assistant))
        except asyncio.CancelledError:
            if session:
                with suppress(BrokenPipeError, ConnectionResetError):
                    await send(dict(method="session/cancel", params=dict(sessionId=session)))
            raise
        except Exception as exc:
            error = str(exc)
            parser.snap["errors"].append(error)
        finally:
            proc.kill("SIGKILL")
            exit = await asyncio.shield(proc.exited)
            cleanup()
        from ..spawn import SpawnExit

        result = parser.finish(
            SpawnExit(
                0 if error is None else exit.code,
                exit.signal,
                exit.timed_out,
                exit.stderr,
                exit.error,
            )
        )
        if error and not exit.timed_out:
            result["exit"] = (
                "auth"
                if AUTH_RE.search(error)
                else "rate_limited"
                if RATE_LIMIT_RE.search(error)
                else "error"
            )
        if "max_turns" in req:
            result.setdefault("warnings", []).append("cursor does not support max_turns")
        if req.get("allowed_tools"):
            result.setdefault("warnings", []).append(
                "Cursor ACP does not support allowed_tools grants; Wise mode permissions apply"
            )
        if "resume" in req and not isinstance(req["resume"], str):
            result.setdefault("warnings", []).append("ignored non-string resume cursor")
        return result

    return AgentHandle(
        done=await arm_task(finish(), proc, cleanup),
        pid=proc.pid,
        kill=proc.kill,
        snapshot=lambda: copy.deepcopy(parser.snap),
    )
