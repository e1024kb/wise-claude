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

from .client import Client, ConnectError, connect, ensure_daemon
from .host_watch import HostWatch
from .protocol import RPC_CLIENT_DISCONNECTED
from .version import plugin_version, source_build_id
from .rpc import RpcError as RpcError

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


DaemonUnavailableError = ConnectError


class DaemonLink:
    def __init__(
        self,
        *,
        daemon: dict[str, Any] | None = None,
        auto_start: bool = True,
        current_version: Callable[[], str] | None = None,
        connector: Callable[..., Awaitable[Client]] | None = None,
    ) -> None:
        self._options = dict(daemon or {})
        pinned = self._options.get("version")
        self._current_version = current_version or (
            (lambda: pinned) if pinned is not None else source_build_id
        )
        self._connect = connector or (ensure_daemon if auto_start else connect)
        self._client: Client | None = None
        self._opening: asyncio.Task[Client] | None = None
        self._closed = False
        self._last_refresh_ms = 0.0

    def refresh(self, now: float | None = None) -> None:
        now = asyncio.get_running_loop().time() * 1000 if now is None else now
        if self._client is None or now - self._last_refresh_ms < 3000:
            return
        self._last_refresh_ms = now
        if self._client.hello["version"] != self._current_version():
            self._drop(self._client)

    def _drop(self, client: Client) -> None:
        if self._client is client:
            self._client = None
        client.close()

    async def _open(self) -> Client:
        if self._closed:
            raise RpcError(RPC_CLIENT_DISCONNECTED, "connection closed")
        if self._opening is None:

            async def opening() -> Client:
                try:
                    client = await self._connect(
                        **{**self._options, "version": self._current_version()}
                    )
                    if self._closed:
                        client.close()
                        raise RpcError(RPC_CLIENT_DISCONNECTED, "connection closed")
                    self._client = client
                    return client
                finally:
                    self._opening = None

            self._opening = asyncio.create_task(opening())
            self._opening.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        return await asyncio.shield(self._opening)

    async def with_client(self, operation: Callable[[Client], Awaitable[Any]]) -> Any:
        first = self._client if self._client is not None else await self._open()
        try:
            return await operation(first)
        except RpcError as error:
            if error.code != RPC_CLIENT_DISCONNECTED:
                raise
            self._drop(first)
        return await operation(await self._open())

    async def __call__(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_ms: int | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> Any:
        async def operation(client: Client) -> Any:
            queue: asyncio.Queue[Mapping[str, Any] | None] = asyncio.Queue()

            async def forward() -> None:
                while (item := await queue.get()) is not None:
                    if on_progress:
                        try:
                            await on_progress(item)
                        except Exception:
                            pass

            def notification(message: dict[str, Any]) -> None:
                progress = message.get("params")
                if (
                    message["method"] == "progress"
                    and isinstance(progress, dict)
                    and progress.get("run_id") == params.get("run_id")
                    and isinstance(progress.get("waiting_ms"), (int, float))
                    and not isinstance(progress.get("waiting_ms"), bool)
                ):
                    queue.put_nowait(progress)

            worker = asyncio.create_task(forward()) if on_progress else None
            unsubscribe = client.on_notification(notification) if on_progress else None
            try:
                result = await client.call(method, params, timeout_ms=timeout_ms)
                if worker:
                    queue.put_nowait(None)
                    await worker
                return result
            finally:
                if unsubscribe:
                    unsubscribe()
                if worker and not worker.done():
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)

        return await self.with_client(operation)

    async def close(self) -> None:
        self._closed = True
        client = self._client
        if client:
            self._drop(client)
        opening = self._opening
        if opening:
            opening.cancel()
            await asyncio.gather(opening, return_exceptions=True)
        if client:
            await client.rpc.task


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
            "This MCP host does not support form elicitation. Use a native picker and await "
            "the user's answer, keeping asynchronous prompts open, or use the terminal TUI. "
            "If no persistent picker is available, show each raw preflight question in text "
            "and wait for an explicit reply; never submit defaults as answers.",
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
    call: DaemonCall | None = None,
    *,
    version: str | None = None,
    refresh: Callable[[], None] | None = None,
    close: Callable[[], Awaitable[None]] | None = None,
    daemon: dict[str, Any] | None = None,
    auto_start: bool = True,
    current_version: Callable[[], str] | None = None,
) -> Server[Any]:
    if call is None:
        link = DaemonLink(daemon=daemon, auto_start=auto_start, current_version=current_version)
        call, refresh, close = link, link.refresh, link.close
    return _create_server(
        "parent",
        call,
        version=version or (daemon or {}).get("version") or plugin_version(),
        refresh=refresh,
        close=close,
    )


def create_unit_mcp_server(
    call: DaemonCall | None = None,
    *,
    version: str | None = None,
    token: str | None = None,
    ask_timeout_ms: int = WAIT_DEFAULT_MS,
    close: Callable[[], Awaitable[None]] | None = None,
    daemon: dict[str, Any] | None = None,
) -> Server[Any]:
    env = (daemon or {}).get("env", os.environ)
    if call is None:
        options = daemon
        if options is None:
            options = {"env": env, "client": "wise-engine unit-mcp"}
            if env.get("WISE_ENGINE_SOCKET"):
                options["socket_path"] = env["WISE_ENGINE_SOCKET"]
            if env.get("WISE_DATA_ROOT"):
                options["data_root"] = env["WISE_DATA_ROOT"]
        link = DaemonLink(daemon=options, auto_start=False)
        call, close = link, link.close
    return _create_server(
        "child",
        call,
        version=version or (daemon or {}).get("version") or plugin_version(),
        token=env.get(TOKEN_VAR, "") if token is None else token,
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
        watch = HostWatch(
            on_gone=lambda _: tasks.cancel_scope.cancel(),
            ppid=parent_pid,
            interval_ms=host_poll_seconds * 1000,
        )
        try:
            async with _cancellable_stdin() as stdin:
                async with stdio_server(stdin=stdin) as (read, write):
                    await server.run(read, write, server.create_initialization_options())
        finally:
            watch.stop()
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


MCP_USAGE = """wise-engine mcp [options]

  Serve the eight wise_* tools over stdio MCP; connects to (and starts) wise-engined.

Options: --data-root <dir> --socket <path> --lock <path> --log <path> --idle-ms <n> --no-start
"""
UNIT_MCP_USAGE = """wise-engine unit-mcp [options]

  Child-side stdio MCP server: wise_report, wise_ask, wise_context, wise_checkpoint.
  Reads WISE_STEP_TOKEN, WISE_ENGINE_SOCKET, WISE_DATA_ROOT from the env; never starts the daemon.

Options: --token <t> --socket <path> --data-root <dir>
"""


async def mcp_command(argv: list[str], io: Any) -> int:
    from .daemon import parse_daemon_args, path_opts_from

    args = parse_daemon_args(["mcp", *argv])
    if args["flags"].get("help") is True or (argv and argv[0] == "-h"):
        io.err(MCP_USAGE)
        return 0
    env = io.env if getattr(io, "env", None) is not None else os.environ
    options = {**path_opts_from(args, env), "client": "wise-engine mcp"}
    try:
        await serve_stdio(
            create_mcp_server(daemon=options, auto_start=args["flags"].get("no-start") is not True)
        )
        return 0
    except Exception as error:
        io.err(f"wise-engine mcp: {error}\n")
        return 70


async def unit_mcp_command(argv: list[str], io: Any) -> int:
    from .daemon import parse_daemon_args

    flags = parse_daemon_args(["unit-mcp", *argv])["flags"]
    if flags.get("help") is True or (argv and argv[0] == "-h"):
        io.err(UNIT_MCP_USAGE)
        return 0
    env = io.env if getattr(io, "env", None) is not None else os.environ
    options: dict[str, Any] = {"env": env, "client": "wise-engine unit-mcp"}
    for key, flag, var in (
        ("socket_path", "socket", "WISE_ENGINE_SOCKET"),
        ("data_root", "data-root", "WISE_DATA_ROOT"),
    ):
        if env.get(var):
            options[key] = env[var]
        if isinstance(flags.get(flag), str) and flags[flag]:
            options[key] = flags[flag]
    token = flags.get("token")
    try:
        await serve_stdio(
            create_unit_mcp_server(
                daemon=options, token=token if isinstance(token, str) and token else None
            )
        )
        return 0
    except Exception as error:
        io.err(f"wise-engine unit-mcp: {error}\n")
        return 70
