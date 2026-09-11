from __future__ import annotations

import asyncio
import codecs
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..scheduler import JS_WHITESPACE
from ..spawn import SpawnOptions, clean_env, spawn_clean
from .agent import DEFAULT_STEP_TIMEOUT_MS, headline, utf16_length, utf16_slice

STDERR_TAIL = 800
STDOUT_CAP = 1024 * 1024
Json = dict[str, Any]


@dataclass
class BashHandle:
    pid: int
    kill: Callable[..., Any]
    result: asyncio.Task[Json]


def _last_line(text: str) -> str:
    return next(
        (
            line.strip(JS_WHITESPACE)
            for line in reversed(text.split("\n"))
            if line.strip(JS_WHITESPACE)
        ),
        "",
    )


async def start_bash_step(step: Json, opts: Json) -> BashHandle:
    timeout_ms = (
        step["timeout"] * 1000
        if "timeout" in step
        else opts.get("default_timeout_ms", DEFAULT_STEP_TIMEOUT_MS)
    )
    proc = await spawn_clean(
        "bash",
        ["-c", step["run"]],
        SpawnOptions(
            cwd=opts["cwd"], env=clean_env(parent=opts.get("parent_env")), timeout_ms=timeout_ms
        ),
    )
    proc.stdin.end()

    async def finish() -> Json:
        chunks = []
        remaining = STDOUT_CAP
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while chunk := await proc.stdout.read(65536):
                text = decoder.decode(chunk)
                if remaining:
                    clipped = utf16_slice(text, 0, remaining)
                    chunks.append(clipped)
                    remaining -= utf16_length(clipped)
            final = decoder.decode(b"", final=True)
            if remaining:
                chunks.append(utf16_slice(final, 0, remaining))
            exit = await proc.exited
        except asyncio.CancelledError:
            proc.exited.cancel()
            await asyncio.gather(proc.exited, return_exceptions=True)
            raise
        stdout = "".join(chunks).strip(JS_WHITESPACE)
        ok = exit.code == 0 and not exit.timed_out and exit.error is None
        outputs = {}
        if ok and step.get("outputs"):
            outputs[step["outputs"][0]] = stdout
        result = dict(
            ok=ok,
            code=exit.code,
            timed_out=exit.timed_out,
            stdout=stdout,
            stderr=exit.stderr,
            outputs=outputs,
        )
        if ok:
            return {**result, "verdict": headline(_last_line(stdout)) or "ok"}
        tail = utf16_slice(exit.stderr.strip(JS_WHITESPACE), -STDERR_TAIL)
        error = (
            f"timed out after {timeout_ms} ms"
            if exit.timed_out
            else f"spawn failed: {exit.error}"
            if exit.error is not None
            else tail
            or f"exit code {exit.code if exit.code is not None else 'null'}"
            + (f" ({exit.signal})" if exit.signal else "")
        )
        return {
            **result,
            "verdict": headline(f"failed: {_last_line(error) or error}"),
            "error": error,
        }

    return BashHandle(proc.pid, proc.kill, asyncio.create_task(finish()))


async def run_bash_step(step: Json, opts: Json) -> Json:
    return await (await start_bash_step(step, opts)).result
