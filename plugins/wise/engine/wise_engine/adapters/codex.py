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
    TemporaryFile,
    arm_task,
    clip,
    dumps,
    exit_detail,
    finish_process,
    loads,
    number,
    probe_process,
    string,
    temporary_file,
)

CODEX_BIN = "codex"
CODEX_KEY_VAR = "OPENAI_API_KEY"
CODEX_CONFIG_VAR = "CODEX_HOME"
SANDBOX_MAP = {
    "approval-required": "read-only",
    "auto": "workspace-write",
    "full-access": "danger-full-access",
}
RATE_LIMIT_RE = re.compile(r"rate.?limit|429|too many requests|usage limit|quota", re.I)
AUTH_RE = re.compile(
    r"not logged in|codex login|unauthorized|401|invalid api key|authentication failed|token expired",
    re.I,
)
LOGGED_IN_RE = re.compile(r"logged in", re.I)
PROMPT_ARGV_MAX = 100_000


def compose_prompt(req: Json) -> str:
    return f"{req['system']}\n\n{req['prompt']}" if "system" in req else req["prompt"]


def prompt_via_stdin(prompt: str) -> bool:
    return len(prompt.encode("utf-8")) > PROMPT_ARGV_MAX


def build_argv(req: Json, *, schema_path: str | None = None) -> list[str]:
    resume = req.get("resume") if isinstance(req.get("resume"), str) and req["resume"] else None
    argv = ["exec"] if resume is None else ["exec", "resume", resume]
    argv += ["--json", "--skip-git-repo-check"]
    if resume is None:
        argv += ["-C", req["cwd"], "-s", SANDBOX_MAP[req["mode"]]]
        for directory in req.get("add_dirs", []):
            argv += ["--add-dir", directory]
    else:
        argv += ["-c", f"sandbox_mode={dumps(SANDBOX_MAP[req['mode']])}"]
        if req.get("add_dirs"):
            argv += ["-c", f"sandbox_workspace_write.writable_roots={dumps(req['add_dirs'])}"]
    argv += ["-c", 'approval_policy="never"']
    effort = effort_map(req["effort"]) if "effort" in req else None
    if effort is not None:
        argv += ["-c", f"model_reasoning_effort={dumps(effort)}"]
    if req.get("model") and req["model"] != "inherit":
        argv += ["-m", req["model"]]
    if "schema" in req:
        if schema_path is None:
            raise ValueError("codex: schema given without schemaPath")
        argv += ["--output-schema", schema_path]
    prompt = compose_prompt(req)
    return [*argv, "-" if prompt_via_stdin(prompt) else prompt]


def strict_schema(schema: Json) -> Json:
    def tighten(node: Any) -> Any:
        if isinstance(node, list):
            return [tighten(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {}
        for key, value in node.items():
            if key in ("properties", "$defs", "definitions"):
                out[key] = (
                    {name: tighten(spec) for name, spec in value.items()}
                    if isinstance(value, dict)
                    else value
                )
            elif key in ("items", "anyOf", "oneOf", "allOf", "not"):
                out[key] = tighten(value)
            else:
                out[key] = value
        props = out.get("properties")
        if out.get("type") == "object" or isinstance(props, dict):
            out.setdefault("additionalProperties", False)
            if isinstance(props, dict):
                required = (
                    {item for item in out.get("required", []) if isinstance(item, str)}
                    if isinstance(out.get("required", []), list)
                    else set()
                )
                for name, spec in props.items():
                    if name in required or not isinstance(spec, dict):
                        continue
                    kind = spec.get("type")
                    if isinstance(kind, str) and kind != "null":
                        props[name] = {**spec, "type": [kind, "null"]}
                    elif isinstance(kind, list) and "null" not in kind:
                        props[name] = {**spec, "type": [*kind, "null"]}
                out["required"] = list(props)
        return out

    return tighten(schema)


def write_schema_file(schema: Json) -> TemporaryFile:
    return temporary_file("wise-codex-", "schema.json", dumps(schema))


def child_env(req: Json, parent: Mapping[str, str | None] | None = None) -> dict[str, str]:
    return clean_env(
        parent=parent,
        keep=[CODEX_CONFIG_VAR],
        secrets=[CODEX_KEY_VAR] if req["auth"] == "api-key" else [],
        extra=req.get("env"),
    )


def effort_map(effort: str) -> str | None:
    return effort_for("codex", effort)


class StreamParser(Parser):
    harness = "codex"

    def __init__(self, *, expect_json: bool = False, **opts: Any) -> None:
        super().__init__(**opts)
        self.expect_json = expect_json
        self.snap = dict(turns=0, completed=0, commands=[], file_changes=0, errors=[], warnings=[])
        self.last_agent_text = ""
        self.usage: Json = {}

    def accept(self, parsed: Json) -> None:
        kind = parsed.get("type")
        if kind == "thread.started" and isinstance(parsed.get("thread_id"), str):
            self.snap["thread_id"] = parsed["thread_id"]
        elif kind == "turn.started":
            self.snap["turns"] += 1
        elif kind == "turn.completed":
            self.snap["completed"] += 1
            if isinstance(parsed.get("usage"), dict):
                self.usage = parsed["usage"]
        elif kind in ("turn.failed", "error"):
            error = parsed.get("error")
            text = string(error.get("message")) if isinstance(error, dict) else None
            if text is None:
                text = dumps(error) if isinstance(error, dict) else string(parsed.get("message"))
            if text is None:
                text = string(error)
            self.snap["errors"].append(text if text is not None else f"codex {kind}")
        elif kind == "item.completed" and isinstance(parsed.get("item"), dict):
            item = parsed["item"]
            if item.get("type") == "agent_message":
                text = string(item.get("text"))
                if text:
                    self.last_agent_text = text
            elif item.get("type") == "command_execution":
                self.snap["commands"].append(
                    string(item.get("command")) if isinstance(item.get("command"), str) else "?"
                )
            elif item.get("type") == "file_change":
                self.snap["file_changes"] += (
                    len(item["changes"]) if isinstance(item.get("changes"), list) else 1
                )
            elif item.get("type") == "error":
                self.snap["warnings"].append(
                    string(item.get("message"))
                    if isinstance(item.get("message"), str)
                    else "codex item warning"
                )

    def classify(self, exit: SpawnExit) -> Json:
        if exit.timed_out:
            return dict(exit="timeout", error=clip(exit.stderr) or "timed out")
        errors, warnings = self.snap["errors"], self.snap["warnings"]
        if errors or not self.snap["completed"] or exit.code not in (None, 0):
            haystack = (
                "\n".join(errors) + "\n" + exit.stderr + "\n" + (exit.error or "")
            ).strip() or "\n".join(warnings)
            detail = (
                errors[-1]
                if errors
                else exit.error
                if exit.error is not None
                else clip(exit.stderr)
                if exit.stderr.strip()
                else warnings[-1]
                if warnings
                else exit_detail(exit, "no turn.completed event")
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
        usage = dict(
            input=number(self.usage.get("input_tokens")) or 0,
            output=number(self.usage.get("output_tokens")) or 0,
            cache_read=number(self.usage.get("cached_input_tokens")) or 0,
            cache_write=number(self.usage.get("cache_write_input_tokens")) or 0,
            pool=self.pool,
        )
        res = dict(text=self.last_agent_text, usage=usage, **self.classify(exit))
        if "thread_id" in self.snap:
            res["cursor"] = self.snap["thread_id"]
        if self.snap["warnings"]:
            res["warnings"] = list(self.snap["warnings"])
        if self.expect_json and res["exit"] == "ok":
            try:
                res["json"] = loads(self.last_agent_text)
            except ValueError:
                res.update(
                    exit="error",
                    error=f"final agent message is not JSON: {clip(self.last_agent_text) or '(empty)'}",
                )
        return res


def create_stream_parser(**opts: Any) -> StreamParser:
    return StreamParser(**opts)


async def start_codex(
    req: Json,
    on_event: EventCallback,
    *,
    bin: str = CODEX_BIN,
    parent_env: Mapping[str, str | None] | None = None,
) -> AgentHandle:
    schema = write_schema_file(strict_schema(req["schema"])) if "schema" in req else None
    try:
        argv = build_argv(req, schema_path=schema.path if schema else None)
        proc = await spawn_clean(
            bin,
            argv,
            SpawnOptions(
                cwd=req["cwd"], env=child_env(req, parent_env), timeout_ms=req["timeout_ms"]
            ),
        )
    except BaseException:
        if schema:
            schema.cleanup()
        raise
    prompt = compose_prompt(req)
    proc.stdin.end(prompt if prompt_via_stdin(prompt) else "")
    parser = StreamParser(pool=req["auth"], expect_json="schema" in req)

    async def finish() -> Json:
        res = await finish_process(proc, parser, on_event, schema.cleanup if schema else None)
        if "resume" in req and not isinstance(req["resume"], str):
            res["warnings"] = [*res.get("warnings", []), "ignored non-string resume cursor"]
        return res

    return AgentHandle(
        done=await arm_task(finish(), proc, schema.cleanup if schema else None),
        pid=proc.pid,
        kill=proc.kill,
        snapshot=parser.snapshot,
    )


async def probe_auth(
    auth: str, *, bin: str = CODEX_BIN, parent_env: Mapping[str, str | None] | None = None
) -> Json:
    if auth == "api-key":
        return dict(
            ok=bool((os.environ if parent_env is None else parent_env).get(CODEX_KEY_VAR)),
            login_cmd=f"export {CODEX_KEY_VAR}=...",
        )
    exit, stdout = await probe_process(
        bin, ["login", "status"], child_env(dict(auth=auth), parent_env)
    )
    text = stdout + "\n" + exit.stderr
    return dict(
        ok=exit.code == 0
        and not exit.timed_out
        and LOGGED_IN_RE.search(text) is not None
        and re.search("not logged in", text, re.I) is None,
        login_cmd="codex login",
    )


codex_adapter = ProviderAdapter("codex", CODEX_BIN, start_codex, probe_auth, effort_map)
