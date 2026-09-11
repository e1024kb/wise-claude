from __future__ import annotations

import asyncio
import codecs
import inspect
import itertools
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from .protocol import (
    RPC_CLIENT_DISCONNECTED,
    RPC_CLIENT_TIMEOUT,
    RPC_DOMAIN_ERROR,
    RPC_INTERNAL_ERROR,
    RPC_INVALID_REQUEST,
    RPC_METHOD_NOT_FOUND,
    RPC_PARSE_ERROR,
)
from .scheduler import JS_WHITESPACE, UNDEFINED
from .render import _json as _wire_json


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = UNDEFINED) -> None:
        super().__init__(message)
        self.code = code
        self.data = data

    def to_json(self) -> dict[str, Any]:
        result = {"code": self.code, "message": str(self)}
        if self.data is not UNDEFINED:
            result["data"] = self.data
        return result


def domain_error(code: str, message: str, extra: Mapping[str, Any] | None = None) -> RpcError:
    return RpcError(RPC_DOMAIN_ERROR, message, {**(extra or {}), "code": code})


def domain_code(error: Any) -> str | None:
    if (
        isinstance(error, RpcError)
        and error.code == RPC_DOMAIN_ERROR
        and isinstance(error.data, dict)
    ):
        code = error.data.get("code")
        if isinstance(code, str):
            return code
    return None


class LineFramer:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""

    def push(self, chunk: bytes | str) -> list[str]:
        self._buffer += chunk if isinstance(chunk, str) else self._decoder.decode(chunk)
        *lines, self._buffer = self._buffer.split("\n")
        return [line.removesuffix("\r") for line in lines if line.strip(JS_WHITESPACE)]


def encode_message(message: Mapping[str, Any]) -> bytes:
    return (_wire_json(message) + "\n").encode("utf-8", errors="backslashreplace")


def _reject_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON token {value}")


def _is_id(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


@dataclass
class CallContext:
    notify: Callable[[str, Any], None]
    signal: asyncio.Event
    connection_id: int


RpcHandler = Callable[[Any, CallContext], Any | Awaitable[Any]]
RpcHandlerMap = dict[str, RpcHandler]
_CONNECTION_IDS = itertools.count(1)


class Connection:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        handlers: RpcHandlerMap,
        *,
        connection_id: int | None = None,
        guard: Callable[[str, CallContext], None] | None = None,
        on_close: Callable[[], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.writer = writer
        self.signal = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._draining: asyncio.Task[None] | None = None
        self._on_close = on_close
        self._on_error = on_error
        self._guard = guard
        self._handlers = handlers
        self.context = CallContext(self.notify, self.signal, connection_id or next(_CONNECTION_IDS))
        self.task = asyncio.create_task(self._read(reader))

    def _write(self, message: dict[str, Any]) -> None:
        if self.writer.is_closing():
            return
        try:
            self.writer.write(encode_message(message))
            if self._draining is None or self._draining.done():
                self._draining = asyncio.create_task(self._drain())
        except (OSError, RuntimeError):
            self.close()

    async def _drain(self) -> None:
        try:
            await self.writer.drain()
        except (OSError, RuntimeError):
            self.close()

    def notify(self, method: str, params: Any = UNDEFINED) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _error(self, id: Any, error: RpcError) -> None:
        self._write({"jsonrpc": "2.0", "id": id, "error": error.to_json()})

    async def _dispatch(self, message: dict[str, Any]) -> None:
        id = message.get("id")
        has_id = id is not None
        id = id if _is_id(id) else None
        method = message.get("method")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            if has_id or "method" not in message:
                self._error(id, RpcError(RPC_INVALID_REQUEST, "invalid request"))
            return
        try:
            if self._guard:
                self._guard(method, self.context)
            handler = self._handlers.get(method)
            if handler is None:
                raise RpcError(RPC_METHOD_NOT_FOUND, f"method not found: {method}")
            result = handler(message.get("params", UNDEFINED), self.context)
            if inspect.isawaitable(result):
                result = await result
            if has_id:
                self._write(
                    {"jsonrpc": "2.0", "id": id, "result": None if result is UNDEFINED else result}
                )
        except Exception as error:
            if has_id:
                self._error(
                    id,
                    error
                    if isinstance(error, RpcError)
                    else RpcError(RPC_INTERNAL_ERROR, str(error)),
                )
        try:
            await self.writer.drain()
        except (OSError, RuntimeError):
            self.close()

    async def _read(self, reader: asyncio.StreamReader) -> None:
        framer = LineFramer()
        try:
            while chunk := await reader.read(65536):
                for line in framer.push(chunk):
                    try:
                        message = json.loads(line, parse_constant=_reject_constant)
                    except ValueError:
                        self._error(None, RpcError(RPC_PARSE_ERROR, "parse error"))
                        continue
                    if not isinstance(message, dict):
                        self._error(None, RpcError(RPC_INVALID_REQUEST, "invalid request"))
                        continue
                    task = asyncio.create_task(self._dispatch(message))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
        except (OSError, RuntimeError) as error:
            if self._on_error:
                self._on_error(error)
        finally:
            self.close()
            if self._tasks:
                await asyncio.sleep(0)
                for task in self._tasks:
                    task.cancel()
                await asyncio.gather(*self._tasks, return_exceptions=True)
            if self._draining:
                await asyncio.gather(self._draining, return_exceptions=True)
            try:
                await self.writer.wait_closed()
            except OSError:
                pass

    def close(self) -> None:
        if self.signal.is_set():
            return
        self.signal.set()
        self.writer.close()
        if self._on_close:
            self._on_close()


def serve_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    handlers: RpcHandlerMap,
    **options: Any,
) -> Connection:
    return Connection(reader, writer, handlers, **options)


class RpcClient:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        timeout_ms: float = 30_000,
    ) -> None:
        self.writer = writer
        self.default_timeout_ms = timeout_ms
        self.is_closed = False
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._next_id = itertools.count(1)
        self.task = asyncio.create_task(self._read(reader))

    def on_notification(self, listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        if listener not in self._listeners:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def notify(self, method: str, params: Any = UNDEFINED) -> None:
        if not self.is_closed:
            self.writer.write(
                encode_message({"jsonrpc": "2.0", "method": method, "params": params})
            )

    async def call(
        self, method: str, params: Any = UNDEFINED, *, timeout_ms: float | None = None
    ) -> Any:
        if self.is_closed:
            raise RpcError(RPC_CLIENT_DISCONNECTED, "connection closed")
        id = next(self._next_id)
        future = asyncio.get_running_loop().create_future()
        self._pending[id] = future
        timeout = self.default_timeout_ms if timeout_ms is None else timeout_ms
        try:
            self.writer.write(
                encode_message({"jsonrpc": "2.0", "id": id, "method": method, "params": params})
            )
            async with asyncio.timeout(timeout / 1000 if timeout > 0 else None):
                await self.writer.drain()
                return await future
        except TimeoutError:
            raise RpcError(RPC_CLIENT_TIMEOUT, f"{method}: no reply in {timeout:g} ms") from None
        except OSError:
            self.close()
            raise RpcError(RPC_CLIENT_DISCONNECTED, "connection closed") from None
        finally:
            self._pending.pop(id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def _read(self, reader: asyncio.StreamReader) -> None:
        framer = LineFramer()
        try:
            while chunk := await reader.read(65536):
                for line in framer.push(chunk):
                    try:
                        message = json.loads(line, parse_constant=_reject_constant)
                    except ValueError:
                        continue
                    if not isinstance(message, dict):
                        continue
                    if isinstance(message.get("method"), str):
                        for listener in tuple(self._listeners):
                            listener({"method": message["method"], "params": message.get("params")})
                        continue
                    id = message.get("id")
                    if not _is_id(id):
                        continue
                    future = self._pending.get(cast(int, id))
                    if future is None or future.done():
                        continue
                    error = message.get("error")
                    if error:
                        future.set_exception(
                            RpcError(error["code"], error["message"], error.get("data", UNDEFINED))
                        )
                    else:
                        future.set_result(message.get("result", UNDEFINED))
        except (OSError, RuntimeError):
            pass
        finally:
            self.close()

    def close(self) -> None:
        if self.is_closed:
            return
        self.is_closed = True
        self.writer.close()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RpcError(RPC_CLIENT_DISCONNECTED, "connection closed"))
        self._pending.clear()
