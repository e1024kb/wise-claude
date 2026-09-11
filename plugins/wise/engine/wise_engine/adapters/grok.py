from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import tomlkit

from ..adapter_types import AgentHandle, EventCallback, Json
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
    rec,
    string,
    temporary_file,
    token_usage,
)

GROK_BIN = "grok"
GROK_KEY_VAR = "XAI_API_KEY"
GROK_CONFIG_VAR = "GROK_HOME"
PERMISSION_MAP = {
    "approval-required": ["--permission-mode", "dontAsk"],
    "auto": ["--permission-mode", "acceptEdits"],
    "full-access": ["--always-approve"],
}
RATE_LIMIT_RE = re.compile(r"rate.?limit|429|too many requests|quota exceeded", re.I)
AUTH_RE = re.compile(
    r"GROK_AUTH_EXPIRED|not logged in|grok login|unauthorized|401|re-?authenticat|token expired|sign in",
    re.I,
)
PROMPT_ARGV_MAX = 100_000


def prompt_via_file(prompt: str) -> bool:
    return len(prompt.encode("utf-8")) > PROMPT_ARGV_MAX


def build_argv(req: Json, *, prompt_path: str | None = None) -> list[str]:
    if prompt_via_file(req["prompt"]):
        if prompt_path is None:
            raise ValueError("grok: large prompt without promptPath")
        argv = ["--prompt-file", prompt_path]
    else:
        argv = ["-p", req["prompt"]]
    argv += [
        "--output-format",
        "streaming-json",
        "--no-auto-update",
        "--cwd",
        req["cwd"],
        *PERMISSION_MAP[req["mode"]],
    ]
    for rule in req.get("allowed_tools", []):
        argv += ["--allow", rule]
    if req.get("model") and req["model"] != "inherit":
        argv += ["-m", req["model"]]
    effort = effort_map(req["effort"]) if "effort" in req else None
    if effort is not None:
        argv += ["--reasoning-effort", effort]
    if "schema" in req:
        argv += ["--json-schema", dumps(req["schema"])]
    if "max_turns" in req:
        argv += ["--max-turns", str(req["max_turns"])]
    if isinstance(req.get("resume"), str) and req["resume"]:
        argv += ["--resume", req["resume"]]
    if "system" in req:
        argv += ["--rules", req["system"]]
    return argv


def write_prompt_file(prompt: str) -> TemporaryFile:
    return temporary_file("wise-grok-", "prompt.md", prompt)


def child_env(req: Json, parent: Mapping[str, str | None] | None = None) -> dict[str, str]:
    return clean_env(
        parent=parent,
        keep=[GROK_CONFIG_VAR, "GROK_AUTH_PATH"],
        secrets=[GROK_KEY_VAR] if req["auth"] == "api-key" else [],
        extra=req.get("env"),
    )


@dataclass
class McpOverlay:
    config: TemporaryFile
    env: dict[str, str]
    allowed_tools: list[str]

    def cleanup(self) -> None:
        self.config.cleanup()


def prepare_mcp_overlay(req: Json, env: dict[str, str]) -> McpOverlay | None:
    servers = (req.get("mcp_config") or {}).get("mcpServers", {})
    if not servers:
        return None
    if req.get("mcp_policy") == "engine-only":
        raise ValueError(
            "grok: engine-only MCP policy is unsupported; project MCP servers cannot be isolated"
        )
    source = (
        Path(env.get(GROK_CONFIG_VAR) or str(Path(env.get("HOME", str(Path.home()))) / ".grok"))
        .expanduser()
        .resolve()
    )
    try:
        text = (source / "config.toml").read_text() if (source / "config.toml").exists() else ""
        config = tomlkit.parse(text)
    except (OSError, ValueError):
        raise ValueError("grok: cannot read user config.toml for child MCP") from None
    temporary = temporary_file("wise-grok-mcp-", "config.toml", "")
    directory = Path(temporary.path).parent
    try:
        Path(temporary.path).chmod(0o600)
        source.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Shared provider storage keeps resume cursors and token refreshes valid after cleanup.
        (source / "sessions").mkdir(exist_ok=True, mode=0o700)
        for path in source.iterdir():
            if path.name in ("config.toml", "auth.json") or path.name.endswith((".sock", ".lock")):
                continue
            (directory / path.name).symlink_to(path, target_is_directory=path.is_dir())
        if "mcp_servers" not in config:
            config["mcp_servers"] = tomlkit.table()
        allowed: list[str] = []
        process_env = dict(env)
        for name, server in servers.items():
            alias = "wise-child-" + uuid.uuid4().hex
            entry = {key: value for key, value in server.items() if key != "type"}
            if server.get("env"):
                injected_env = {}
                for index, (key, value) in enumerate(server["env"].items()):
                    variable = f"WISE_GROK_MCP_{uuid.uuid4().hex.upper()}_{index}"
                    process_env[variable] = value
                    injected_env[key] = "${" + variable + "}"
                entry["env"] = injected_env
            config["mcp_servers"][alias] = entry
            if name == "wise-engine" and (server.get("env") or {}).get("WISE_STEP_TOKEN"):
                allowed.extend(
                    f"MCPTool({alias}__{tool})"
                    for tool in ("wise_report", "wise_ask", "wise_context", "wise_checkpoint")
                )
        Path(temporary.path).write_text(tomlkit.dumps(config), encoding="utf-8")
        return McpOverlay(
            temporary,
            {
                **process_env,
                GROK_CONFIG_VAR: str(directory),
                "GROK_AUTH_PATH": env.get("GROK_AUTH_PATH") or str(source / "auth.json"),
            },
            allowed,
        )
    except BaseException:
        temporary.cleanup()
        raise


def effort_map(effort: str) -> str | None:
    return effort_for("grok", effort)


def _looks_like_result(value: Any) -> bool:
    return isinstance(value, dict) and (
        value.get("type") == "end"
        or any(key in value for key in ("text", "sessionId", "usage", "structuredOutput", "error"))
    )


class StreamParser(Parser):
    harness = "grok"

    def __init__(self, **opts: Any) -> None:
        super().__init__(**opts)
        self.snap = dict(lines=0, result=False)
        self.buffer = ""
        self.last_line_result: Json | None = None
        self.text_deltas: list[str] = []

    def feed(self, chunk: str | bytes) -> list[Json]:
        text = self.decode_chunk(chunk)
        self.buffer += text
        return super().feed(text)

    def ingest(self, line: str) -> Json:
        self.snap["lines"] += 1
        return super().ingest(line)

    def accept(self, parsed: Json) -> None:
        if parsed.get("type") == "text" and isinstance(parsed.get("data"), str):
            self.text_deltas.append(parsed["data"])
        if _looks_like_result(parsed):
            self.last_line_result = parsed
            if isinstance(parsed.get("sessionId"), str):
                self.snap["session_id"] = parsed["sessionId"]

    def read_result(self) -> Json | None:
        try:
            value = loads(self.buffer.strip())
            if _looks_like_result(value):
                return value
        except ValueError:
            pass
        return self.last_line_result

    def classify(self, exit: SpawnExit, result: Json | None) -> Json:
        if exit.timed_out:
            return dict(exit="timeout", error=clip(exit.stderr) or "timed out")
        if result is not None:
            if re.search(r"max.?turns", string(result.get("stopReason")) or "", re.I):
                return dict(exit="max_turns", error="max turns reached")
            if result.get("is_error") is True or result.get("isError") is True:
                failure = string(result.get("text"))
                if failure is None:
                    failure = string(result.get("error"))
                if failure is None:
                    failure = "grok reported an error"
            elif isinstance(result.get("error"), dict):
                failure = string(result["error"].get("message"))
                if failure is None:
                    failure = dumps(result["error"])
            else:
                failure = string(result.get("error"))
            if failure is None:
                return dict(exit="ok")
            haystack, error = failure + "\n" + exit.stderr, clip(failure)
        else:
            haystack = exit.stderr + "\n" + self.buffer
            error = clip(exit.stderr or self.buffer)
        kind = (
            "rate_limited"
            if RATE_LIMIT_RE.search(haystack)
            else "auth"
            if AUTH_RE.search(haystack)
            else "error"
        )
        if result is None and kind == "error":
            error = (
                exit.error
                if exit.error is not None
                else clip(exit.stderr)
                if exit.stderr.strip()
                else f"no JSON result (stdout: {clip(self.buffer)})"
                if self.buffer.strip()
                else exit_detail(exit, "no JSON result")
            )
        return dict(exit=kind, error=error)

    def finish(self, exit: SpawnExit) -> Json:
        self.flush()
        result = self.read_result()
        if result is not None:
            self.snap["result"] = True
            if isinstance(result.get("sessionId"), str):
                self.snap["session_id"] = result["sessionId"]
            models = list(rec(result.get("modelUsage")))
            if models:
                self.snap["model"] = models[0]
        text = string(rec(result).get("text"))
        res = dict(
            text="".join(self.text_deltas) if text is None else text,
            usage=token_usage(result, self.pool),
            **self.classify(exit, result),
        )
        if result is not None and "structuredOutput" in result:
            res["json"] = result["structuredOutput"]
        if "session_id" in self.snap:
            res["cursor"] = self.snap["session_id"]
        return res


def create_stream_parser(**opts: Any) -> StreamParser:
    return StreamParser(**opts)


async def start_grok(
    req: Json,
    on_event: EventCallback,
    *,
    bin: str = GROK_BIN,
    parent_env: Mapping[str, str | None] | None = None,
) -> AgentHandle:
    prompt_file = write_prompt_file(req["prompt"]) if prompt_via_file(req["prompt"]) else None
    overlay = None

    def cleanup() -> None:
        if prompt_file:
            prompt_file.cleanup()
        if overlay:
            overlay.cleanup()

    try:
        env = child_env(req, parent_env)
        overlay = prepare_mcp_overlay(req, env)
        argv = build_argv(req, prompt_path=prompt_file.path if prompt_file else None)
        if overlay:
            argv += ["--leader-socket", str(Path(overlay.config.path).parent / "leader.sock")]
            for rule in overlay.allowed_tools:
                argv += ["--allow", rule]
        proc = await spawn_clean(
            bin,
            argv,
            SpawnOptions(
                cwd=req["cwd"], env=overlay.env if overlay else env, timeout_ms=req["timeout_ms"]
            ),
        )
    except BaseException:
        cleanup()
        raise
    proc.stdin.end()
    parser = StreamParser(pool=req["auth"])

    async def finish() -> Json:
        res = await finish_process(proc, parser, on_event, cleanup)
        if "resume" in req and not isinstance(req["resume"], str):
            res["warnings"] = [*res.get("warnings", []), "ignored non-string resume cursor"]
        return res

    return AgentHandle(
        done=await arm_task(finish(), proc, cleanup),
        pid=proc.pid,
        kill=proc.kill,
        snapshot=parser.snapshot,
    )


def auth_file_path(env: Mapping[str, str | None] | None = None, home: str | None = None) -> str:
    env = os.environ if env is None else env
    if env.get("GROK_AUTH_PATH"):
        return str(env["GROK_AUTH_PATH"])
    root = env.get(GROK_CONFIG_VAR)
    if root is None:
        base = home if home is not None else env.get("HOME")
        root = str(Path(base if base is not None else Path.home()) / ".grok")
    return str(Path(root) / "auth.json")


async def probe_auth(
    auth: str, *, parent_env: Mapping[str, str | None] | None = None, home: str | None = None
) -> Json:
    env = os.environ if parent_env is None else parent_env
    if auth == "api-key":
        return dict(ok=bool(env.get(GROK_KEY_VAR)), login_cmd="export XAI_API_KEY=...")
    try:
        parsed = loads(Path(auth_file_path(env, home)).read_text(encoding="utf-8"))
        ok = isinstance(parsed, dict) and bool(parsed)
    except (OSError, ValueError):
        ok = False
    return dict(ok=ok, login_cmd="grok login")


grok_adapter = ProviderAdapter("grok", GROK_BIN, start_grok, probe_auth, effort_map)
