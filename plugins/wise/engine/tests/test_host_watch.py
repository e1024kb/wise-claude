import asyncio

import pytest

from wise_engine.host_watch import HostWatch, exit_after_close

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_stdin_end_fires_once():
    reasons = []
    watch = HostWatch(on_gone=reasons.append, ppid=lambda: 42, interval_ms=5)
    watch.stdin_closed()
    watch.stdin_closed()
    watch.stdout_error()
    assert reasons == ["stdin closed"]
    await asyncio.sleep(0.01)
    assert watch.task.done()


async def test_stdout_error():
    reasons = []
    watch = HostWatch(on_gone=reasons.append, ppid=lambda: 42)
    watch.stdout_error()
    assert reasons == ["stdout error"]
    watch.stop()


async def test_parent_poll():
    reasons = []
    parent = 42
    watch = HostWatch(on_gone=reasons.append, ppid=lambda: parent, interval_ms=5)
    await asyncio.sleep(0.02)
    assert not reasons
    parent = 1
    await asyncio.sleep(0.03)
    assert reasons == ["host exited"]
    watch.stop()


async def test_stop_prevents_notification():
    reasons = []
    watch = HostWatch(on_gone=reasons.append, ppid=lambda: 1)
    watch.stop()
    watch.stdin_closed()
    await asyncio.sleep(0.01)
    assert not reasons


async def test_exit_after_close():
    codes = []
    exit_after_close(3, codes.append)
    assert not codes
    await asyncio.sleep(0.08)
    assert codes == [3]
