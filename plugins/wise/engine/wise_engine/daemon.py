from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ledger import append_event, read_events, read_state, reset_running, utc_now, write_state
from .paths import wise_data_root
from .protocol import (
    RPC_INVALID_PARAMS,
    RPC_INVALID_REQUEST,
    WAIT_DEFAULT_MS,
    WAIT_MAX_MS,
    WAIT_POLL_MS,
    WAIT_PROGRESS_MS,
)
from .rpc import CallContext, Connection, RpcError, RpcHandlerMap, domain_error, serve_connection
from .scheduler import JS_WHITESPACE, UNDEFINED
from .version import source_build_id

IDLE_MS_DEFAULT = 30 * 60 * 1000
LOG_ROTATE_BYTES = 10 * 1024 * 1024
ACTIVE_RUN = frozenset(("initializing", "running"))
DONE_RUN = frozenset(("completed", "failed", "cancelled"))


@dataclass(frozen=True)
class DaemonPaths:
    data_root: str
    runs_root: str
    socket_path: str
    lock_path: str
    log_path: str


def daemon_paths(
    *,
    env: Mapping[str, str] | None = None,
    home: str | None = None,
    data_root: str | None = None,
    socket_path: str | None = None,
    lock_path: str | None = None,
    log_path: str | None = None,
) -> DaemonPaths:
    values = dict(os.environ if env is None else env)
    if home is not None:
        values["HOME"] = home
    root = data_root if data_root is not None else str(wise_data_root(values))
    runtime = values.get("XDG_RUNTIME_DIR")
    return DaemonPaths(
        root,
        str(Path(root) / "runs"),
        socket_path
        if socket_path is not None
        else str(Path(runtime) / "wise/engined.sock" if runtime else Path(root) / "engined.sock"),
        lock_path if lock_path is not None else str(Path(root) / "engined.lock"),
        log_path if log_path is not None else str(Path(root) / "engined.log"),
    )


def rotate_log(log_path: str, limit: int = LOG_ROTATE_BYTES) -> bool:
    try:
        if Path(log_path).stat().st_size <= limit:
            return False
        os.replace(log_path, log_path + ".1")
        return True
    except OSError:
        return False


def _positive_integer(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
        and value == int(value)
    )


def pid_alive(pid: int) -> bool:
    if not _positive_integer(pid):
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False


def group_alive(pgid: int) -> bool:
    if not _positive_integer(pgid):
        return False
    try:
        os.killpg(int(pgid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False


def kill_group(pgid: int, sig: int | str = signal.SIGTERM) -> bool:
    if not _positive_integer(pgid):
        return False
    try:
        os.killpg(int(pgid), getattr(signal, sig) if isinstance(sig, str) else sig)
        return True
    except (OSError, OverflowError, AttributeError, ValueError):
        return False


class DaemonError(Exception):
    def __init__(self, code: str, message: str, pid: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.pid = pid


def read_lock(lock_path: str) -> int | None:
    try:
        match = re.match(r"([+-]?[0-9]+)", Path(lock_path).read_text().strip(JS_WHITESPACE))
        pid = int(match[1]) if match else 0
        return pid if pid > 0 else None
    except (OSError, ValueError):
        return None


def acquire_lock(lock_path: str) -> None:
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for _ in range(3):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            holder = read_lock(lock_path)
            if holder is not None and pid_alive(holder):
                raise DaemonError(
                    "ALREADY_RUNNING",
                    f"wise-engined already running (pid {holder}, lock {lock_path})",
                    holder,
                ) from None
            path.unlink(missing_ok=True)
            continue
        except OSError as error:
            raise DaemonError("LOCK_FAILED", f"cannot create {lock_path}: {error}") from error
        try:
            os.write(fd, f"{os.getpid()}\n".encode())
        finally:
            os.close(fd)
        return
    raise DaemonError("LOCK_FAILED", f"could not take {lock_path} after 3 attempts")


def release_lock(lock_path: str) -> None:
    if read_lock(lock_path) == os.getpid():
        Path(lock_path).unlink(missing_ok=True)


def child_path(run_dir: str) -> str:
    return str(Path(run_dir) / "daemon.json")


def _normalize_record(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if any(
        not isinstance(value.get(key), (int, float)) or isinstance(value.get(key), bool)
        for key in ("pgid", "pid")
    ):
        return None
    return {
        "pgid": value["pgid"],
        "pid": value["pid"],
        "started_at": value.get("started_at") if isinstance(value.get("started_at"), str) else "",
    }


def write_children(run_dir: str, children: list[dict[str, Any]]) -> None:
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    path = child_path(run_dir)
    temporary = path + ".tmp"
    try:
        Path(temporary).write_text(json.dumps({"children": children}, separators=(",", ":")) + "\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_children(run_dir: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(Path(child_path(run_dir)).read_text())
        values = parsed.get("children") if isinstance(parsed, dict) else None
        return [
            record
            for value in (values if isinstance(values, list) else [parsed])
            if (record := _normalize_record(value)) is not None
        ]
    except (OSError, ValueError):
        return []


def record_child(run_dir: str, child: dict[str, Any]) -> dict[str, Any]:
    record = {
        "pgid": child["pgid"],
        "pid": child["pid"],
        "started_at": child.get("started_at") if child.get("started_at") is not None else utc_now(),
    }
    write_children(
        run_dir, [c for c in read_children(run_dir) if c["pid"] != record["pid"]] + [record]
    )
    return record


def read_child(run_dir: str) -> dict[str, Any] | None:
    children = read_children(run_dir)
    return children[-1] if children else None


def clear_child(run_dir: str, pid: int | None = None) -> None:
    left = (
        [child for child in read_children(run_dir) if child["pid"] != pid]
        if pid is not None
        else []
    )
    if left:
        write_children(run_dir, left)
    else:
        Path(child_path(run_dir)).unlink(missing_ok=True)


def list_run_dirs(runs_root: str) -> list[str]:
    try:
        slugs = sorted(Path(runs_root).iterdir())
    except OSError:
        return []
    result: list[str] = []
    for slug in slugs:
        try:
            result.extend(
                str(path) for path in sorted(slug.iterdir()) if (path / "state.json").exists()
            )
        except OSError:
            continue
    return result


def find_run_dir(runs_root: str, run_id: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        return None
    try:
        for slug in Path(runs_root).iterdir():
            path = slug / run_id
            if (path / "state.json").exists():
                return str(path)
    except OSError:
        pass
    return None


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: state[key]
        for key in ("run_id", "status", "started_at", "last_activity_at", "cwd")
        if key in state
    }
    result["workflow"] = (state.get("workflow") or {}).get("name") or ""
    if "completed_at" in state:
        result["completed_at"] = state["completed_at"]
    if state.get("gate"):
        result["gate"] = state["gate"]
    return result


def read_state_safe(run_dir: str) -> dict[str, Any] | None:
    try:
        return read_state(run_dir)
    except Exception:
        return None


def recover_runs(runs_root: str, log: Callable[[str], None] = lambda _: None) -> dict[str, Any]:
    report: dict[str, Any] = {"paused": [], "killed": []}
    for directory in list_run_dirs(runs_root):
        for child in read_children(directory):
            if group_alive(child["pgid"]) and kill_group(child["pgid"]):
                report["killed"].append(child["pgid"])
        clear_child(directory)
        state = read_state_safe(directory)
        if not state or state["status"] != "running":
            continue
        reset = reset_running(directory)
        reset["status"] = "paused"
        write_state(directory, reset)
        append_event(
            directory,
            {"run_id": reset["run_id"], "type": "warn", "message": "daemon restarted, run paused"},
        )
        report["paused"].append(reset["run_id"])
        log(f"recovery: run {reset['run_id']} paused")
    return report


@dataclass
class DaemonRuntime:
    paths: DaemonPaths
    version: str
    pid: int
    started_at: str
    log: Callable[[str], None]

    def list_run_dirs(self) -> list[str]:
        return list_run_dirs(self.paths.runs_root)

    def find_run_dir(self, run_id: str) -> str | None:
        return find_run_dir(self.paths.runs_root, run_id)

    def require_run_dir(self, run_id: str) -> str:
        directory = self.find_run_dir(run_id)
        if directory is None:
            raise domain_error("RUN_NOT_FOUND", f"no such run: {run_id}", {"run_id": run_id})
        return directory


def as_record(params: Any, method: str) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise RpcError(RPC_INVALID_PARAMS, f"{method}: params must be an object")
    return params


def require_string(record: dict[str, Any], key: str, method: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or value == "":
        raise RpcError(RPC_INVALID_PARAMS, f"{method}: {key} must be a non-empty string")
    return value


def optional_number(record: dict[str, Any], key: str, method: str) -> float | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise RpcError(RPC_INVALID_PARAMS, f"{method}: {key} must be a number")
    return value


async def sleep(ms: float, stopped: asyncio.Event) -> None:
    if stopped.is_set() or ms <= 0:
        return
    try:
        await asyncio.wait_for(stopped.wait(), ms / 1000)
    except TimeoutError:
        pass


def wait_snapshot(run_dir: str, after: float) -> dict[str, Any]:
    state = read_state(run_dir)
    result = {
        "events": read_events(run_dir, math.floor(after)),
        "status": state["status"],
        "done": state["status"] in DONE_RUN,
    }
    if state["status"] == "gated" and state.get("gate"):
        result["gate"] = state["gate"]
    return result


def ledger_handlers(rt: DaemonRuntime, tuning: dict[str, Any] | None = None) -> RpcHandlerMap:
    tuning = tuning or {}
    poll_ms = tuning.get("poll_ms", WAIT_POLL_MS)
    progress_ms = tuning.get("progress_ms", WAIT_PROGRESS_MS)

    def status(params: Any, _: CallContext) -> Any:
        record = {} if params is UNDEFINED else as_record(params, "status")
        if "run_id" in record:
            return summarize(
                read_state(rt.require_run_dir(require_string(record, "run_id", "status")))
            )
        states = [
            summarize(state)
            for directory in rt.list_run_dirs()
            if (state := read_state_safe(directory)) is not None
        ]
        return sorted(states, key=lambda state: state.get("last_activity_at", ""), reverse=True)

    async def wait(params: Any, ctx: CallContext) -> Any:
        record = as_record(params, "wait")
        run_id = require_string(record, "run_id", "wait")
        after = optional_number(record, "after", "wait") or 0
        requested = optional_number(record, "timeout_ms", "wait")
        timeout = min(max(0, WAIT_DEFAULT_MS if requested is None else requested), WAIT_MAX_MS)
        directory = rt.require_run_dir(run_id)
        loop = asyncio.get_running_loop()
        start = last_progress = loop.time() * 1000
        while True:
            snapshot = wait_snapshot(directory, after)
            if snapshot["events"] or snapshot["done"] or snapshot["status"] in ("gated", "paused"):
                return snapshot
            now = loop.time() * 1000
            elapsed = now - start
            if elapsed >= timeout or ctx.signal.is_set():
                return snapshot
            if now - last_progress >= progress_ms:
                ctx.notify("progress", {"run_id": run_id, "waiting_ms": int(elapsed)})
                last_progress = now
            await sleep(min(poll_ms, timeout - elapsed), ctx.signal)

    def cancel(params: Any, _: CallContext) -> Any:
        record = as_record(params, "cancel")
        run_id = require_string(record, "run_id", "cancel")
        reason = record.get("reason") if isinstance(record.get("reason"), str) else None
        directory = rt.require_run_dir(run_id)
        state = read_state(directory)
        if state["status"] == "cancelled":
            return {"status": "cancelled"}
        if state["status"] == "completed":
            raise RpcError(RPC_INVALID_PARAMS, f"cancel: run {run_id} is already completed")
        for child in read_children(directory):
            if group_alive(child["pgid"]):
                kill_group(child["pgid"])
        clear_child(directory)
        now = utc_now()
        for step in state["steps"].values():
            if step["status"] == "running":
                step.update(status="cancelled", completed_at=now)
        state.update(status="cancelled", completed_at=now, last_activity_at=now)
        state.pop("gate", None)
        write_state(directory, state)
        event = {"run_id": state["run_id"], "type": "run.done", "verdict": "cancelled"}
        if reason:
            event["message"] = reason
        append_event(directory, event)
        rt.log(f"cancel: run {run_id}" + (f" ({reason})" if reason else ""))
        return {"status": "cancelled"}

    def resume(params: Any, _: CallContext) -> Any:
        record = as_record(params, "resume")
        run_id = require_string(record, "run_id", "resume")
        directory = rt.require_run_dir(run_id)
        state = read_state(directory)
        if state["status"] in DONE_RUN and state["status"] != "failed":
            raise RpcError(RPC_INVALID_PARAMS, f"resume: run {run_id} is {state['status']}")
        if state["status"] == "gated":
            raise RpcError(RPC_INVALID_PARAMS, f"resume: run {run_id} is gated, answer it instead")
        if state["status"] == "running":
            return {"run_id": run_id, "status": "running"}
        reset = reset_running(directory)
        append_event(
            directory, {"run_id": reset["run_id"], "type": "warn", "message": "run resumed"}
        )
        rt.log(f"resume: run {run_id}")
        return {"run_id": reset["run_id"], "status": reset["status"]}

    def stub(method: str) -> Callable[[Any, CallContext], Any]:
        def handler(_params: Any, _ctx: CallContext) -> Any:
            raise domain_error(
                "NOT_IMPLEMENTED",
                f"{method}: not implemented in this daemon build",
                {"method": method},
            )

        return handler

    return {
        **{
            name: stub(name)
            for name in (
                "preflight",
                "run",
                "answer",
                "report",
                "nudge",
                "child_report",
                "child_ask",
                "child_context",
                "child_checkpoint",
            )
        },
        "status": status,
        "wait": wait,
        "cancel": cancel,
        "resume": resume,
    }


class Daemon:
    def __init__(
        self,
        runtime: DaemonRuntime,
        handlers: RpcHandlerMap,
        idle_ms: float,
        is_busy: Callable[[], bool] | None,
    ) -> None:
        self.runtime = runtime
        self.handlers = handlers
        self.closed: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._connections: set[Connection] = set()
        self._server: asyncio.Server | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[None] | None = None
        self._last_busy = asyncio.get_running_loop().time()
        self._shutdown_requested = False
        self._idle_ms = idle_ms
        self._is_busy = is_busy

    def connections(self) -> int:
        return len(self._connections)

    def active_runs(self) -> int:
        return sum(
            1
            for directory in self.runtime.list_run_dirs()
            if (state := read_state_safe(directory)) and state.get("status") in ACTIVE_RUN
        )

    async def close(self, reason: str = "closed") -> None:
        if self._closing is None:
            self._closing = asyncio.create_task(self._close(reason))
        await asyncio.shield(self._closing)

    async def _close(self, reason: str) -> None:
        if self._ticker:
            self._ticker.cancel()
        connections = tuple(self._connections)
        for connection in connections:
            connection.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if connections:
            await asyncio.gather(*(c.task for c in connections), return_exceptions=True)
        Path(self.runtime.paths.socket_path).unlink(missing_ok=True)
        release_lock(self.runtime.paths.lock_path)
        self.runtime.log(f"engined: closed ({reason})")
        if not self.closed.done():
            self.closed.set_result(reason)

    async def _tick(self) -> None:
        tick_ms = max(25, min(math.floor(self._idle_ms / 4), 5000))
        while True:
            await asyncio.sleep(tick_ms / 1000)
            runs = self.active_runs()
            if self._shutdown_requested and runs == 0:
                await self.close("shutdown")
                return
            if self._connections or runs or (self._is_busy and self._is_busy()):
                self._last_busy = asyncio.get_running_loop().time()
                continue
            if (asyncio.get_running_loop().time() - self._last_busy) * 1000 >= self._idle_ms:
                await self.close("idle")
                return

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._last_busy = asyncio.get_running_loop().time()
        greeted = False

        def hello(params: Any, _ctx: CallContext) -> Any:
            nonlocal greeted
            record = as_record(params, "hello")
            if not isinstance(record.get("version"), str) or not isinstance(
                record.get("client"), str
            ):
                raise RpcError(RPC_INVALID_PARAMS, "hello: version and client are required")
            if record["version"] != self.runtime.version:
                raise domain_error(
                    "DAEMON_VERSION_MISMATCH",
                    f"daemon {self.runtime.version} does not match client {record['version']}",
                    {
                        "daemon_version": self.runtime.version,
                        "client_version": record["version"],
                        "pid": self.runtime.pid,
                    },
                )
            greeted = True
            return {
                "version": self.runtime.version,
                "pid": self.runtime.pid,
                "started_at": self.runtime.started_at,
            }

        def shutdown(params: Any, _ctx: CallContext) -> Any:
            record = {} if params is UNDEFINED else as_record(params, "shutdown")
            runs = self.active_runs()
            self._shutdown_requested = True
            if record.get("when") == "now" or runs == 0:
                asyncio.get_running_loop().call_soon(
                    lambda: asyncio.create_task(self.close("shutdown"))
                )
            return {"accepted": True, "active_runs": runs}

        def guard(method: str, _ctx: CallContext) -> None:
            if not greeted and method not in ("hello", "shutdown"):
                raise RpcError(RPC_INVALID_REQUEST, f"hello required before {method}")

        def on_close() -> None:
            self._connections.discard(connection)
            self._last_busy = asyncio.get_running_loop().time()

        connection = serve_connection(
            reader,
            writer,
            {**self.handlers, "hello": hello, "shutdown": shutdown},
            guard=guard,
            on_close=on_close,
            on_error=lambda error: self.runtime.log(f"engined: connection error {error}"),
        )
        self._connections.add(connection)


async def start_daemon(
    *,
    env: Mapping[str, str] | None = None,
    home: str | None = None,
    data_root: str | None = None,
    socket_path: str | None = None,
    lock_path: str | None = None,
    log_path: str | None = None,
    idle_ms: float = IDLE_MS_DEFAULT,
    version: str | None = None,
    handlers: RpcHandlerMap | Callable[[DaemonRuntime], RpcHandlerMap] | None = None,
    is_busy: Callable[[], bool] | None = None,
    log: Callable[[str], None] | None = None,
    wait: dict[str, Any] | None = None,
) -> Daemon:
    paths = daemon_paths(
        env=env,
        home=home,
        data_root=data_root,
        socket_path=socket_path,
        lock_path=lock_path,
        log_path=log_path,
    )
    Path(paths.data_root).mkdir(parents=True, exist_ok=True, mode=0o700)
    Path(paths.socket_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    acquire_lock(paths.lock_path)
    daemon: Daemon | None = None
    try:
        runtime = DaemonRuntime(
            paths,
            version if version is not None else source_build_id(),
            os.getpid(),
            utc_now(),
            log or (lambda _: None),
        )
        recover_runs(paths.runs_root, runtime.log)
        injected = handlers(runtime) if callable(handlers) else handlers or {}
        daemon = Daemon(runtime, {**ledger_handlers(runtime, wait), **injected}, idle_ms, is_busy)
        Path(paths.socket_path).unlink(missing_ok=True)
        daemon._server = await asyncio.start_unix_server(daemon._accept, path=paths.socket_path)
        os.chmod(paths.socket_path, 0o600)
        daemon._ticker = asyncio.create_task(daemon._tick())
        runtime.log(
            f"engined: listening {paths.socket_path} (pid {runtime.pid}, v{runtime.version})"
        )
        return daemon
    except BaseException:
        if daemon and daemon._server:
            daemon._server.close()
            await daemon._server.wait_closed()
            Path(paths.socket_path).unlink(missing_ok=True)
        release_lock(paths.lock_path)
        raise


DAEMON_USAGE = """wise-engine daemon <serve|start|stop|status> [options]

  serve    run in the foreground (what the detached start launches)
  start    start a detached daemon if none answers; prints its status
  stop     ask the daemon to exit when idle (--now: exit immediately)
  status   socket alive, pid, version

Options: --data-root <dir> --socket <path> --lock <path> --log <path> --idle-ms <n> --json
"""


def parse_daemon_args(argv: list[str]) -> dict[str, Any]:
    sub = argv[0] if argv else "help"
    flags: dict[str, Any] = {}
    index = 1
    while index < len(argv):
        token = argv[index]
        if token.startswith("--"):
            if "=" in token:
                key, value = token[2:].split("=", 1)
                flags[key] = value
            elif index + 1 < len(argv) and not argv[index + 1].startswith("--"):
                flags[token[2:]] = argv[index + 1]
                index += 1
            else:
                flags[token[2:]] = True
        index += 1
    return {"sub": sub, "flags": flags}


def path_opts_from(args: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {"env": env}
    for flag, key in (
        ("data-root", "data_root"),
        ("socket", "socket_path"),
        ("lock", "lock_path"),
        ("log", "log_path"),
    ):
        value = args["flags"].get(flag)
        if isinstance(value, str) and value:
            result[key] = value
    idle = args["flags"].get("idle-ms")
    if isinstance(idle, str) and re.fullmatch(r"[0-9]+", idle):
        result["idle_ms"] = int(idle)
    return result


async def daemon_command(argv: list[str], io: Any) -> int:
    import inspect
    from .client import daemon_status, ensure_daemon, stop_daemon

    args = parse_daemon_args(argv)
    env = io.env if getattr(io, "env", None) is not None else os.environ
    opts = path_opts_from(args, env)
    output_json = args["flags"].get("json") is True
    try:
        if args["sub"] == "serve":
            from .executor import create_executor

            executor: Any = None

            def factory(runtime: DaemonRuntime) -> RpcHandlerMap:
                nonlocal executor
                executor = create_executor(runtime, {"env": env})
                return executor.handlers

            try:
                daemon = await start_daemon(
                    **opts,
                    log=lambda line: io.out(f"{utc_now()} {line}\n"),
                    handlers=factory,
                    is_busy=lambda: bool(executor and executor.is_busy()),
                )
            except DaemonError as error:
                if error.code == "ALREADY_RUNNING":
                    io.err(f"engined: {error}\n")
                    return 75
                raise
            loop = asyncio.get_running_loop()
            installed = []
            try:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(
                        sig, lambda: asyncio.create_task(daemon.close("signal"))
                    )
                    installed.append(sig)
                executor.pick_up()
                reason = await daemon.closed
                return 130 if reason == "signal" else 0
            finally:
                for sig in installed:
                    loop.remove_signal_handler(sig)
                stopped = executor.stop()
                if inspect.isawaitable(stopped):
                    await stopped
                await daemon.close()
        if args["sub"] == "start":
            client = await ensure_daemon(**opts)
            hello = client.hello
            client.close()
            value = {"alive": True, **hello, "socket": client.socket_path}
            io.out(
                json.dumps(value, separators=(",", ":")) + "\n"
                if output_json
                else f"engined running: pid {hello['pid']}, v{hello['version']}, {client.socket_path}\n"
            )
            return 0
        if args["sub"] == "stop":
            result = await stop_daemon(**opts, now=args["flags"].get("now") is True)
            text = "engined not running\n"
            if result["was_running"]:
                text = (
                    "engined stopped\n"
                    if result["stopped"]
                    else f"engined shutdown requested, {result['active_runs']} active run(s) keep it alive\n"
                )
            io.out(json.dumps(result, separators=(",", ":")) + "\n" if output_json else text)
            return 0 if result["stopped"] else 1
        if args["sub"] == "status":
            status = await daemon_status(**opts)
            text = f"engined not running ({status['socketPath']})\n"
            if status["alive"]:
                mismatch = " (version mismatch)" if status.get("version_mismatch") else ""
                text = f"engined running: pid {status.get('pid')}, v{status.get('version')}{mismatch}, {status['socketPath']}\n"
            io.out(json.dumps(status, separators=(",", ":")) + "\n" if output_json else text)
            return 0 if status["alive"] else 1
        if args["sub"] in ("help", "--help", "-h"):
            io.out(DAEMON_USAGE)
            return 0
        io.err(f"wise-engine daemon: unknown subcommand '{args['sub']}'\n\n{DAEMON_USAGE}")
        return 64
    except Exception as error:
        io.err(f"wise-engine daemon: {error}\n")
        return 70
