from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from mcp import Client as McpClient

from wise_engine.client import connect
from wise_engine.daemon import start_daemon
from wise_engine.mcp_server import DaemonLink, create_mcp_server, create_unit_mcp_server
from wise_engine.rpc import RpcError, domain_error

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def root():
    with tempfile.TemporaryDirectory(prefix="wl-", dir="/tmp") as root:
        yield root


def body(result):
    return json.loads(result.content[0].text)


async def test_parent_and_child_real_socket_dispatch(root):
    calls = []

    def handle(method):
        def handler(params, ctx):
            calls.append((method, params))
            return {"method": method, "params": params}

        return handler

    names = (
        "preflight",
        "run",
        "wait",
        "answer",
        "status",
        "cancel",
        "nudge",
        "resume",
        "child_report",
        "child_context",
        "child_checkpoint",
    )
    daemon = await start_daemon(
        data_root=root, env={}, version="test", handlers={name: handle(name) for name in names}
    )
    options = {"data_root": root, "env": {}, "version": "test"}
    try:
        async with McpClient(
            create_mcp_server(daemon=options, auto_start=False), mode="legacy"
        ) as client:
            cases = [
                (
                    "wise_preflight",
                    {"workflow": "wf", "cwd": root, "interactive": False},
                    "preflight",
                ),
                ("wise_run", {"workflow": "wf", "cwd": root, "interactive": False}, "run"),
                ("wise_wait", {"run_id": "r", "timeout_ms": 1}, "wait"),
                ("wise_answer", {"run_id": "r", "gate_id": "g", "value": "ok"}, "answer"),
                ("wise_status", {}, "status"),
                ("wise_cancel", {"run_id": "r"}, "cancel"),
                ("wise_nudge", {"run_id": "r", "step": "a", "message": "go"}, "nudge"),
                ("wise_resume", {"run_id": "r"}, "resume"),
            ]
            for name, args, method in cases:
                result = body(await client.call_tool(name, args))
                assert result["method"] == method
            assert daemon.connections() == 1
        await asyncio.sleep(0.01)
        assert daemon.connections() == 0
        async with McpClient(
            create_unit_mcp_server(daemon=options, token="secret"), mode="legacy"
        ) as client:
            for name, args, method in [
                ("wise_report", {"kind": "progress", "text": "hi"}, "child_report"),
                ("wise_context", {"key": "ticket"}, "child_context"),
                ("wise_checkpoint", {"data": {}}, "child_checkpoint"),
            ]:
                result = body(await client.call_tool(name, args))
                assert result["method"] == method
                assert result["params"]["token"] == "secret"
        assert len(calls) == 11
    finally:
        await daemon.close()


async def test_reconnect_once_after_daemon_restart(root):
    options = {"data_root": root, "env": {}, "version": "test"}
    first = await start_daemon(**options)
    link = DaemonLink(daemon=options, auto_start=False)
    second = None
    try:
        assert await link("status", {}) == []
        await first.close()
        second = await start_daemon(**options)
        assert await link("status", {}) == []
        assert second.connections() == 1
    finally:
        await link.close()
        if second:
            await second.close()
        await first.close()


async def test_refresh_drops_stale_build_before_run(root):
    daemon = await start_daemon(data_root=root, env={}, version="old")
    version = "old"
    options = {"data_root": root, "env": {}, "version": "old"}
    server = create_mcp_server(daemon=options, auto_start=False, current_version=lambda: version)
    try:
        async with McpClient(server, mode="legacy") as client:
            assert body(await client.call_tool("wise_status", {})) == []
            version = "new"
            assert body(await client.call_tool("wise_status", {})) == []
            error = body(
                await client.call_tool(
                    "wise_preflight", {"workflow": "wf", "cwd": root, "interactive": False}
                )
            )["error"]
            assert error["code"] == "DAEMON_VERSION_MISMATCH"
    finally:
        await daemon.close()


async def test_lazy_open_is_shared_and_cancellation_does_not_cancel_peer(root):
    daemon = await start_daemon(data_root=root, env={}, version="test")
    opened = 0
    ready = asyncio.Event()

    async def connector(**options):
        nonlocal opened
        opened += 1
        await ready.wait()
        return await connect(**options)

    link = DaemonLink(daemon={"data_root": root, "env": {}, "version": "test"}, connector=connector)
    try:
        cancelled = asyncio.create_task(link("status", {}))
        peer = asyncio.create_task(link("status", {}))
        await asyncio.sleep(0.01)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        ready.set()
        assert await peer == []
        assert opened == 1
    finally:
        await link.close()
        await daemon.close()


async def test_close_during_open_cleans_connection(root):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def connector(**options):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    link = DaemonLink(connector=connector)
    pending = asyncio.create_task(link("status", {}))
    await started.wait()
    await link.close()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert stopped.is_set()
    with pytest.raises(RpcError, match="connection closed"):
        await link("status", {})


async def test_no_retry_for_domain_errors_or_second_disconnect(root):
    calls = 0

    def fail(params, ctx):
        nonlocal calls
        calls += 1
        raise domain_error("RUN_NOT_FOUND", "missing")

    daemon = await start_daemon(data_root=root, env={}, version="test", handlers={"status": fail})
    options = {"data_root": root, "env": {}, "version": "test"}
    link = DaemonLink(daemon=options, auto_start=False)
    try:
        with pytest.raises(RpcError, match="missing"):
            await link("status", {})
        assert calls == 1

        async def disconnect(client):
            nonlocal calls
            calls += 1
            raise RpcError(-32801, "connection closed")

        with pytest.raises(RpcError):
            await link.with_client(disconnect)
        assert calls == 3
    finally:
        await link.close()
        await daemon.close()


async def test_progress_filtered_unsubscribed_and_cancelled_without_run_cancel(root):
    entered = asyncio.Event()
    disconnected = asyncio.Event()
    calls = []

    async def wait(params, ctx):
        calls.append("wait")
        entered.set()
        ctx.notify("progress", {"run_id": "other", "waiting_ms": 1})
        ctx.notify("progress", {"run_id": "r", "waiting_ms": "bad"})
        ctx.notify("progress", {"run_id": "r", "waiting_ms": 2})
        await ctx.signal.wait()
        disconnected.set()
        return {}

    daemon = await start_daemon(data_root=root, env={}, version="test", handlers={"wait": wait})
    link = DaemonLink(daemon={"data_root": root, "env": {}, "version": "test"}, auto_start=False)
    progress = []

    async def collect(value):
        progress.append(value)

    pending = asyncio.create_task(link("wait", {"run_id": "r"}, on_progress=collect))
    try:
        await entered.wait()
        await asyncio.sleep(0.01)
        assert progress == [{"run_id": "r", "waiting_ms": 2}]
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert link._client.rpc._listeners == []
        assert link._client.rpc._pending == {}
        assert await link("status", {}) == []
        assert calls == ["wait"]
        assert not disconnected.is_set()
    finally:
        await link.close()
        await daemon.close()
    assert disconnected.is_set()


async def test_dead_daemon_errors_and_child_never_autostarts(root):
    options = {"data_root": root, "env": {}, "version": "test"}
    servers = [
        create_mcp_server(daemon=options, auto_start=False),
        create_unit_mcp_server(daemon=options, token="secret"),
    ]
    for server, tool, args in [
        (servers[0], "wise_status", {}),
        (servers[1], "wise_context", {"key": "ticket"}),
    ]:
        async with McpClient(server, mode="legacy") as client:
            error = body(await client.call_tool(tool, args))["error"]
            assert error["code"] == error["cause"] == "DAEMON_UNAVAILABLE"
            assert "/wise-init" in error["hint"]
    assert list(Path(root).iterdir()) == []


async def test_child_environment_token_and_socket(monkeypatch, root):
    from wise_engine.version import source_build_id

    calls = []
    daemon = await start_daemon(
        data_root=root,
        env={},
        version=source_build_id(),
        handlers={"child_context": lambda params, ctx: calls.append(params) or {"value": "ok"}},
    )
    monkeypatch.setenv("WISE_ENGINE_SOCKET", daemon.runtime.paths.socket_path)
    monkeypatch.setenv("WISE_DATA_ROOT", root)
    monkeypatch.setenv("WISE_STEP_TOKEN", "from-env")
    try:
        async with McpClient(create_unit_mcp_server(), mode="legacy") as client:
            assert body(await client.call_tool("wise_context", {"key": "ticket"})) == {
                "value": "ok"
            }
        assert calls == [{"token": "from-env", "key": "ticket"}]
    finally:
        await daemon.close()


async def test_real_host_death_closes_stdio_and_daemon_socket(root):
    import sys
    from wise_engine.paths import ENGINE_ROOT

    daemon = await start_daemon(data_root=root, env={}, version="test")
    child_code = (
        "import sys,asyncio;"
        f"sys.path.insert(0,{str(ENGINE_ROOT)!r});"
        "from wise_engine.mcp_server import create_mcp_server,serve_stdio;"
        f"asyncio.run(serve_stdio(create_mcp_server(daemon={{'data_root':{root!r},'env':{{}},'version':'test'}},auto_start=False),host_poll_seconds=.01))"
    )
    parent_code = (
        "import subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}],stdin=sys.stdin,stdout=sys.stdout,stderr=sys.stderr);"
        "print(p.pid,file=sys.stderr,flush=True);time.sleep(30)"
    )
    parent = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        parent_code,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    import os

    held_stdin = os.dup(parent.stdin.transport.get_extra_info("pipe").fileno())
    child_pid = int(await asyncio.wait_for(parent.stderr.readline(), 2))
    try:

        async def send(message):
            parent.stdin.write((json.dumps(message) + "\n").encode())
            await parent.stdin.drain()

        await send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
        )
        assert json.loads(await asyncio.wait_for(parent.stdout.readline(), 2))["id"] == 1
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "wise_status", "arguments": {}},
            }
        )
        assert json.loads(await asyncio.wait_for(parent.stdout.readline(), 2))["id"] == 2
        assert daemon.connections() == 1
        parent.kill()
        await asyncio.wait_for(parent.wait(), 2)
        assert os.fstat(held_stdin).st_mode
        assert await asyncio.wait_for(parent.stdout.read(), 2) == b""
        await asyncio.sleep(0.02)
        assert daemon.connections() == 0
    finally:
        import os
        import signal

        if parent.returncode is None:
            parent.kill()
            await parent.wait()
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        parent.stdin.close()
        os.close(held_stdin)
        await daemon.close()
