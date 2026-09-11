from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

Json = dict[str, Any]
EventCallback = Callable[[Json], None]


@dataclass
class AgentHandle:
    done: Awaitable[Json]
    pid: int | None = None
    kill: Callable[[str], Any] | None = None
    nudge: Callable[[str], Any] | None = None
    snapshot: Callable[[], Any] | None = None


class Adapter(Protocol):
    id: str
    bin: str | None

    async def probe_auth(self, auth: str) -> Json: ...

    async def run(self, req: Json, on_event: EventCallback) -> Json: ...

    def effort_map(self, effort: str) -> str | None: ...


AgentStarter = Callable[[str, Json, EventCallback], Awaitable[AgentHandle]]
