from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from ..adapter_types import AgentHandle, EventCallback, Json
from ..resolve import effort_for
from ..spawn import SpawnExit, SpawnOptions, clean_env, spawn_clean
from ._common import (
    Parser,
    ProviderAdapter,
    arm_task,
    clip,
    dumps,
    empty_usage,
    exit_detail,
    finish_process,
    loads,
    probe_process,
    rec,
    string,
)
from .gemini import compose_prompt, extract_json

CURSOR_BIN = "cursor-agent"
CURSOR_KEY_VAR = "CURSOR_API_KEY"
CURSOR_KEEP_VARS = ("CURSOR_CONFIG_DIR", "CURSOR_API_ENDPOINT")
RATE_LIMIT_RE = re.compile(r"rate.?limit|429|too many requests|usage limit|quota|exhausted", re.I)
AUTH_RE = re.compile(
    r"authentication required|not authenticated|not logged in|unauthenticated|unauthorized|401|invalid api key|token expired|agent login",
    re.I,
)
MODE_ARGS = {
    "approval-required": ["--mode", "ask", "--sandbox", "enabled"],
    "auto": ["--force", "--sandbox", "enabled"],
    "full-access": ["--force", "--sandbox", "disabled", "--approve-mcps"],
}


def build_argv(req: Json) -> list[str]:
    argv = [
        "--print",
        "--output-format",
        "stream-json",
        "--stream-partial-output",
        "--trust",
        "--workspace",
        req["cwd"],
        *MODE_ARGS[req["mode"]],
    ]
    if req.get("model") and req["model"] != "inherit":
        argv += ["--model", req["model"]]
    if isinstance(req.get("resume"), str) and req["resume"]:
        argv += ["--resume", req["resume"]]
    for directory in req.get("add_dirs", []):
        argv += ["--add-dir", directory]
    return argv


def child_env(req: Json, parent: Mapping[str, str | None] | None = None) -> dict[str, str]:
    return clean_env(
        parent=parent,
        keep=CURSOR_KEEP_VARS,
        secrets=[CURSOR_KEY_VAR] if req["auth"] == "api-key" else [],
        extra=req.get("env"),
    )


def effort_map(effort: str) -> str | None:
    return effort_for("cursor", effort)


class StreamParser(Parser):
    harness = "cursor"

    def __init__(self, *, expect_json: bool = False, **opts: Any) -> None:
        super().__init__(**opts)
        self.expect_json = expect_json
        self.snap = dict(assistant_events=0, tool_calls=0, results=0, errors=[])
        self.assistant = ""
        self.terminal: Json | None = None

    def accept(self, parsed: Json) -> None:
        kind = parsed.get("type")
        if kind == "system" and parsed.get("subtype") == "init":
            for name in ("session_id", "model"):
                if isinstance(parsed.get(name), str):
                    self.snap[name] = parsed[name]
        elif kind == "assistant":
            self.snap["assistant_events"] += 1
            parts = rec(parsed.get("message")).get("content")
            if isinstance(parts, list):
                self.assistant += "".join(
                    string(part.get("text")) or ""
                    for part in parts
                    if isinstance(part, dict) and part.get("type") == "text"
                )
        elif kind == "tool_call" and parsed.get("subtype") == "started":
            self.snap["tool_calls"] += 1
        elif kind == "result":
            self.terminal = parsed
            self.snap["results"] += 1
            if isinstance(parsed.get("session_id"), str):
                self.snap["session_id"] = parsed["session_id"]
            if parsed.get("is_error") is True or parsed.get("subtype") != "success":
                direct = string(parsed.get("error"))
                if direct is None:
                    direct = string(parsed.get("result"))
                self.snap["errors"].append(
                    direct or f"cursor result subtype {parsed.get('subtype', 'undefined')}"
                )
        elif kind == "error":
            message = string(parsed.get("message"))
            if message is None:
                message = string(parsed.get("error"))
            self.snap["errors"].append(message if message is not None else dumps(parsed))

    def classify(self, exit: SpawnExit) -> Json:
        if exit.timed_out:
            return dict(exit="timeout", error=clip(exit.stderr) or "timed out")
        errors = self.snap["errors"]
        if errors or self.terminal is None or exit.code not in (0, None):
            haystack = "\n".join(errors) + "\n" + exit.stderr
            detail = (
                errors[-1]
                if errors
                else exit.error
                if exit.error is not None
                else clip(exit.stderr)
                if exit.stderr.strip()
                else exit_detail(exit, "no successful result event")
            )
            kind = (
                "rate_limited"
                if RATE_LIMIT_RE.search(haystack)
                else "auth"
                if AUTH_RE.search(haystack)
                else "error"
            )
            return dict(exit=kind, error=clip(detail))
        return dict(exit="ok")

    def finish(self, exit: SpawnExit) -> Json:
        self.flush()
        text = string(rec(self.terminal).get("result"))
        res = dict(
            text=self.assistant if text is None else text,
            usage=empty_usage(self.pool),
            **self.classify(exit),
        )
        if "session_id" in self.snap:
            res["cursor"] = self.snap["session_id"]
        if "model" in self.snap:
            res["model"] = self.snap["model"]
        if self.expect_json and res["exit"] == "ok":
            extracted = extract_json(res["text"])
            if extracted["ok"]:
                res["json"] = extracted["json"]
                if extracted.get("warning"):
                    res["warnings"] = [extracted["warning"]]
            else:
                res.update(exit="error", error=extracted["error"])
        return res


def create_stream_parser(**opts: Any) -> StreamParser:
    return StreamParser(**opts)


async def start_cursor(
    req: Json,
    on_event: EventCallback,
    *,
    bin: str = CURSOR_BIN,
    parent_env: Mapping[str, str | None] | None = None,
) -> AgentHandle:
    proc = await spawn_clean(
        bin,
        build_argv(req),
        SpawnOptions(cwd=req["cwd"], env=child_env(req, parent_env), timeout_ms=req["timeout_ms"]),
    )
    proc.stdin.end(compose_prompt(req))
    parser = StreamParser(pool=req["auth"], expect_json="schema" in req)

    async def finish() -> Json:
        res = await finish_process(proc, parser, on_event)
        warnings = list(res.get("warnings", []))
        if "resume" in req and not isinstance(req["resume"], str):
            warnings.append("ignored non-string resume cursor")
        if "max_turns" in req:
            warnings.append("cursor does not support max_turns")
        if warnings:
            res["warnings"] = warnings
        return res

    return AgentHandle(
        done=await arm_task(finish(), proc, None),
        pid=proc.pid,
        kill=proc.kill,
        snapshot=parser.snapshot,
    )


async def probe_auth(
    auth: str, *, bin: str = CURSOR_BIN, parent_env: Mapping[str, str | None] | None = None
) -> Json:
    if auth == "api-key":
        return dict(
            ok=bool((os.environ if parent_env is None else parent_env).get(CURSOR_KEY_VAR)),
            login_cmd=f"export {CURSOR_KEY_VAR}=...",
        )
    exit, stdout = await probe_process(
        bin, ["status", "--format", "json"], child_env(dict(auth=auth), parent_env)
    )
    try:
        ok = (
            exit.code == 0
            and not exit.timed_out
            and rec(loads(stdout)).get("isAuthenticated") is True
        )
    except ValueError:
        ok = False
    return dict(ok=ok, login_cmd="cursor-agent login")


cursor_adapter = ProviderAdapter("cursor", CURSOR_BIN, start_cursor, probe_auth, effort_map)
