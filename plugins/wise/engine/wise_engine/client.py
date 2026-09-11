from __future__ import annotations

import asyncio
import errno
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .daemon import DaemonPaths, daemon_paths, read_lock, rotate_log
from .paths import ENGINE_ROOT
from .rpc import RpcClient, RpcError, domain_code
from .scheduler import UNDEFINED
from .version import source_build_id


class ConnectError(Exception):
    def __init__(self, code: str, message: str, cause_code: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.cause_code = cause_code


def _paths(options: dict[str, Any]) -> DaemonPaths:
    return daemon_paths(
        **{
            key: options[key]
            for key in ("env", "home", "data_root", "socket_path", "lock_path", "log_path")
            if key in options
        }
    )


async def raw_connect(
    socket_path: str, timeout_ms: float
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    try:
        return await asyncio.wait_for(asyncio.open_unix_connection(socket_path), timeout_ms / 1000)
    except TimeoutError:
        raise ConnectError("CONNECT_TIMEOUT", f"connect to {socket_path} timed out") from None
    except OSError as error:
        raise ConnectError(
            "DAEMON_UNAVAILABLE", f"{socket_path}: {error}", errno.errorcode.get(error.errno or 0)
        ) from error


async def socket_alive(socket_path: str, timeout_ms: float) -> bool:
    try:
        _, writer = await raw_connect(socket_path, timeout_ms)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


@dataclass
class Client:
    rpc: RpcClient
    hello: dict[str, Any]
    socket_path: str

    async def call(
        self, method: str, params: Any = UNDEFINED, *, timeout_ms: float | None = None
    ) -> Any:
        return await self.rpc.call(method, params, timeout_ms=timeout_ms)

    def close(self) -> None:
        self.rpc.close()

    def on_notification(self, listener: Any) -> Any:
        return self.rpc.on_notification(listener)


async def connect(**options: Any) -> Client:
    paths = _paths(options)
    timeout = options.get("connect_timeout_ms", 2000)
    reader, writer = await raw_connect(paths.socket_path, timeout)
    rpc = RpcClient(reader, writer, timeout_ms=options.get("timeout_ms", 30000))
    try:
        hello = await rpc.call(
            "hello",
            {
                "version": options["version"] if "version" in options else source_build_id(),
                "client": options.get("client", "wise-engine"),
            },
            timeout_ms=timeout,
        )
        return Client(rpc, hello, paths.socket_path)
    except BaseException:
        rpc.close()
        await asyncio.shield(rpc.task)
        raise


_SPAWNED: set[subprocess.Popen[bytes]] = set()


def spawn_daemon(**options: Any) -> dict[str, Any]:
    paths = _paths(options)
    Path(paths.data_root).mkdir(parents=True, exist_ok=True, mode=0o700)
    rotate_log(paths.log_path)
    fd = os.open(paths.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    entry = options.get("entry")
    if entry is None:
        entry = [
            "-c",
            f"import sys,runpy;sys.path.insert(0,{str(ENGINE_ROOT)!r});runpy.run_module('wise_engine',run_name='__main__')",
            "daemon",
            "serve",
        ]
    args = [
        sys.executable,
        "-u",
        *entry,
        "--data-root",
        paths.data_root,
        "--socket",
        paths.socket_path,
        "--lock",
        paths.lock_path,
        "--log",
        paths.log_path,
    ]
    if "idle_ms" in options:
        args += ["--idle-ms", str(options["idle_ms"])]
    try:
        child = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=fd,
            stderr=fd,
            start_new_session=True,
            env=dict(options.get("env", os.environ)),
        )
        _SPAWNED.add(child)

        def reap() -> None:
            if child.poll() is None:
                asyncio.get_running_loop().call_later(1, reap)
            else:
                _SPAWNED.discard(child)

        try:
            asyncio.get_running_loop().call_later(1, reap)
        except RuntimeError:
            pass
        for process in tuple(_SPAWNED):
            if process.poll() is not None:
                _SPAWNED.discard(process)
        return {"pid": child.pid}
    except OSError:
        return {}
    finally:
        os.close(fd)


async def ensure_daemon(**options: Any) -> Client:
    try:
        return await connect(**options)
    except RpcError as error:
        if domain_code(error) != "DAEMON_VERSION_MISMATCH":
            raise
        result = await stop_daemon(**options)
        if not result["stopped"]:
            raise
    except ConnectError:
        pass
    paths = _paths(options)
    spawn_daemon(**options)
    timeout = options.get("start_timeout_ms", 5000)
    deadline = asyncio.get_running_loop().time() + timeout / 1000
    last_error = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            return await connect(**options)
        except ConnectError as error:
            last_error = error
        await asyncio.sleep(0.1)
    detail = f" ({last_error}); see {paths.log_path}" if last_error else ""
    raise ConnectError(
        "START_TIMEOUT",
        f"wise-engined did not answer on {paths.socket_path} within {timeout} ms{detail}",
    )


async def daemon_status(**options: Any) -> dict[str, Any]:
    paths = _paths(options)
    status: dict[str, Any] = {
        "alive": False,
        "socketPath": paths.socket_path,
        "lockPid": read_lock(paths.lock_path),
    }
    try:
        client = await connect(**options)
        client.close()
        status.update(
            alive=True,
            pid=client.hello["pid"],
            version=client.hello["version"],
            started_at=client.hello["started_at"],
        )
    except RpcError as error:
        if domain_code(error) == "DAEMON_VERSION_MISMATCH":
            status.update(alive=True, version_mismatch=True)
            data = error.data
            if isinstance(data.get("daemon_version"), str):
                status["version"] = data["daemon_version"]
            if isinstance(data.get("pid"), (int, float)):
                status["pid"] = data["pid"]
    except Exception:
        pass
    return status


async def stop_daemon(**options: Any) -> dict[str, Any]:
    paths = _paths(options)
    timeout = options.get("connect_timeout_ms", 2000)
    try:
        reader, writer = await raw_connect(paths.socket_path, timeout)
    except Exception:
        return {"stopped": True, "was_running": False, "active_runs": 0}
    rpc = RpcClient(reader, writer, timeout_ms=timeout)
    try:
        result = await rpc.call("shutdown", {"when": "now" if options.get("now") else "idle"})
        active_runs = result.get("active_runs", 0)
    finally:
        rpc.close()
    deadline = asyncio.get_running_loop().time() + options.get("stop_timeout_ms", 5000) / 1000
    while asyncio.get_running_loop().time() < deadline:
        if not await socket_alive(paths.socket_path, timeout):
            return {"stopped": True, "was_running": True, "active_runs": active_runs}
        await asyncio.sleep(0.05)
    return {"stopped": False, "was_running": True, "active_runs": active_runs}
