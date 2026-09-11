from __future__ import annotations

import asyncio
import codecs
import copy
import json
import math
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..adapter_types import AgentHandle, EventCallback, Json
from ..render import _json as wire_json
from ..spawn import LineSplitter, Spawned, SpawnExit, SpawnOptions, spawn_clean


def dumps(value: Any) -> str:
    return wire_json(value)


def loads(text: str) -> Any:
    def invalid_constant(value: str) -> Any:
        raise ValueError(f"invalid JSON constant: {value}")

    def integer(value: str) -> int | float:
        parsed = int(value)
        return parsed if abs(parsed) <= 2**53 else float(value)

    return json.loads(text, parse_constant=invalid_constant, parse_int=integer)


def string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def number(value: Any) -> int | float | None:
    return value if type(value) in (int, float) and math.isfinite(value) else None


def rec(value: Any) -> Json:
    return value if isinstance(value, dict) else {}


def clip(text: str) -> str:
    return (
        text.strip()
        .encode("utf-16-le", "surrogatepass")[:1000]
        .decode("utf-16-le", "surrogatepass")
    )


def js_string(value: Any) -> str:
    return "null" if value is None else str(value)


def empty_usage(pool: str) -> Json:
    return dict(input=0, output=0, cache_read=0, cache_write=0, pool=pool)


def token_usage(result: Json | None, pool: str) -> Json:
    source = rec(rec(result).get("usage"))
    usage = dict(
        input=number(source.get("input_tokens")) or 0,
        output=number(source.get("output_tokens")) or 0,
        cache_read=number(source.get("cache_read_input_tokens")) or 0,
        cache_write=number(source.get("cache_creation_input_tokens")) or 0,
        pool=pool,
    )
    cost = number(rec(result).get("total_cost_usd"))
    if cost is not None:
        usage["cost_usd"] = cost
    return usage


def exit_detail(exit: SpawnExit, prefix: str) -> str:
    return f"{prefix} (exit code {js_string(exit.code)}, signal {js_string(exit.signal)})"


class Parser:
    harness: str

    def __init__(self, *, pool: str, now: Callable[[], str] | None = None) -> None:
        self.pool = pool
        self.now = now or (
            lambda: datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        self.lines = LineSplitter()
        self.snap: Json = {}

    def ingest(self, line: str) -> Json:
        event: Json = dict(ts=self.now(), harness=self.harness, line=line)
        if line.strip():
            try:
                parsed = loads(line)
            except ValueError:
                return event
            event["parsed"] = parsed
            if isinstance(parsed, dict):
                self.accept(parsed)
        return event

    def accept(self, parsed: Json) -> None:
        raise NotImplementedError

    def feed(self, chunk: str | bytes) -> list[Json]:
        return [self.ingest(line) for line in self.lines.feed(chunk)]

    def flush(self) -> None:
        for line in self.lines.finish():
            self.ingest(line)

    def snapshot(self) -> Json:
        return copy.deepcopy(self.snap)

    def finish(self, exit: SpawnExit) -> Json:
        raise NotImplementedError


async def finish_process(
    proc: Spawned,
    parser: Parser,
    on_event: EventCallback,
    cleanup: Callable[[], None] | None = None,
) -> Json:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        while chunk := await proc.stdout.read(65536):
            for event in parser.feed(decoder.decode(chunk)):
                on_event(event)
        for event in parser.feed(decoder.decode(b"", final=True)):
            on_event(event)
        return parser.finish(await proc.exited)
    except BaseException:
        proc.kill("SIGKILL")
        await asyncio.shield(proc.exited)
        raise
    finally:
        if cleanup is not None:
            cleanup()


async def arm_task(
    coroutine: Coroutine[Any, Any, Json], proc: Spawned, cleanup: Callable[[], None] | None = None
) -> asyncio.Task[Json]:
    task = asyncio.create_task(coroutine)
    try:
        # Start cleanup before the caller can cancel the returned task.
        await asyncio.sleep(0)
    except asyncio.CancelledError:
        proc.kill("SIGKILL")
        task.cancel()
        await asyncio.shield(asyncio.gather(task, proc.exited, return_exceptions=True))
        if cleanup:
            cleanup()
        raise
    return task


async def probe_process(bin: str, argv: list[str], env: dict[str, str]) -> tuple[SpawnExit, str]:
    proc = await spawn_clean(bin, argv, SpawnOptions(cwd=Path.cwd(), env=env, timeout_ms=15_000))
    proc.stdin.end()
    chunks = []
    try:
        while chunk := await proc.stdout.read(65536):
            chunks.append(chunk)
        return await proc.exited, b"".join(chunks).decode("utf-8", "replace")
    except BaseException:
        proc.kill("SIGKILL")
        await asyncio.shield(proc.exited)
        raise


@dataclass
class TemporaryFile:
    path: str

    def cleanup(self) -> None:
        shutil.rmtree(Path(self.path).parent, ignore_errors=True)


def temporary_file(prefix: str, name: str, text: str) -> TemporaryFile:
    directory = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return TemporaryFile(str(path))
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


@dataclass
class ProviderAdapter:
    id: str
    bin: str | None
    start: Callable[..., Awaitable[AgentHandle]]
    probe: Callable[..., Awaitable[Json]]
    effort: Callable[[str], str | None]

    async def probe_auth(self, auth: str) -> Json:
        return await self.probe(auth)

    async def run(self, req: Json, on_event: EventCallback) -> Json:
        return await (await self.start(req, on_event)).done

    def effort_map(self, effort: str) -> str | None:
        return self.effort(effort)
