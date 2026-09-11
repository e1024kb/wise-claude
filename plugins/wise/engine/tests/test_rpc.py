from __future__ import annotations

import asyncio
import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from wise_engine.protocol import RPC_CLIENT_DISCONNECTED, RPC_CLIENT_TIMEOUT
from wise_engine.rpc import (
    LineFramer,
    RpcClient,
    RpcError,
    domain_code,
    domain_error,
    encode_message,
    serve_connection,
)
from wise_engine.scheduler import UNDEFINED


@asynccontextmanager
async def pair(handlers, **options):
    with tempfile.TemporaryDirectory(prefix="wr-", dir="/tmp") as directory:
        path = str(Path(directory) / "s")
        connections = []

        def accept(reader, writer):
            connections.append(serve_connection(reader, writer, handlers, **options))

        server = await asyncio.start_unix_server(accept, path=path)
        reader, writer = await asyncio.open_unix_connection(path)
        try:
            yield reader, writer, connections
        finally:
            writer.close()
            await writer.wait_closed()
            for connection in connections:
                connection.close()
            await asyncio.gather(*(c.task for c in connections), return_exceptions=True)
            server.close()
            await server.wait_closed()


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_framing():
    framer = LineFramer()
    assert framer.push(b'{"a":') == []
    assert framer.push(b'1}\n{"b":2}\n{"c"') == ['{"a":1}', '{"b":2}']
    assert framer.push(b":3}\r\n\n") == ['{"c":3}']
    line = json.dumps({"t": "héllo ✅ жизнь"}, ensure_ascii=False) + "\n"
    raw = line.encode()
    for cut in range(1, len(raw)):
        framer = LineFramer()
        assert framer.push(raw[:cut]) + framer.push(raw[cut:]) == [line.rstrip()]
    assert encode_message({"jsonrpc": "2.0", "params": UNDEFINED}) == b'{"jsonrpc":"2.0"}\n'


@pytest.mark.anyio
async def test_split_and_multiple_requests():
    async with pair({"echo": lambda p, _: p}) as (reader, writer, _):
        wire = encode_message({"jsonrpc": "2.0", "id": 7, "method": "echo", "params": {"a": 3}})
        writer.write(wire[:15])
        await asyncio.sleep(0.01)
        writer.write(wire[15:])
        assert json.loads(await reader.readline()) == {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {"a": 3},
        }
        writer.write(
            b'{"jsonrpc":"2.0","id":"a","method":"echo","params":1}\n{"jsonrpc":"2.0","id":"b","method":"echo","params":[2]}\n'
        )
        assert [json.loads(await reader.readline())["result"] for _ in range(2)] == [1, [2]]


@pytest.mark.anyio
async def test_error_envelopes_and_guard():
    def fail_domain(p, c):
        raise domain_error("RUN_NOT_FOUND", "no run", {"run_id": "x"})

    def fail_plain(p, c):
        raise ValueError("boom")

    async with pair({"fail_domain": fail_domain, "fail_plain": fail_plain}) as (reader, writer, _):
        writer.write(
            b'bad\n{"jsonrpc":"1.0","id":2}\n{"jsonrpc":"2.0","id":3,"method":"nope"}\n{"jsonrpc":"2.0","id":4,"method":"fail_domain"}\n{"jsonrpc":"2.0","id":5,"method":"fail_plain"}\n[]\n'
        )
        errors = [json.loads(await reader.readline()) for _ in range(6)]
        assert sorted(e["error"]["code"] for e in errors) == sorted(
            [-32700, -32600, -32601, -32000, -32603, -32600]
        )
        assert next(e for e in errors if e["id"] == 4)["error"]["data"] == {
            "run_id": "x",
            "code": "RUN_NOT_FOUND",
        }
    greeted = False

    def guard(method, ctx):
        if not greeted and method != "hello":
            raise RpcError(-32600, "hello first")

    def hello(p, c):
        nonlocal greeted
        greeted = True
        return "hi"

    async with pair({"hello": hello, "echo": lambda p, _: p}, guard=guard) as (reader, writer, _):
        client = RpcClient(reader, writer)
        with pytest.raises(RpcError, match="hello first"):
            await client.call("echo", 1)
        assert await client.call("hello") == "hi"
        assert await client.call("echo", 1) == 1
        client.close()
        await client.task


@pytest.mark.anyio
async def test_client_errors_notifications_and_unsubscribe():
    seen = []

    def tick(p, ctx):
        ctx.notify("progress", {"n": 1})
        ctx.notify("progress", {"n": 2})
        return "ticked"

    def fail(p, ctx):
        raise domain_error("RUN_NOT_FOUND", "no run", {"run_id": "x"})

    async with pair({"tick": tick, "note": lambda p, _: seen.append(p), "fail": fail}) as (
        reader,
        writer,
        _,
    ):
        client = RpcClient(reader, writer)
        received = []
        off = client.on_notification(received.append)
        assert await client.call("tick") == "ticked"
        assert received == [
            {"method": "progress", "params": {"n": 1}},
            {"method": "progress", "params": {"n": 2}},
        ]
        off()
        assert await client.call("tick") == "ticked"
        assert len(received) == 2
        client.notify("note", {"hello": 1})
        await asyncio.sleep(0.01)
        assert seen == [{"hello": 1}]
        with pytest.raises(RpcError) as error:
            await client.call("fail")
        assert domain_code(error.value) == "RUN_NOT_FOUND"
        with pytest.raises(RpcError) as error:
            await client.call("nope")
        assert error.value.code == -32601
        assert domain_code(error.value) is None
        client.close()
        await client.task


@pytest.mark.anyio
async def test_timeout_cancellation_disconnect_and_connection_signal():
    signal = None

    async def hang(p, ctx):
        nonlocal signal
        signal = ctx.signal
        await asyncio.Future()

    async with pair({"hang": hang, "echo": lambda p, _: p}) as (reader, writer, connections):
        client = RpcClient(reader, writer, timeout_ms=20)
        with pytest.raises(RpcError) as error:
            await client.call("hang")
        assert error.value.code == RPC_CLIENT_TIMEOUT
        with pytest.raises(RpcError):
            await client.call("hang", timeout_ms=5)
        assert not client._pending
        pending = asyncio.create_task(client.call("hang", timeout_ms=0))
        await asyncio.sleep(0.01)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not client._pending
        assert await client.call("echo", 7) == 7
        assert not signal.is_set()
        pending = asyncio.create_task(client.call("hang", timeout_ms=0))
        await asyncio.sleep(0.01)
        client.close()
        with pytest.raises(RpcError) as error:
            await pending
        assert error.value.code == RPC_CLIENT_DISCONNECTED
        await asyncio.wait_for(connections[0].task, 1)
        assert signal.is_set()
        with pytest.raises(RpcError) as error:
            await client.call("echo")
        assert error.value.code == RPC_CLIENT_DISCONNECTED
        await client.task


@pytest.mark.anyio
async def test_concurrent_requests_and_late_reply():
    async def delayed(p, ctx):
        await asyncio.sleep(0.03)
        return p

    async with pair({"delayed": delayed, "echo": lambda p, _: p}) as (reader, writer, _):
        client = RpcClient(reader, writer)
        pending = asyncio.create_task(client.call("delayed", 4))
        assert await client.call("echo", 5) == 5
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await asyncio.sleep(0.04)
        assert await client.call("echo", 6) == 6
        client.close()
        await client.task


@pytest.mark.anyio
async def test_strict_json_and_wire_numbers():
    assert (
        encode_message(
            {
                "x": float("inf"),
                "nested": {"omit": UNDEFINED},
                "array": [UNDEFINED],
                "surrogate": "\ud800",
            }
        )
        == b'{"x":null,"nested":{},"array":[null],"surrogate":"\\ud800"}\n'
    )
    async with pair({"echo": lambda p, _: p}) as (reader, writer, _):
        writer.write(b'{"jsonrpc":"2.0","id":1,"method":"echo","params":NaN}\n')
        error = json.loads(await reader.readline())
        assert error["id"] is None
        assert error["error"]["code"] == -32700


@pytest.mark.anyio
async def test_notification_failures_have_no_response_and_invalid_ids_use_null():
    async with pair({"echo": lambda p, _: p}) as (reader, writer, _):
        writer.write(
            b'{"jsonrpc":"2.0","method":"unknown"}\n{"jsonrpc":"2.0","id":true,"method":"echo","params":4}\n'
        )
        assert json.loads(await reader.readline()) == {"jsonrpc": "2.0", "id": None, "result": 4}
        writer.write(b'{"jsonrpc":"2.0","id":3,"method":"echo"}\n')
        assert json.loads(await reader.readline()) == {"jsonrpc": "2.0", "id": 3, "result": None}


def test_protocol_and_wire_fixtures():
    from wise_engine import protocol

    fixtures = Path(__file__).resolve().parents[1] / "test/fixtures/contracts"
    expected = json.loads((fixtures / "protocol-constants.json").read_text())
    assert {
        name: list(getattr(protocol, name)) if name == "METHOD_NAMES" else getattr(protocol, name)
        for name in expected
    } == expected
    for case in json.loads((fixtures / "rpc.json").read_text()):
        assert encode_message(case["message"]).decode() == case["wire"]
    assert domain_code(None) is None
    assert domain_code(ValueError("bad")) is None
    assert LineFramer().push("\ufeff\n\x1c\n") == ["\x1c"]
