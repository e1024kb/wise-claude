from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Any, Literal, Protocol

import anyio
from jsonschema import Draft7Validator  # type: ignore[import-untyped]
from mcp import MCPError
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ContentBlock,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

WAIT_DEFAULT_MS = 110_000
WAIT_MAX_MS = 600_000
CALL_HEADROOM_MS = 30_000
MCP_SERVER_NAME = "wise-engine"
TOKEN_VAR = "WISE_STEP_TOKEN"
INIT_HINT = "wise-engined is not reachable; run /wise-init, then retry."

ProgressCallback = Callable[[Mapping[str, Any]], Awaitable[None]]


class DaemonCall(Protocol):
    """Call the daemon and remove pending waits and progress listeners on cancellation."""

    async def __call__(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_ms: int | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> Any: ...


class DaemonUnavailableError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def ok_result(value: Any) -> CallToolResult:
    content: list[ContentBlock] = [TextContent(type="text", text=_json(value))]
    if isinstance(value, dict):
        return CallToolResult(content=content, structured_content=value)
    return CallToolResult(content=content)


def error_result(code: str, message: str, /, **extra: Any) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(
                type="text", text=_json({"error": {"code": code, "message": message, **extra}})
            )
        ],
        is_error=True,
    )


def to_error_result(error: DaemonUnavailableError | RpcError) -> CallToolResult:
    if isinstance(error, DaemonUnavailableError):
        return error_result("DAEMON_UNAVAILABLE", str(error), cause=error.code, hint=INIT_HINT)
    data = error.data if isinstance(error.data, dict) else {}
    extra = {key: value for key, value in data.items() if key != "code"}
    code = data.get("code")
    if error.code == -32000 and isinstance(code, str):
        return error_result(code, str(error), **extra)
    return error_result("RPC_ERROR", str(error), **{"rpc_code": error.code, **extra})


def tool_definitions(kind: Literal["parent", "child"]) -> list[dict[str, Any]]:
    definitions: dict[str, list[dict[str, Any]]] = json.loads(
        files("wise_engine").joinpath("tool_schemas.json").read_text(encoding="utf-8")
    )
    return definitions[kind]


def _normalize(value: Any, schema: dict[str, Any]) -> Any:
    if isinstance(value, dict) and schema.get("type") == "object":
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        result = {}
        for key, item in value.items():
            if key in properties:
                result[key] = _normalize(item, properties[key])
            elif isinstance(additional, dict):
                result[key] = _normalize(item, additional)
            elif additional:
                result[key] = item
        for key, child in properties.items():
            if key not in result and "default" in child:
                result[key] = copy.deepcopy(child["default"])
        return result
    if isinstance(value, list) and schema.get("type") == "array":
        return [_normalize(item, schema.get("items", {})) for item in value]
    return value


def question_form_schema(question: Mapping[str, Any]) -> dict[str, Any]:
    options = question.get("options") or []
    prop: dict[str, Any] = {"type": "string", "title": question["label"]}
    if question["kind"] == "choice" and options:
        prop["oneOf"] = [{"const": item["value"], "title": item["label"]} for item in options]
    elif question["kind"] == "multi" and options:
        prop["type"] = "array"
        prop["items"] = {
            "anyOf": [{"const": item["value"], "title": item["label"]} for item in options]
        }
    elif not question.get("optional"):
        prop["minLength"] = 1
    if options and question["kind"] in ("choice", "multi"):
        notes = [
            f"{item['label']}: {item['description']}" for item in options if item.get("description")
        ]
        if notes:
            prop["description"] = "\n".join(notes)
    default = question.get("default")
    if (prop["type"] == "string" and isinstance(default, str)) or (
        prop["type"] == "array" and isinstance(default, list)
    ):
        prop["default"] = default
    key = question["id"]
    return {"type": "object", "properties": {key: prop}, "required": [key]}


def _accepted_answer(question: Mapping[str, Any], content: Mapping[str, Any]) -> Any:
    value = content.get(question["id"])
    options = question.get("options") or []
    allowed = {item["value"] for item in options}
    if question["kind"] == "multi":
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return None
        return None if allowed and any(item not in allowed for item in value) else value
    if not isinstance(value, str):
        return None
    if question["kind"] == "text" and not question.get("optional") and not value.strip():
        return None
    if question["kind"] == "choice" and allowed and value not in allowed:
        return None
    return value


async def _preflight(
    call: DaemonCall,
    args: dict[str, Any],
    ctx: ServerRequestContext[Any, Any],
    refresh: Callable[[], None] | None,
) -> CallToolResult:
    capabilities = ctx.session.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    if (
        elicitation is None
        or (elicitation.form is None and elicitation.url is not None)
        or not ctx.session.can_send_request
    ):
        return error_result(
            "INTERACTIVE_UI_REQUIRED",
            "This MCP host does not support form elicitation. Use a native picker or the terminal TUI; "
            "never ask these questions in plain chat.",
        )
    if refresh is not None:
        refresh()
    answers = dict(args.get("answers", {}))
    for _ in range(256):
        result = await call(
            "preflight",
            {"workflow": args["workflow"], "cwd": args["cwd"], "answers": answers.copy()},
        )
        questions = [
            q for q in result["questions"] if not q.get("locked") and q["id"] not in answers
        ]
        if result["requires_missing"] or not questions:
            return ok_result({**result, "questions": [], "answers": answers})
        question = questions[0]
        response = await ctx.session.elicit_form(
            message=f"Configure {result['workflow']}",
            requested_schema=question_form_schema(question),
            related_request_id=ctx.request_id,
        )
        if response.action != "accept":
            return error_result(
                "PREFLIGHT_CANCELLED",
                "The user cancelled workflow preflight.",
                question=question["id"],
                action=response.action,
            )
        answer = _accepted_answer(question, response.content or {})
        if answer is None:
            return error_result(
                "INTERACTIVE_UI_INVALID",
                "The form returned an invalid answer.",
                question=question["id"],
            )
        answers[question["id"]] = answer
    return error_result("PREFLIGHT_LIMIT", "Preflight exceeded its question limit.")


async def _parent_call(
    name: str,
    args: dict[str, Any],
    ctx: ServerRequestContext[Any, Any],
    call: DaemonCall,
    refresh: Callable[[], None] | None,
) -> CallToolResult:
    if name == "wise_preflight":
        if args.pop("interactive"):
            return await _preflight(call, args, ctx, refresh)
    if name in ("wise_preflight", "wise_run") and refresh is not None:
        refresh()
    if name == "wise_wait":
        timeout_ms = min(args.get("timeout_ms", WAIT_DEFAULT_MS), WAIT_MAX_MS)
        args["timeout_ms"] = timeout_ms
        token = (ctx.meta or {}).get("progress_token")

        async def progress(params: Mapping[str, Any]) -> None:
            if not isinstance(token, (str, int)) or isinstance(token, bool):
                return
            waiting = params.get("waiting_ms")
            if (
                params.get("run_id") != args["run_id"]
                or isinstance(waiting, bool)
                or not isinstance(waiting, (int, float))
                or not math.isfinite(waiting)
            ):
                return
            try:
                await ctx.session.send_progress_notification(
                    progress_token=token,
                    progress=waiting,
                    message=f"wise_wait: run {args['run_id']} has been waiting for {math.floor(waiting / 1000 + 0.5)} s",
                )
            except (MCPError, anyio.BrokenResourceError, anyio.ClosedResourceError):
                pass

        return ok_result(
            await call(
                "wait",
                args,
                timeout_ms=timeout_ms + CALL_HEADROOM_MS,
                on_progress=progress
                if isinstance(token, (str, int)) and not isinstance(token, bool)
                else None,
            )
        )
    return ok_result(await call(name.removeprefix("wise_"), args))


async def _child_call(
    name: str,
    args: dict[str, Any],
    call: DaemonCall,
    token: str,
    ask_timeout_ms: int,
) -> CallToolResult:
    if not token:
        return error_result("TOKEN_INVALID", f"{TOKEN_VAR} is not set for this child")
    params = {"token": token, **args}
    if name == "wise_checkpoint":
        params.setdefault("data", None)
    if name != "wise_ask":
        return ok_result(await call(name.replace("wise_", "child_", 1), params))
    params["timeout_ms"] = ask_timeout_ms
    while True:
        result = await call(
            "child_ask", params.copy(), timeout_ms=ask_timeout_ms + CALL_HEADROOM_MS
        )
        if result["status"] == "answered":
            return ok_result({"value": result["value"]})
        if result["status"] == "needs-human":
            return error_result(
                "needs-human",
                "no human is attached and no decision covers this question",
                error="needs-human",
            )
        params["ask_id"] = result["ask_id"]


def _create_server(
    kind: Literal["parent", "child"],
    call: DaemonCall,
    *,
    version: str,
    close: Callable[[], Awaitable[None]] | None,
    refresh: Callable[[], None] | None = None,
    token: str = "",
    ask_timeout_ms: int = WAIT_DEFAULT_MS,
) -> Server[Any]:
    definitions = tool_definitions(kind)
    schemas = {tool["name"]: tool["inputSchema"] for tool in definitions}
    validators = {name: Draft7Validator(schema) for name, schema in schemas.items()}

    @asynccontextmanager
    async def lifespan(_: Server[Any]) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            if close is not None:
                with anyio.CancelScope(shield=True):
                    await close()

    async def list_tools(
        ctx: ServerRequestContext[Any, Any],
        params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        return ListToolsResult(tools=[Tool.model_validate(tool) for tool in definitions])

    async def call_tool(
        ctx: ServerRequestContext[Any, Any],
        params: CallToolRequestParams,
    ) -> CallToolResult:
        if params.name not in schemas:
            raise MCPError(code=-32602, message=f"Unknown tool: {params.name}")
        args = _normalize(
            params.arguments if params.arguments is not None else {}, schemas[params.name]
        )
        error = next(validators[params.name].iter_errors(args), None)
        if error is not None:
            path = ".".join(str(part) for part in error.absolute_path)
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=f"Invalid arguments for {params.name}: {path}: {error.message}",
                    )
                ],
                is_error=True,
            )
        try:
            if kind == "parent":
                return await _parent_call(params.name, args, ctx, call, refresh)
            return await _child_call(params.name, args, call, token, ask_timeout_ms)
        except (DaemonUnavailableError, RpcError) as rpc_error:
            return to_error_result(rpc_error)

    return Server(
        MCP_SERVER_NAME,
        version=version,
        lifespan=lifespan,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def create_mcp_server(
    call: DaemonCall,
    *,
    version: str,
    refresh: Callable[[], None] | None = None,
    close: Callable[[], Awaitable[None]] | None = None,
) -> Server[Any]:
    return _create_server("parent", call, version=version, refresh=refresh, close=close)


def create_unit_mcp_server(
    call: DaemonCall,
    *,
    version: str,
    token: str | None = None,
    ask_timeout_ms: int = WAIT_DEFAULT_MS,
    close: Callable[[], Awaitable[None]] | None = None,
) -> Server[Any]:
    return _create_server(
        "child",
        call,
        version=version,
        token=os.environ.get(TOKEN_VAR, "") if token is None else token,
        ask_timeout_ms=min(max(0, ask_timeout_ms), WAIT_MAX_MS),
        close=close,
    )


async def serve_stdio(
    server: Server[Any],
    *,
    parent_pid: Callable[[], int] = os.getppid,
    host_poll_seconds: float = 5,
) -> None:
    try:
        await _serve_stdio(server, parent_pid, host_poll_seconds)
    except* (BrokenPipeError, ConnectionResetError, anyio.BrokenResourceError):
        pass


async def _serve_stdio(
    server: Server[Any],
    parent_pid: Callable[[], int],
    host_poll_seconds: float,
) -> None:
    async with anyio.create_task_group() as tasks:

        async def watch_parent() -> None:
            while True:
                if parent_pid() == 1:
                    tasks.cancel_scope.cancel()
                    return
                await anyio.sleep(host_poll_seconds)

        tasks.start_soon(watch_parent)
        try:
            async with _cancellable_stdin() as stdin:
                async with stdio_server(stdin=stdin) as (read, write):
                    await server.run(read, write, server.create_initialization_options())
        finally:
            tasks.cancel_scope.cancel()


class _PipeInput(anyio.AsyncFile[str]):
    def __init__(self, stream: Any, reader: asyncio.StreamReader) -> None:
        super().__init__(stream)
        self.reader = reader

    async def readline(self) -> str:
        return (await self.reader.readline()).decode("utf-8", errors="replace")


@asynccontextmanager
async def _cancellable_stdin() -> AsyncIterator[anyio.AsyncFile[str]]:
    # A blocked thread reading stdin cannot stop when the host dies with the pipe open.
    saved_fd = os.dup(sys.stdin.fileno())
    was_blocking = os.get_blocking(saved_fd)
    stream = os.fdopen(os.dup(saved_fd), "r", encoding="utf-8")
    transport: asyncio.ReadTransport | None = None
    try:
        reader = asyncio.StreamReader(limit=sys.maxsize)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader),
            stream,
        )
        with open(os.devnull, "rb") as empty:
            os.dup2(empty.fileno(), sys.stdin.fileno())
        yield _PipeInput(stream, reader)
    finally:
        os.dup2(saved_fd, sys.stdin.fileno())
        os.close(saved_fd)
        if transport is not None:
            transport.close()
        else:
            stream.close()
        os.set_blocking(sys.stdin.fileno(), was_blocking)
