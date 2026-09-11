from __future__ import annotations

import json
import os
import re
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..adapter_types import AgentHandle, EventCallback, Json
from ..host_setup import _json_clean
from ..resolve import effort_for
from ..spawn import SpawnExit, SpawnOptions, clean_env, spawn_clean
from ._common import (
    Parser,
    ProviderAdapter,
    TemporaryFile,
    arm_task,
    clip,
    dumps,
    exit_detail,
    finish_process,
    loads,
    number,
    rec,
    string,
    temporary_file,
)

GEMINI_BIN = "gemini"
GEMINI_KEY_VARS = ("GOOGLE_API_KEY", "GEMINI_API_KEY")
GEMINI_CONFIG_VAR = "GEMINI_CLI_HOME"
GEMINI_SYSTEM_SETTINGS = "GEMINI_CLI_SYSTEM_SETTINGS_PATH"
GEMINI_SYSTEM_DEFAULTS = "GEMINI_CLI_SYSTEM_DEFAULTS_PATH"
GEMINI_KEEP_VARS = (
    GEMINI_CONFIG_VAR,
    "GOOGLE_CLOUD_PROJECT",
    GEMINI_SYSTEM_SETTINGS,
    GEMINI_SYSTEM_DEFAULTS,
)
APPROVAL_MAP = {"approval-required": "default", "auto": "auto_edit", "full-access": "yolo"}
RATE_LIMIT_RE = re.compile(r"rate.?limit|429|too many requests|quota|RESOURCE_EXHAUSTED", re.I)
AUTH_RE = re.compile(
    r"error authenticating|IneligibleTier|FatalAuthenticationError|not logged in|unauthenticated|unauthorized|401|invalid api key|API key not valid|credentials|log ?in|authenticate",
    re.I,
)
TURN_LIMIT_RE = re.compile(r"FatalTurnLimitedError|turn limit|max.?turns|session turns", re.I)
PROMPT_ARGV_MAX = 100_000
SCHEMA_INSTRUCTION = "Respond with only a JSON object matching this JSON schema. No prose, no code fence, nothing before or after the object."
FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n([\s\S]*?)\n\s*```")


def prompt_via_stdin(prompt: str) -> bool:
    return len(prompt.encode("utf-8")) > PROMPT_ARGV_MAX


def compose_prompt(req: Json) -> str:
    parts = [req["system"]] if "system" in req else []
    parts.append(req["prompt"])
    if "schema" in req:
        parts.append(SCHEMA_INSTRUCTION + "\n" + dumps(req["schema"]))
    return "\n\n".join(parts)


def build_argv(req: Json) -> list[str]:
    prompt = compose_prompt(req)
    argv = [] if prompt_via_stdin(prompt) else ["-p", prompt]
    argv += [
        "--output-format",
        "stream-json",
        "--skip-trust",
        "--approval-mode",
        APPROVAL_MAP[req["mode"]],
    ]
    for directory in req.get("add_dirs", []):
        argv += ["--include-directories", directory]
    if req.get("model") and req["model"] != "inherit":
        argv += ["-m", req["model"]]
    if isinstance(req.get("resume"), str) and req["resume"]:
        argv += ["--resume", req["resume"]]
    return argv


def child_env(req: Json, parent: Mapping[str, str | None] | None = None) -> dict[str, str]:
    return clean_env(
        parent=parent,
        keep=GEMINI_KEEP_VARS,
        secrets=GEMINI_KEY_VARS if req["auth"] == "api-key" else [],
        extra=req.get("env"),
    )


def prepare_mcp(req: Json, env: dict[str, str]) -> TemporaryFile | None:
    config = req.get("mcp_config", {})
    if not isinstance(config, dict) or not isinstance(config.get("mcpServers", {}), dict):
        raise ValueError("gemini: mcp_config must contain an MCP server mapping")
    servers = config.get("mcpServers", {})
    if not servers:
        return None
    for server in servers.values():
        if (
            not isinstance(server, dict)
            or not isinstance(server.get("command"), str)
            or not server["command"]
            or not isinstance(server.get("args", []), list)
            or not all(isinstance(arg, str) for arg in server.get("args", []))
            or not isinstance(server.get("env", {}), dict)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in server.get("env", {}).items()
            )
        ):
            raise ValueError(
                "gemini: child MCP requires a stdio command, string arguments and string environment"
            )
    default = (
        "/Library/Application Support/GeminiCli/settings.json"
        if sys.platform == "darwin"
        else "/etc/gemini-cli/settings.json"
    )
    original = Path(env.get(GEMINI_SYSTEM_SETTINGS, default)).expanduser().absolute()
    try:
        settings = json.loads(_json_clean(original.read_text()))
    except FileNotFoundError:
        settings = {}
    if not isinstance(settings, dict) or not isinstance(settings.get("mcpServers", {}), dict):
        raise ValueError("gemini: system settings must contain a valid MCP server mapping")
    policy = settings.get("mcp", {})
    if isinstance(policy, dict) and policy.get("allowed") is not None:
        raise ValueError("gemini: system MCP allowlist prevents injecting a per-step server")
    merged = dict(settings.get("mcpServers", {}))
    for server in servers.values():
        alias = "wise-step-" + uuid.uuid4().hex
        values = {}
        for index, (key, value) in enumerate(server.get("env", {}).items()):
            name = "WISE_MCP_" + uuid.uuid4().hex.upper() + "_" + str(index)
            env[name] = value
            values[key] = "${" + name + "}"
        merged[alias] = {**server, "env": values, "trust": True}
    overlay = temporary_file(
        "wise-gemini-mcp-", "settings.json", dumps({**settings, "mcpServers": merged})
    )
    try:
        Path(overlay.path).chmod(0o600)
    except BaseException:
        overlay.cleanup()
        raise
    env[GEMINI_SYSTEM_SETTINGS] = overlay.path
    # Keep defaults at their original location when the override file moves.
    env.setdefault(GEMINI_SYSTEM_DEFAULTS, str(original.with_name("system-defaults.json")))
    return overlay


def effort_map(effort: str) -> str | None:
    return effort_for("gemini", effort)


def extract_json(text: str) -> Json:
    try:
        return dict(ok=True, json=loads(text.strip()))
    except ValueError:
        pass
    fence = FENCE_RE.search(text)
    if fence:
        try:
            return dict(
                ok=True,
                json=loads(fence[1].strip()),
                warning="JSON extracted from a fenced code block",
            )
        except ValueError:
            pass
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        index = start
        end = -1
        while index < len(text):
            char = text[index]
            if in_string:
                if char == "\\":
                    index += 1
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
            index += 1
        if end == -1:
            break
        try:
            return dict(
                ok=True,
                json=loads(text[start:end]),
                warning="JSON extracted from surrounding prose",
            )
        except ValueError:
            start = text.find("{", start + 1)
    return dict(ok=False, error=f"final assistant text is not JSON: {clip(text) or '(empty)'}")


class StreamParser(Parser):
    harness = "gemini"

    def __init__(self, *, expect_json: bool = False, **opts: Any) -> None:
        super().__init__(**opts)
        self.expect_json = expect_json
        self.snap = dict(segments=0, tool_uses=0, result=False, errors=[], warnings=[])
        self.segments: list[str] = []
        self.current = ""
        self.stats: Json = {}
        self.status: str | None = None
        self.stdout = ""

    def close_segment(self) -> None:
        if self.current.strip():
            self.segments.append(self.current)
            self.snap["segments"] = len(self.segments)
        self.current = ""

    def feed(self, chunk: str | bytes) -> list[Json]:
        text = self.decode_chunk(chunk)
        self.stdout += text
        return super().feed(text)

    def accept(self, parsed: Json) -> None:
        kind = parsed.get("type")
        if kind == "init":
            for name in ("session_id", "model"):
                if isinstance(parsed.get(name), str):
                    self.snap[name] = parsed[name]
        elif kind == "message" and parsed.get("role") == "assistant":
            self.current += string(parsed.get("content")) or ""
        elif kind == "tool_use":
            self.snap["tool_uses"] += 1
        elif kind == "tool_result":
            self.close_segment()
        elif kind == "error":
            error = parsed.get("error")
            if isinstance(error, dict):
                message = string(error.get("message"))
                if message is None:
                    message = dumps(error)
                code = f" (code {error['code']})" if "code" in error else ""
                self.snap["errors"].append(
                    f"{error['type']}: {message}{code}"
                    if isinstance(error.get("type"), str) and error["type"]
                    else message + code
                )
                if isinstance(parsed.get("stats"), dict):
                    self.stats = parsed["stats"]
            else:
                message = string(parsed.get("message"))
                self.snap["errors" if parsed.get("severity") == "error" else "warnings"].append(
                    message if message is not None else "gemini error event"
                )
        elif kind == "result":
            self.snap["result"] = True
            self.status = string(parsed.get("status"))
            if isinstance(parsed.get("stats"), dict):
                self.stats = parsed["stats"]

    def classify(self, exit: SpawnExit) -> Json:
        if exit.timed_out:
            return dict(exit="timeout", error=clip(exit.stderr) or "timed out")
        if self.snap["result"] and self.status == "success":
            return dict(exit="ok")
        errors = self.snap["errors"]
        haystack = "\n".join(errors) + "\n" + exit.stderr
        detail = (
            errors[-1]
            if errors
            else exit.error
            if exit.error is not None
            else clip(exit.stderr)
            if exit.stderr.strip()
            else f"no result event (stdout: {clip(self.stdout)})"
            if self.stdout.strip()
            else exit_detail(exit, "no result event")
        )
        kind = (
            "max_turns"
            if exit.code == 53 or TURN_LIMIT_RE.search(haystack)
            else "auth"
            if exit.code == 41 or AUTH_RE.search(haystack)
            else "rate_limited"
            if RATE_LIMIT_RE.search(haystack)
            else "error"
        )
        return dict(exit=kind, error=clip(detail))

    def finish(self, exit: SpawnExit) -> Json:
        self.flush()
        self.close_segment()
        usage = dict(
            input=number(self.stats.get("input_tokens")) or 0,
            output=number(self.stats.get("output_tokens")) or 0,
            cache_read=number(self.stats.get("cached")) or 0,
            cache_write=0,
            pool=self.pool,
        )
        res = dict(
            text=self.segments[-1] if self.segments else "", usage=usage, **self.classify(exit)
        )
        if "session_id" in self.snap:
            res["cursor"] = self.snap["session_id"]
        warnings = list(self.snap["warnings"])
        if self.expect_json and res["exit"] == "ok":
            extracted = extract_json(res["text"])
            if extracted["ok"]:
                res["json"] = extracted["json"]
                if "warning" in extracted:
                    warnings.append(extracted["warning"])
            else:
                res.update(exit="error", error=extracted["error"])
        if warnings:
            res["warnings"] = warnings
        return res


def create_stream_parser(**opts: Any) -> StreamParser:
    return StreamParser(**opts)


async def start_gemini(
    req: Json,
    on_event: EventCallback,
    *,
    bin: str = GEMINI_BIN,
    parent_env: Mapping[str, str | None] | None = None,
) -> AgentHandle:
    env = child_env(req, parent_env)
    overlay = prepare_mcp(req, env)
    try:
        proc = await spawn_clean(
            bin,
            build_argv(req),
            SpawnOptions(cwd=req["cwd"], env=env, timeout_ms=req["timeout_ms"]),
        )
    except BaseException:
        if overlay:
            overlay.cleanup()
        raise
    prompt = compose_prompt(req)
    proc.stdin.end(prompt if prompt_via_stdin(prompt) else "")
    parser = StreamParser(pool=req["auth"], expect_json="schema" in req)

    async def finish() -> Json:
        res = await finish_process(proc, parser, on_event, overlay.cleanup if overlay else None)
        if "resume" in req and not isinstance(req["resume"], str):
            res["warnings"] = [*res.get("warnings", []), "ignored non-string resume cursor"]
        return res

    return AgentHandle(
        done=await arm_task(finish(), proc, overlay.cleanup if overlay else None),
        pid=proc.pid,
        kill=proc.kill,
        snapshot=parser.snapshot,
    )


def auth_file_path(env: Mapping[str, str | None] | None = None, home: str | None = None) -> str:
    env = os.environ if env is None else env
    root = env.get(GEMINI_CONFIG_VAR)
    if root is None:
        root = home if home is not None else env.get("HOME")
    return str(Path(root if root is not None else Path.home()) / ".gemini" / "oauth_creds.json")


async def probe_auth(
    auth: str, *, parent_env: Mapping[str, str | None] | None = None, home: str | None = None
) -> Json:
    env = os.environ if parent_env is None else parent_env
    if auth == "api-key":
        return dict(
            ok=any(env.get(name) for name in GEMINI_KEY_VARS), login_cmd="export GEMINI_API_KEY=..."
        )
    try:
        parsed = rec(loads(Path(auth_file_path(env, home)).read_text(encoding="utf-8")))
        ok = bool(string(parsed.get("access_token")) or string(parsed.get("refresh_token")))
    except (OSError, ValueError):
        ok = False
    return dict(ok=ok, login_cmd="gemini (interactive, then /auth)")


gemini_adapter = ProviderAdapter("gemini", GEMINI_BIN, start_gemini, probe_auth, effort_map)
