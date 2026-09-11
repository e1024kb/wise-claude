from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client, MCPError
from mcp.types import ElicitResult

from wise_engine.mcp_server import (
    CALL_HEADROOM_MS,
    WAIT_DEFAULT_MS,
    WAIT_MAX_MS,
    DaemonUnavailableError,
    ProgressCallback,
    RpcError,
    create_mcp_server,
    create_unit_mcp_server,
    question_form_schema,
)

pytestmark = pytest.mark.anyio
ENGINE = Path(__file__).resolve().parents[1]
CONTRACTS = ENGINE / "test" / "fixtures" / "contracts"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeDaemon:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], int | None]] = []
        self.handlers: dict[str, Callable[..., Awaitable[Any]]] = {}
        self.results: dict[str, Any] = {}
        self.closed = 0
        self.refreshed = 0

    async def __call__(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_ms: int | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> Any:
        self.calls.append((method, copy.deepcopy(params), timeout_ms))
        if method in self.handlers:
            return await self.handlers[method](params, on_progress)
        return self.results.get(method, {"accepted": True})

    async def close(self) -> None:
        self.closed += 1

    def refresh(self) -> None:
        self.refreshed += 1


def body(result: Any) -> Any:
    return json.loads(result.content[0].text)


def parent(daemon: FakeDaemon) -> Any:
    return create_mcp_server(daemon, version="fixture", close=daemon.close, refresh=daemon.refresh)


@pytest.mark.parametrize("kind", ["parent", "child"])
async def test_tool_catalog_matches_captured_contract(kind: str) -> None:
    daemon = FakeDaemon()
    server = (
        parent(daemon)
        if kind == "parent"
        else create_unit_mcp_server(daemon, version="fixture", token="secret")
    )
    async with Client(server, mode="legacy") as client:
        result = await client.list_tools()
        assert [
            tool.model_dump(by_alias=True, exclude_none=True) for tool in result.tools
        ] == json.loads((CONTRACTS / f"{kind}-tools.json").read_text())["tools"]


@pytest.mark.parametrize(
    ("name", "args", "method", "expected"),
    [
        (
            "wise_preflight",
            {"workflow": "flow", "cwd": "/project", "interactive": False},
            "preflight",
            {"workflow": "flow", "cwd": "/project"},
        ),
        (
            "wise_run",
            {"workflow": "flow", "cwd": "/project"},
            "run",
            {"workflow": "flow", "cwd": "/project", "answers": {}, "context": {}, "inputs": {}},
        ),
        (
            "wise_answer",
            {"run_id": "run", "gate_id": "gate", "value": ["a", "b"]},
            "answer",
            {"run_id": "run", "gate_id": "gate", "value": ["a", "b"]},
        ),
        ("wise_status", {}, "status", {}),
        ("wise_status", {"run_id": "run"}, "status", {"run_id": "run"}),
        (
            "wise_cancel",
            {"run_id": "run", "reason": "operator"},
            "cancel",
            {"run_id": "run", "reason": "operator"},
        ),
        (
            "wise_nudge",
            {"run_id": "run", "step": "step", "message": "continue"},
            "nudge",
            {"run_id": "run", "step": "step", "message": "continue"},
        ),
        ("wise_resume", {"run_id": "run", "profile": "max"}, "resume", {"run_id": "run"}),
    ],
)
async def test_parent_dispatch(
    name: str, args: dict[str, Any], method: str, expected: dict[str, Any]
) -> None:
    daemon = FakeDaemon()
    async with Client(parent(daemon), mode="legacy") as client:
        result = await client.call_tool(name, args)
        assert body(result) == {"accepted": True}
        assert result.structured_content == {"accepted": True}
    assert daemon.calls == [(method, expected, None)]
    assert daemon.refreshed == int(name in ("wise_run", "wise_preflight"))
    assert daemon.closed == 1


async def test_nested_unknown_keys_are_stripped_but_records_survive() -> None:
    daemon = FakeDaemon()
    async with Client(parent(daemon), mode="legacy") as client:
        await client.call_tool(
            "wise_run",
            {
                "workflow": "flow",
                "cwd": "/project",
                "unknown": 1,
                "answers": {"harness.work": "codex"},
                "inputs": {"topic": "transport"},
                "context": {
                    "unknown": 2,
                    "ticket": [{"ref": "ref", "title": "Title", "ignored": True}],
                    "decisions": {"choice": "a"},
                },
            },
        )
    assert daemon.calls[0][1] == {
        "workflow": "flow",
        "cwd": "/project",
        "answers": {"harness.work": "codex"},
        "inputs": {"topic": "transport"},
        "context": {"ticket": [{"ref": "ref", "title": "Title"}], "decisions": {"choice": "a"}},
    }


@pytest.mark.parametrize(
    "name,args",
    [
        ("wise_cancel", {}),
        ("wise_cancel", {"run_id": None}),
        ("wise_wait", {"run_id": "run", "after": True}),
        ("wise_wait", {"run_id": "run", "timeout_ms": -1}),
        ("wise_wait", {"run_id": "run", "timeout_ms": 1.5}),
        ("wise_run", {"workflow": "flow", "cwd": "/p", "answers": None}),
        ("wise_answer", {"run_id": "run", "gate_id": "gate", "value": [False]}),
    ],
)
async def test_invalid_arguments_never_reach_daemon(name: str, args: dict[str, Any]) -> None:
    daemon = FakeDaemon()
    async with Client(parent(daemon), mode="legacy") as client:
        result = await client.call_tool(name, args)
        assert result.is_error
        assert name in result.content[0].text
    assert daemon.calls == []


@pytest.mark.parametrize("value", [[{"run_id": "run"}], None, "hello", 3, {"value": "café 🦉"}])
async def test_success_result_envelopes(value: Any) -> None:
    daemon = FakeDaemon()
    daemon.results["status"] = value
    async with Client(parent(daemon), mode="legacy") as client:
        result = await client.call_tool("wise_status")
        assert body(result) == value
        assert result.structured_content == (value if isinstance(value, dict) else None)
        assert result.content[0].text == json.dumps(
            value, separators=(",", ":"), ensure_ascii=False
        )


@pytest.mark.parametrize(
    "error,expected",
    [
        (
            DaemonUnavailableError("SOCKET_MISSING", "not reachable"),
            {
                "code": "DAEMON_UNAVAILABLE",
                "message": "not reachable",
                "cause": "SOCKET_MISSING",
                "hint": "wise-engined is not reachable; run /wise-init, then retry.",
            },
        ),
        (
            RpcError(-32000, "login", {"code": "AUTH_REQUIRED", "login_cmd": "codex login"}),
            {"code": "AUTH_REQUIRED", "message": "login", "login_cmd": "codex login"},
        ),
        (
            RpcError(-32603, "broken", {"detail": "fixture"}),
            {"code": "RPC_ERROR", "message": "broken", "rpc_code": -32603, "detail": "fixture"},
        ),
    ],
)
async def test_domain_and_connection_errors_keep_tool_envelopes(
    error: Exception, expected: dict[str, Any]
) -> None:
    daemon = FakeDaemon()

    async def fail(params: Any, progress: Any) -> Any:
        raise error

    daemon.handlers["status"] = fail
    async with Client(parent(daemon), mode="legacy") as client:
        result = await client.call_tool("wise_status")
        assert result.is_error
        assert result.structured_content is None
        assert body(result) == {"error": expected}


async def test_unexpected_errors_and_unknown_tools_stay_protocol_errors() -> None:
    daemon = FakeDaemon()

    async def fail(params: Any, progress: Any) -> Any:
        raise RuntimeError("fixture failure")

    daemon.handlers["status"] = fail
    async with Client(parent(daemon), mode="legacy") as client:
        with pytest.raises(MCPError):
            await client.call_tool("wise_status")
        with pytest.raises(MCPError):
            await client.call_tool("unknown")


@pytest.mark.parametrize("timeout", [None, 0, 900_000])
async def test_wait_bounds_progress_filtering_and_token(timeout: int | None) -> None:
    daemon = FakeDaemon()
    observed = []

    async def wait(params: Any, progress: Any) -> Any:
        assert progress is not None
        await progress({"run_id": "other", "waiting_ms": 1})
        await progress({"run_id": "run", "waiting_ms": True})
        await progress({"run_id": "run", "waiting_ms": 30_000})
        await progress({"run_id": "run", "waiting_ms": 60_000})
        return {"events": [], "status": "running", "done": False}

    async def progress(amount: Any, total: Any, message: Any) -> None:
        observed.append((amount, total, message))

    daemon.handlers["wait"] = wait
    args = {"run_id": "run", "after": 2}
    if timeout is not None:
        args["timeout_ms"] = timeout
    async with Client(parent(daemon), mode="legacy") as client:
        result = await client.call_tool("wise_wait", args, progress_callback=progress)
        assert not body(result)["done"]
    expected = WAIT_DEFAULT_MS if timeout is None else min(timeout, WAIT_MAX_MS)
    assert daemon.calls == [
        ("wait", {"run_id": "run", "after": 2, "timeout_ms": expected}, expected + CALL_HEADROOM_MS)
    ]
    assert observed == [
        (30_000, None, "wise_wait: run run has been waiting for 30 s"),
        (60_000, None, "wise_wait: run run has been waiting for 60 s"),
    ]


async def test_wait_without_token_has_no_progress_subscription() -> None:
    daemon = FakeDaemon()

    async def wait(params: Any, progress: Any) -> Any:
        assert progress is None
        return {"done": True}

    daemon.handlers["wait"] = wait
    async with Client(parent(daemon), mode="legacy") as client:
        assert body(await client.call_tool("wise_wait", {"run_id": "run"})) == {"done": True}


@pytest.mark.parametrize("kind", ["parent", "child"])
async def test_cancelled_long_poll_releases_handler_without_cancelling_run(kind: str) -> None:
    daemon = FakeDaemon()
    started = anyio.Event()
    finished = anyio.Event()

    async def blocked(params: Any, progress: Any) -> Any:
        started.set()
        try:
            await anyio.sleep_forever()
        finally:
            finished.set()

    daemon.handlers["wait" if kind == "parent" else "child_ask"] = blocked
    server = (
        parent(daemon)
        if kind == "parent"
        else create_unit_mcp_server(daemon, version="fixture", token="secret")
    )
    async with Client(server, mode="legacy") as client:
        async with anyio.create_task_group() as tasks:

            async def invoke() -> None:
                await client.call_tool(
                    "wise_wait" if kind == "parent" else "wise_ask",
                    {"run_id": "run"} if kind == "parent" else {"question": "Continue?"},
                )

            tasks.start_soon(invoke)
            await started.wait()
            tasks.cancel_scope.cancel()
        with anyio.fail_after(2):
            await finished.wait()
        assert body(
            await client.call_tool(
                "wise_status" if kind == "parent" else "wise_context",
                {} if kind == "parent" else {"key": "guidance"},
            )
        ) == {"accepted": True}
    assert all(method != "cancel" for method, _, _ in daemon.calls)


def question(identifier: str, kind: str = "choice") -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": kind,
        "label": identifier,
        "options": [
            {"value": "a", "label": "A", "description": "First"},
            {"value": "b", "label": "B"},
        ],
        "default": ["a"] if kind == "multi" else "a",
    }


async def test_interactive_preflight_collects_real_staged_answers() -> None:
    daemon = FakeDaemon()
    forms = []
    questions = [
        question("step-select", "multi"),
        question("harness.work"),
        {"id": "input.topic", "kind": "text", "label": "Topic"},
    ]

    async def preflight(params: Any, progress: Any) -> Any:
        unanswered = [q for q in questions if q["id"] not in params["answers"]]
        return {
            "workflow": "flow",
            "version": 2,
            "questions": unanswered[:1],
            "defaults": {"harness.work": "a"},
            "requires_missing": [],
        }

    async def elicit(ctx: Any, params: Any) -> ElicitResult:
        forms.append(params.requested_schema)
        identifier = params.requested_schema["required"][0]
        return ElicitResult(
            action="accept",
            content={
                identifier: {"step-select": ["b"], "harness.work": "b", "input.topic": "transport"}[
                    identifier
                ]
            },
        )

    daemon.handlers["preflight"] = preflight
    async with Client(parent(daemon), mode="legacy", elicitation_callback=elicit) as client:
        result = body(
            await client.call_tool("wise_preflight", {"workflow": "flow", "cwd": "/project"})
        )
    assert result["answers"] == {
        "step-select": ["b"],
        "harness.work": "b",
        "input.topic": "transport",
    }
    assert result["questions"] == []
    assert forms == [question_form_schema(q) for q in questions]
    assert len(daemon.calls) == 4


async def test_preflight_without_form_capability_starts_nothing() -> None:
    daemon = FakeDaemon()
    async with Client(parent(daemon), mode="legacy") as client:
        result = await client.call_tool("wise_preflight", {"workflow": "flow", "cwd": "/project"})
    assert body(result)["error"]["code"] == "INTERACTIVE_UI_REQUIRED"
    assert daemon.calls == []
    assert daemon.refreshed == 0


async def test_preflight_stays_pending_until_user_answers() -> None:
    daemon = FakeDaemon()
    displayed = anyio.Event()
    answer_ready = anyio.Event()
    completed = anyio.Event()
    results: list[Any] = []

    async def preflight(params: Any, progress: Any) -> Any:
        return {
            "workflow": "flow",
            "questions": [] if params["answers"] else [question("step-select", "multi")],
            "requires_missing": [],
        }

    async def elicit(ctx: Any, params: Any) -> ElicitResult:
        displayed.set()
        await answer_ready.wait()
        return ElicitResult(action="accept", content={"step-select": ["b"]})

    daemon.handlers["preflight"] = preflight
    async with Client(parent(daemon), mode="legacy", elicitation_callback=elicit) as client:

        async def collect() -> None:
            results.append(
                body(await client.call_tool("wise_preflight", {"workflow": "flow", "cwd": "/p"}))
            )
            completed.set()

        with anyio.fail_after(5):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(collect)
                await displayed.wait()
                assert not completed.is_set()
                assert len(daemon.calls) == 1
                assert daemon.calls[0][1]["answers"] == {}
                answer_ready.set()
                await completed.wait()
    assert results[0]["answers"] == {"step-select": ["b"]}


@pytest.mark.parametrize(
    "action,answer,expected",
    [
        ("decline", None, "PREFLIGHT_CANCELLED"),
        ("cancel", None, "PREFLIGHT_CANCELLED"),
        ("accept", "unknown", "INTERACTIVE_UI_INVALID"),
        ("accept", None, "INTERACTIVE_UI_INVALID"),
    ],
)
async def test_preflight_decline_and_invalid_answers(
    action: str, answer: Any, expected: str
) -> None:
    daemon = FakeDaemon()
    daemon.results["preflight"] = {
        "workflow": "flow",
        "questions": [question("harness.work")],
        "requires_missing": [],
    }

    async def elicit(ctx: Any, params: Any) -> ElicitResult:
        return ElicitResult(
            action=action, content={"harness.work": answer} if answer is not None else None
        )

    async with Client(parent(daemon), mode="legacy", elicitation_callback=elicit) as client:
        result = await client.call_tool("wise_preflight", {"workflow": "flow", "cwd": "/project"})
    assert body(result)["error"]["code"] == expected
    assert len(daemon.calls) == 1


@pytest.mark.parametrize(
    "name,args,method,expected",
    [
        (
            "wise_report",
            {"kind": "finding", "text": "A finding", "data": {"x": 1}},
            "child_report",
            {"kind": "finding", "text": "A finding", "data": {"x": 1}},
        ),
        ("wise_context", {"key": "ticket", "token": "spoof"}, "child_context", {"key": "ticket"}),
        ("wise_checkpoint", {"data": [1, "two"]}, "child_checkpoint", {"data": [1, "two"]}),
        ("wise_checkpoint", {}, "child_checkpoint", {"data": None}),
    ],
)
async def test_child_dispatch_and_token_scope(
    name: str, args: dict[str, Any], method: str, expected: dict[str, Any]
) -> None:
    daemon = FakeDaemon()
    async with Client(
        create_unit_mcp_server(daemon, version="fixture", token="secret", close=daemon.close),
        mode="legacy",
    ) as client:
        assert body(await client.call_tool(name, args)) == {"accepted": True}
    assert daemon.calls == [(method, {"token": "secret", **expected}, None)]
    assert daemon.closed == 1


async def test_child_missing_token_is_tool_error(monkeypatch: Any) -> None:
    monkeypatch.delenv("WISE_STEP_TOKEN", raising=False)
    daemon = FakeDaemon()
    async with Client(create_unit_mcp_server(daemon, version="fixture"), mode="legacy") as client:
        result = await client.call_tool("wise_context", {"key": "ticket"})
    assert body(result)["error"]["code"] == "TOKEN_INVALID"
    assert daemon.calls == []


@pytest.mark.parametrize("outcome", ["answered", "needs-human"])
async def test_child_ask_repolls_with_same_ask_id(outcome: str) -> None:
    daemon = FakeDaemon()

    async def ask(params: Any, progress: Any) -> Any:
        if "ask_id" not in params:
            return {"status": "pending", "ask_id": "gate"}
        return {"status": outcome, "value": ["b"]}

    daemon.handlers["child_ask"] = ask
    async with Client(
        create_unit_mcp_server(daemon, version="fixture", token="secret", ask_timeout_ms=5),
        mode="legacy",
    ) as client:
        result = await client.call_tool(
            "wise_ask", {"question": "Which?", "options": ["a", "b"], "allow_text": False}
        )
    assert daemon.calls == [
        (
            "child_ask",
            {
                "token": "secret",
                "question": "Which?",
                "options": ["a", "b"],
                "allow_text": False,
                "timeout_ms": 5,
            },
            30_005,
        ),
        (
            "child_ask",
            {
                "token": "secret",
                "question": "Which?",
                "options": ["a", "b"],
                "allow_text": False,
                "timeout_ms": 5,
                "ask_id": "gate",
            },
            30_005,
        ),
    ]
    if outcome == "answered":
        assert body(result) == {"value": ["b"]}
    else:
        assert result.is_error
        assert body(result)["error"]["error"] == "needs-human"


SUBPROCESS_SERVER = """
import asyncio, os, sys
from wise_engine.mcp_server import create_mcp_server, create_unit_mcp_server, serve_stdio
parent_dead = False
async def call(method, params, *, timeout_ms=None, on_progress=None):
    global parent_dead
    if params.get('run_id') == 'parent-dead':
        parent_dead = True
    if method in ('wait', 'child_ask'):
        if on_progress is not None:
            await on_progress({'run_id': params['run_id'], 'waiting_ms': 30000})
        print('WAIT_STARTED', file=sys.stderr, flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            print('WAIT_CANCELLED', file=sys.stderr, flush=True)
    return {'method': method, 'params': params}
async def close():
    print('LINK_CLOSED', file=sys.stderr, flush=True)
server = (create_unit_mcp_server(call, version='fixture', token='secret', close=close)
          if os.environ.get('FIXTURE_CHILD') else create_mcp_server(call, version='fixture', close=close))
asyncio.run(serve_stdio(server, parent_pid=lambda: 1 if parent_dead else os.getppid(), host_poll_seconds=0.01))
"""


@asynccontextmanager
async def raw_server(*, child: bool = False) -> AsyncIterator[asyncio.subprocess.Process]:
    env = {**os.environ, "PYTHONPATH": str(ENGINE)}
    if child:
        env["FIXTURE_CHILD"] = "1"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        SUBPROCESS_SERVER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        await send(
            process,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "fixture", "version": "1"},
                },
            },
        )
        initialized = await receive(process)
        assert initialized["result"]["protocolVersion"] == "2025-11-25"
        await send(process, {"method": "notifications/initialized"})
        yield process
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            process.kill()
            await process.wait()


async def send(process: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write(
        (json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False) + "\n").encode()
    )
    await process.stdin.drain()


async def receive(process: asyncio.subprocess.Process) -> dict[str, Any]:
    assert process.stdout is not None
    line = await asyncio.wait_for(process.stdout.readline(), 5)
    assert line, "MCP process closed stdout before responding"
    result: dict[str, Any] = json.loads(line)
    return result


@pytest.mark.parametrize("child", [False, True])
async def test_stdio_schema_framing_and_clean_eof(child: bool) -> None:
    async with raw_server(child=child) as process:
        await send(process, {"id": 2, "method": "tools/list", "params": {}})
        result = await receive(process)
        kind = "child" if child else "parent"
        assert result["result"] == json.loads((CONTRACTS / f"{kind}-tools.json").read_text())
        message = {
            "jsonrpc": "2.0",
            "id": "unicode",
            "method": "tools/call",
            "params": {
                "name": "wise_context" if child else "wise_cancel",
                "arguments": {"key": "café 🦉"}
                if child
                else {"run_id": "run", "reason": "café 🦉"},
            },
        }
        encoded = (json.dumps(message, ensure_ascii=False) + "\n").encode()
        split = encoded.index("🦉".encode()) + 1
        assert process.stdin is not None
        process.stdin.write(encoded[:split])
        await process.stdin.drain()
        process.stdin.write(encoded[split:])
        await process.stdin.drain()
        response = await receive(process)
        assert response["id"] == "unicode"
        assert "café 🦉" in response["result"]["content"][0]["text"]
        assert response["result"]["structuredContent"]["method"] == (
            "child_context" if child else "cancel"
        )
        process.stdin.close()
        assert await asyncio.wait_for(process.wait(), 3) == 0
        assert process.stderr is not None
        assert b"LINK_CLOSED" in await process.stderr.read()


@pytest.mark.parametrize("child", [False, True])
async def test_host_eof_cancels_pending_tool_and_closes_link(child: bool) -> None:
    async with raw_server(child=child) as process:
        await send(
            process,
            {
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "wise_ask" if child else "wise_wait",
                    "arguments": {"question": "Continue?"} if child else {"run_id": "run"},
                },
            },
        )
        assert process.stderr is not None
        assert b"WAIT_STARTED" in await asyncio.wait_for(process.stderr.readline(), 3)
        assert process.stdin is not None
        process.stdin.close()
        assert await asyncio.wait_for(process.wait(), 3) == 0
        stderr = await process.stderr.read()
        assert b"WAIT_CANCELLED" in stderr
        assert b"LINK_CLOSED" in stderr


async def test_parent_disappearance_closes_server_even_with_open_stdin() -> None:
    async with raw_server() as process:
        await send(
            process,
            {
                "id": 2,
                "method": "tools/call",
                "params": {"name": "wise_wait", "arguments": {"run_id": "parent-dead"}},
            },
        )
        assert process.stderr is not None
        assert b"WAIT_STARTED" in await asyncio.wait_for(process.stderr.readline(), 3)
        assert await asyncio.wait_for(process.wait(), 3) == 0
        stderr = await process.stderr.read()
        assert b"WAIT_CANCELLED" in stderr
        assert b"LINK_CLOSED" in stderr


async def test_stdio_progress_and_cancellation_preserve_request_scope() -> None:
    async with raw_server() as process:
        await send(
            process,
            {
                "id": "waiting",
                "method": "tools/call",
                "params": {
                    "name": "wise_wait",
                    "arguments": {"run_id": "run"},
                    "_meta": {"progressToken": "fixture-progress"},
                },
            },
        )
        assert await receive(process) == {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {
                "progressToken": "fixture-progress",
                "progress": 30000,
                "message": "wise_wait: run run has been waiting for 30 s",
            },
        }
        assert process.stderr is not None
        assert b"WAIT_STARTED" in await asyncio.wait_for(process.stderr.readline(), 3)
        await send(
            process, {"method": "notifications/cancelled", "params": {"requestId": "waiting"}}
        )
        assert b"WAIT_CANCELLED" in await asyncio.wait_for(process.stderr.readline(), 3)
        await send(
            process,
            {
                "id": "health",
                "method": "tools/call",
                "params": {"name": "wise_status", "arguments": {}},
            },
        )
        assert (await receive(process))["id"] == "health"


async def test_stdout_disconnect_closes_link_with_open_stdin() -> None:
    async with raw_server() as process:
        assert process.stdout is not None
        process.stdout._transport.close()
        await send(
            process,
            {"id": 2, "method": "tools/call", "params": {"name": "wise_status", "arguments": {}}},
        )
        assert await asyncio.wait_for(process.wait(), 3) == 0
        assert process.stderr is not None
        assert b"LINK_CLOSED" in await process.stderr.read()
