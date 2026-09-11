from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from wise_engine.client import ConnectError, connect, daemon_status, ensure_daemon, stop_daemon
from wise_engine.daemon import (
    DaemonError,
    acquire_lock,
    child_path,
    clear_child,
    daemon_paths,
    find_run_dir,
    group_alive,
    kill_group,
    list_run_dirs,
    pid_alive,
    read_child,
    read_children,
    read_lock,
    record_child,
    recover_runs,
    release_lock,
    rotate_log,
    start_daemon,
)
from wise_engine.ledger import (
    append_event,
    init_state,
    new_ulid,
    read_events,
    read_state,
    start_run,
    start_step,
    update_run,
    write_state,
)
from wise_engine.rpc import RpcClient, RpcError, domain_code

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def root():
    with tempfile.TemporaryDirectory(prefix="wd-", dir="/tmp") as directory:
        yield directory


@pytest.fixture
async def daemon(root):
    instance = await start_daemon(
        data_root=root,
        env={},
        version="test",
        idle_ms=60000,
        wait={"poll_ms": 5, "progress_ms": 10},
    )
    yield instance
    await instance.close()


@pytest.fixture
async def client(daemon):
    instance = await connect(data_root=daemon.runtime.paths.data_root, env={}, version="test")
    yield instance
    instance.close()
    await instance.rpc.task


def make_run(root, slug="ws"):
    run_id = new_ulid()
    directory = str(Path(root) / "runs" / slug / run_id)
    init_state(
        run_dir=directory,
        run_id=run_id,
        workflow={"name": "wf", "version": 2, "dir": root},
        step_ids=["a", "b"],
        cwd=root,
    )
    start_run(directory, {})
    start_step(directory, "a")
    return directory, run_id


async def test_handshake_permissions_connections(daemon, client):
    assert client.hello["version"] == "test"
    assert client.hello["pid"] == os.getpid()
    assert "T" in client.hello["started_at"]
    assert await client.call("status", {}) == []
    assert stat.S_IMODE(Path(client.socket_path).stat().st_mode) == 0o600
    assert read_lock(daemon.runtime.paths.lock_path) == os.getpid()
    assert daemon.connections() == 1
    client.close()
    await asyncio.sleep(0.01)
    assert daemon.connections() == 0


async def test_mismatch_and_status(daemon):
    opts = dict(data_root=daemon.runtime.paths.data_root, env={}, version="wrong")
    with pytest.raises(RpcError) as error:
        await connect(**opts)
    assert domain_code(error.value) == "DAEMON_VERSION_MISMATCH"
    assert error.value.data["daemon_version"] == "test"
    status = await daemon_status(**opts)
    assert status["alive"] and status["version_mismatch"]
    assert status["version"] == "test"
    assert status["pid"] == os.getpid()


async def test_hello_guard_shutdown_exception(daemon):
    reader, writer = await asyncio.open_unix_connection(daemon.runtime.paths.socket_path)
    rpc = RpcClient(reader, writer)
    with pytest.raises(RpcError, match="hello required before status"):
        await rpc.call("status", {})
    assert (await rpc.call("shutdown", {}))["accepted"]
    await asyncio.wait_for(daemon.closed, 1)
    rpc.close()
    await rpc.task


async def test_single_instance_concurrent_starts(root):
    results = await asyncio.gather(
        *(start_daemon(data_root=root, env={}, version="test") for _ in range(2)),
        return_exceptions=True,
    )
    daemons = [result for result in results if not isinstance(result, Exception)]
    errors = [result for result in results if isinstance(result, Exception)]
    assert len(daemons) == len(errors) == 1
    assert isinstance(errors[0], DaemonError)
    assert errors[0].code == "ALREADY_RUNNING"
    await daemons[0].close()


async def test_stale_lock_and_idle_cleanup(root):
    lock = Path(root) / "engined.lock"
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lock.write_text(str(dead.pid))
    daemon = await start_daemon(data_root=root, env={}, idle_ms=35)
    assert await asyncio.wait_for(daemon.closed, 1) == "idle"
    assert not lock.exists()
    assert not Path(daemon.runtime.paths.socket_path).exists()
    await daemon.close()


async def test_idle_waits_for_active_run_and_busy(root):
    busy = True
    daemon = await start_daemon(data_root=root, env={}, idle_ms=30, is_busy=lambda: busy)
    try:
        await asyncio.sleep(0.08)
        assert not daemon.closed.done()
        directory, _ = make_run(root)
        busy = False
        await asyncio.sleep(0.08)
        assert not daemon.closed.done()
        update_run(directory, {"status": "paused"})
        assert await asyncio.wait_for(daemon.closed, 1) == "idle"
    finally:
        await daemon.close()


async def test_stubs_and_injected_factory(root):
    seen = []

    def factory(runtime):
        seen.append(runtime)
        return {"run": lambda p, ctx: {"run_id": "injected", "status": "running"}}

    daemon = await start_daemon(data_root=root, env={}, version="test", handlers=factory)
    client = await connect(data_root=root, env={}, version="test")
    try:
        assert seen == [daemon.runtime]
        assert (await client.call("run", {}))["run_id"] == "injected"
        for method in (
            "preflight",
            "answer",
            "report",
            "nudge",
            "child_report",
            "child_ask",
            "child_context",
            "child_checkpoint",
        ):
            with pytest.raises(RpcError) as error:
                await client.call(method, {})
            assert domain_code(error.value) == "NOT_IMPLEMENTED"
    finally:
        client.close()
        await daemon.close()


async def test_status_all_one_missing_and_lookup(daemon, client, root):
    first, first_id = make_run(root, "a")
    second, second_id = make_run(root, "b")
    state = read_state(first)
    state["last_activity_at"] = "2020-01-01T00:00:00Z"
    write_state(first, state)
    one = await client.call("status", {"run_id": second_id})
    assert one["workflow"] == "wf" and one["run_id"] == second_id
    assert [row["run_id"] for row in await client.call("status", {})] == [second_id, first_id]
    assert find_run_dir(daemon.runtime.paths.runs_root, "../escape") is None
    assert find_run_dir(daemon.runtime.paths.runs_root, first_id) == first
    assert list_run_dirs(daemon.runtime.paths.runs_root) == [first, second]
    with pytest.raises(RpcError) as error:
        await client.call("status", {"run_id": "missing"})
    assert domain_code(error.value) == "RUN_NOT_FOUND"


async def test_wait_timeout_events_gate_progress_disconnect(daemon, client, root):
    directory, run_id = make_run(root)
    after = 0
    progress = []
    off = client.on_notification(progress.append)
    snapshot = await client.call("wait", {"run_id": run_id, "after": after, "timeout_ms": 45})
    assert snapshot == {"events": [], "status": "running", "done": False}
    assert progress and progress[0]["method"] == "progress"
    assert progress[0]["params"]["run_id"] == run_id
    off()
    pending = asyncio.create_task(
        client.call("wait", {"run_id": run_id, "after": after, "timeout_ms": 1000})
    )
    await asyncio.sleep(0.01)
    event = append_event(directory, {"run_id": run_id, "type": "warn", "message": "hello"})
    assert (await pending)["events"] == [event]
    gate = {"id": "g", "step": "a", "question": "Continue?", "options": ["yes"]}
    update_run(directory, {"status": "gated", "gate": gate})
    assert (await client.call("wait", {"run_id": run_id}))["gate"] == gate
    update_run(directory, {"status": "paused"})
    assert (await client.call("wait", {"run_id": run_id, "after": 999}))["status"] == "paused"
    update_run(directory, {"status": "running"})
    pending = asyncio.create_task(
        client.call("wait", {"run_id": run_id, "after": 999, "timeout_ms": 600000})
    )
    await asyncio.sleep(0.01)
    client.close()
    with pytest.raises(RpcError):
        await pending
    await asyncio.wait_for(daemon.close(), 1)
    assert read_state(directory)["status"] == "running"


@pytest.mark.parametrize(
    "params",
    [
        None,
        [],
        {},
        {"run_id": 2},
        {"run_id": "x", "after": "no"},
        {"run_id": "x", "timeout_ms": True},
    ],
)
async def test_wait_bad_params(client, params):
    with pytest.raises(RpcError) as error:
        await client.call("wait", params)
    assert error.value.code == -32602


async def test_cancel_and_resume(daemon, client, root):
    directory, run_id = make_run(root)
    record_child(directory, {"pgid": 0, "pid": 0})
    update_run(directory, {"gate": {"id": "g"}})
    assert await client.call("cancel", {"run_id": run_id, "reason": "stop"}) == {
        "status": "cancelled"
    }
    state = read_state(directory)
    assert state["status"] == state["steps"]["a"]["status"] == "cancelled"
    assert "gate" not in state and "completed_at" in state
    assert not read_children(directory)
    assert read_events(directory)[-1]["message"] == "stop"
    count = len(read_events(directory))
    await client.call("cancel", {"run_id": run_id})
    assert len(read_events(directory)) == count
    with pytest.raises(RpcError):
        await client.call("resume", {"run_id": run_id})
    update_run(directory, {"status": "paused"})
    assert await client.call("resume", {"run_id": run_id}) == {
        "run_id": run_id,
        "status": "running",
    }
    assert await client.call("resume", {"run_id": run_id}) == {
        "run_id": run_id,
        "status": "running",
    }
    for status in ("gated", "completed"):
        update_run(directory, {"status": status})
        with pytest.raises(RpcError):
            await client.call("resume", {"run_id": run_id})
    with pytest.raises(RpcError):
        await client.call("cancel", {"run_id": run_id})
    update_run(directory, {"status": "failed"})
    assert (await client.call("resume", {"run_id": run_id}))["status"] == "running"


async def test_recovery_pauses_running_steps_and_kills_all_groups(root):
    directory, run_id = make_run(root)
    children = [
        subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True
        )
        for _ in range(2)
    ]
    try:
        for child in children:
            record_child(directory, {"pid": child.pid, "pgid": child.pid})
        report = recover_runs(str(Path(root) / "runs"))
        assert report == {"paused": [run_id], "killed": [child.pid for child in children]}
        for child in children:
            assert await asyncio.to_thread(child.wait, 2) < 0
        state = read_state(directory)
        assert state["status"] == "paused"
        assert state["steps"]["a"]["status"] == "pending"
        assert read_child(directory) is None
        assert read_events(directory)[-1]["message"] == "daemon restarted, run paused"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


async def test_child_sidecar(root):
    first = record_child(root, {"pid": 1, "pgid": 2, "started_at": "then"})
    second = record_child(root, {"pid": 3, "pgid": 4})
    assert read_child(root) == second
    assert read_children(root) == [first, second]
    record_child(root, {"pid": 1, "pgid": 5})
    assert [c["pid"] for c in read_children(root)] == [3, 1]
    clear_child(root, 3)
    assert len(read_children(root)) == 1
    clear_child(root)
    assert read_child(root) is None
    Path(child_path(root)).write_text(json.dumps(first))
    assert read_children(root) == [first]
    Path(child_path(root)).write_text(
        '{"children":[null,{}, {"pid":true,"pgid":2}, {"pid":2,"pgid":3}]}'
    )
    assert read_children(root) == [{"pid": 2, "pgid": 3, "started_at": ""}]
    clear_child(root, 2)
    assert not Path(child_path(root)).exists()


async def test_stop_idle_and_active_runs(daemon, root):
    directory, _ = make_run(root)
    opts = dict(data_root=root, env={}, stop_timeout_ms=50)
    assert await stop_daemon(**opts) == {"stopped": False, "was_running": True, "active_runs": 1}
    update_run(directory, {"status": "completed"})
    assert await stop_daemon(**opts) == {"stopped": True, "was_running": True, "active_runs": 0}
    assert await stop_daemon(**opts) == {"stopped": True, "was_running": False, "active_runs": 0}


async def test_path_helpers_locks_and_rotation(root):
    paths = daemon_paths(env={"XDG_DATA_HOME": root, "XDG_RUNTIME_DIR": root + "/runtime"})
    assert paths.data_root == root + "/wise"
    assert paths.socket_path == root + "/runtime/wise/engined.sock"
    assert daemon_paths(env={}, home=root).data_root == root + "/.local/share/wise"
    lock = root + "/lock"
    Path(lock).write_text("garbage")
    acquire_lock(lock)
    assert read_lock(lock) == os.getpid()
    release_lock(lock)
    Path(lock).write_text("123garbage")
    assert read_lock(lock) == 123
    release_lock(lock)
    assert Path(lock).exists()
    log = root + "/log"
    Path(log).write_text("12345")
    assert not rotate_log(log, 5)
    assert rotate_log(log, 4)
    assert Path(log + ".1").read_text() == "12345"
    assert not rotate_log(log)
    for value in (0, -1, True, 1.2):
        assert not pid_alive(value)
        assert not group_alive(value)
        assert not kill_group(value)


async def test_failed_listen_releases_lock(root):
    with pytest.raises(OSError):
        await start_daemon(data_root=root, env={}, socket_path=root + "/" + "x" * 200)
    assert read_lock(root + "/engined.lock") is None


async def test_unavailable_and_start_timeout(root):
    opts = dict(data_root=root, env={})
    with pytest.raises(ConnectError) as error:
        await connect(**opts)
    assert error.value.code == "DAEMON_UNAVAILABLE"
    assert not (await daemon_status(**opts))["alive"]
    with pytest.raises(ConnectError) as error:
        await ensure_daemon(**opts, entry=["-c", "pass"], start_timeout_ms=100)
    assert error.value.code == "START_TIMEOUT"
    assert root + "/engined.log" in str(error.value)


async def test_real_detached_autostart_and_version_replacement(root):
    from wise_engine.paths import ENGINE_ROOT

    entry = Path(root) / "daemon_fixture.py"
    entry.write_text(
        "import sys,asyncio,signal\n"
        f"sys.path.insert(0,{str(ENGINE_ROOT)!r})\n"
        "from wise_engine.daemon import start_daemon,parse_daemon_args,path_opts_from\n"
        "async def main():\n"
        " options=path_opts_from(parse_daemon_args(['serve',*sys.argv[2:]]),{})\n"
        " daemon=await start_daemon(**options,version=sys.argv[1])\n"
        " loop=asyncio.get_running_loop()\n"
        " loop.add_signal_handler(signal.SIGTERM,lambda:asyncio.create_task(daemon.close('signal')))\n"
        " await daemon.closed\n"
        "asyncio.run(main())\n"
    )
    options = {
        "data_root": root,
        "env": {},
        "entry": [str(entry), "one"],
        "version": "one",
        "idle_ms": 5000,
    }
    client = None
    second = None
    try:
        client = await ensure_daemon(**options)
        assert client.hello["pid"] != os.getpid()
        assert client.hello["version"] == "one"
        assert await client.call("status", {}) == []
        assert stat.S_IMODE(Path(root, "engined.log").stat().st_mode) == 0o600
        client.close()
        second = await ensure_daemon(**{**options, "entry": [str(entry), "two"], "version": "two"})
        assert second.hello["version"] == "two"
        assert second.hello["pid"] != client.hello["pid"]
    finally:
        if client:
            client.close()
        if second:
            second.close()
        assert (await stop_daemon(data_root=root, env={}, now=True))["stopped"]


async def test_version_mismatch_active_run_does_not_replace(daemon, root):
    make_run(root)
    with pytest.raises(RpcError) as error:
        await ensure_daemon(data_root=root, env={}, version="wrong", stop_timeout_ms=30)
    assert domain_code(error.value) == "DAEMON_VERSION_MISMATCH"
    assert not daemon.closed.done()


async def test_daemon_command_status_help_unknown(daemon, root):
    from types import SimpleNamespace
    from wise_engine.daemon import daemon_command

    output = []
    errors = []
    io = SimpleNamespace(out=output.append, err=errors.append, env={})
    assert await daemon_command(["status", "--data-root", root, "--json"], io) == 0
    assert json.loads(output[-1])["alive"]
    assert await daemon_command(["help"], io) == 0
    assert "daemon <serve|start|stop|status>" in output[-1]
    assert await daemon_command(["wat"], io) == 64
    assert "unknown subcommand 'wat'" in errors[-1]
    await daemon.close()
    assert await daemon_command(["status", "--data-root", root], io) == 1
    assert "not running" in output[-1]


async def test_connected_client_keeps_idle_daemon_alive(root):
    daemon = await start_daemon(data_root=root, env={}, version="test", idle_ms=30)
    client = await connect(data_root=root, env={}, version="test")
    try:
        await asyncio.sleep(0.08)
        assert not daemon.closed.done()
        client.close()
        assert await asyncio.wait_for(daemon.closed, 1) == "idle"
    finally:
        client.close()
        await daemon.close()


async def test_stale_socket_file_replaced(root):
    socket = Path(root) / "engined.sock"
    socket.write_text("not a socket")
    daemon = await start_daemon(data_root=root, env={}, version="test")
    client = await connect(data_root=root, env={}, version="test")
    try:
        assert stat.S_ISSOCK(socket.stat().st_mode)
    finally:
        client.close()
        await daemon.close()
