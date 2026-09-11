from __future__ import annotations

import asyncio
import os
from collections.abc import Callable


class HostWatch:
    def __init__(
        self,
        *,
        on_gone: Callable[[str], None],
        ppid: Callable[[], int] = os.getppid,
        interval_ms: float = 5000,
    ) -> None:
        self._on_gone = on_gone
        self._ppid = ppid
        self._interval = interval_ms / 1000
        self._stopped = False
        self.task = asyncio.create_task(self._poll())

    async def _poll(self) -> None:
        while not self._stopped:
            if self._ppid() == 1:
                self._gone("host exited")
                return
            await asyncio.sleep(self._interval)

    def _gone(self, reason: str) -> None:
        if self._stopped:
            return
        self.stop()
        self._on_gone(reason)

    def stdin_closed(self) -> None:
        self._gone("stdin closed")

    def stdout_error(self) -> None:
        self._gone("stdout error")

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.task.cancel()


def watch_host(
    *,
    on_gone: Callable[[str], None],
    ppid: Callable[[], int] = os.getppid,
    interval_ms: float = 5000,
) -> HostWatch:
    return HostWatch(on_gone=on_gone, ppid=ppid, interval_ms=interval_ms)


def exit_after_close(code: int, exit: Callable[[int], object] = os._exit) -> asyncio.TimerHandle:
    return asyncio.get_running_loop().call_later(0.05, exit, code)
