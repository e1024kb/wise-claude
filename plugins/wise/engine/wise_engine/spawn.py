from __future__ import annotations

import asyncio
import codecs
import errno
import os
import re
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

PASSTHROUGH_VARS = (
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "SHELL",
    "USER",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_CONFIG_GLOBAL",
    "GNUPGHOME",
    "GPG_TTY",
    "GH_HOST",
    "GH_CONFIG_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


def is_blocked_var(name: str) -> bool:
    return (
        name == "CLAUDECODE"
        or name.startswith("CLAUDE_CODE_")
        or re.match(r"^CLAUDE_.*SESSION", name) is not None
    )


def clean_env(
    *,
    parent: Mapping[str, str | None] | None = None,
    keep: Sequence[str] = (),
    secrets: Sequence[str] = (),
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if parent is None else parent
    names = [*PASSTHROUGH_VARS, *(key for key in source if key.startswith("XDG_")), *keep, *secrets]
    result = {}
    for name in names:
        value = source.get(name)
        if value is not None and not is_blocked_var(name):
            result[name] = value
    # Explicit request variables override inherited values, including blocked names.
    result.update(extra or {})
    return result


@dataclass(frozen=True)
class SpawnOptions:
    cwd: str | os.PathLike[str]
    env: Mapping[str, str]
    timeout_ms: float = 0
    kill_grace_ms: float = 5_000
    stderr_cap: int = 64 * 1024


@dataclass(frozen=True)
class SpawnExit:
    code: int | None
    signal: str | None
    timed_out: bool
    stderr: str
    error: str | None = None


class SafeStdin:
    """A child closing its input does not interrupt the caller."""

    def __init__(self, writer: asyncio.StreamWriter | None) -> None:
        self._writer = writer

    def write(self, data: bytes | str) -> None:
        if self._writer is None or self._writer.is_closing():
            return
        try:
            self._writer.write(data.encode("utf-8") if isinstance(data, str) else data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def drain(self) -> None:
        if self._writer is not None:
            try:
                await self._writer.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()

    def end(self, data: bytes | str = b"") -> None:
        self.write(data)
        self.close()

    async def wait_closed(self) -> None:
        if self._writer is not None:
            try:
                await self._writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass


def kill_group(process: asyncio.subprocess.Process, sig: str | int = "SIGTERM") -> None:
    number = signal.Signals[sig] if isinstance(sig, str) else sig
    try:
        os.killpg(process.pid, number)
        return
    except OSError:
        pass
    try:
        process.send_signal(number)
    except ProcessLookupError:
        pass


@dataclass(frozen=True)
class Spawned:
    pid: int
    process: asyncio.subprocess.Process | None
    stdin: SafeStdin
    stdout: asyncio.StreamReader
    exited: asyncio.Future[SpawnExit]

    def kill(self, sig: str | int = "SIGTERM") -> None:
        if self.process is not None:
            kill_group(self.process, sig)


async def _capture_stderr(stream: asyncio.StreamReader, cap: int) -> str:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    chunks = []
    remaining = max(0, cap)

    def retain(text: str) -> None:
        nonlocal remaining
        if remaining:
            # The wire contract counts UTF-16 units, including half of a surrogate pair.
            encoded = text.encode("utf-16-le", "surrogatepass")[: remaining * 2]
            chunks.append(encoded.decode("utf-16-le", "surrogatepass"))
            remaining -= len(encoded) // 2

    while chunk := await stream.read(65536):
        retain(decoder.decode(chunk))
    retain(decoder.decode(b"", final=True))
    return "".join(chunks)


async def spawn_clean(cmd: str, args: Sequence[str], opts: SpawnOptions) -> Spawned:
    """Callers drain stdout while awaiting exited; timeout values use milliseconds."""
    try:
        process = await asyncio.create_subprocess_exec(
            cmd,
            *args,
            cwd=opts.cwd,
            env=dict(opts.env),
            start_new_session=True,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        failed: asyncio.Future[SpawnExit] = asyncio.get_running_loop().create_future()
        failed.set_result(
            SpawnExit(
                None,
                None,
                False,
                "",
                f"{errno.errorcode.get(exc.errno or 0, 'ERROR')}: {exc}",
            )
        )
        stdout = asyncio.StreamReader()
        stdout.feed_eof()
        return Spawned(-1, None, SafeStdin(None), stdout, failed)

    assert process.stdout is not None and process.stderr is not None
    stderr_task = asyncio.create_task(_capture_stderr(process.stderr, opts.stderr_cap))
    timed_out = False

    async def expire() -> None:
        nonlocal timed_out
        await asyncio.sleep(opts.timeout_ms / 1000)
        timed_out = True
        kill_group(process)
        await asyncio.sleep(max(0, opts.kill_grace_ms) / 1000)
        kill_group(process, "SIGKILL")

    timer = asyncio.create_task(expire()) if opts.timeout_ms > 0 else None

    async def monitor() -> SpawnExit:
        try:
            code = await process.wait()
            stderr = await stderr_task
            return SpawnExit(
                code if code >= 0 else None,
                signal.Signals(-code).name if code < 0 else None,
                timed_out,
                stderr,
            )
        except asyncio.CancelledError:
            kill_group(process, "SIGKILL")
            await process.wait()
            await asyncio.gather(stderr_task, return_exceptions=True)
            raise
        finally:
            if timer is not None:
                timer.cancel()
                await asyncio.gather(timer, return_exceptions=True)

    exited = asyncio.create_task(monitor())
    # Arm cleanup before callers can cancel their exit wait.
    try:
        await asyncio.sleep(0)
    except asyncio.CancelledError:
        exited.cancel()
        await asyncio.gather(exited, return_exceptions=True)
        raise
    return Spawned(process.pid, process, SafeStdin(process.stdin), process.stdout, exited)


class LineSplitter:
    def __init__(self) -> None:
        self._buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def feed(self, chunk: str | bytes) -> list[str]:
        self._buffer += self._decoder.decode(chunk) if isinstance(chunk, bytes) else chunk
        parts = self._buffer.split("\n")
        self._buffer = parts.pop()
        return [part.removesuffix("\r") for part in parts]

    def finish(self) -> list[str]:
        lines = self.feed(self._decoder.decode(b"", final=True))
        rest, self._buffer = self._buffer, ""
        self._decoder.reset()
        return [*lines, rest] if rest else lines


def create_line_splitter() -> LineSplitter:
    return LineSplitter()
