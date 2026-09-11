from __future__ import annotations

from ..adapter_types import Adapter, AgentHandle, EventCallback, Json
from .claude import claude_adapter, start_claude
from .codex import codex_adapter, start_codex
from .cursor import cursor_adapter, start_cursor
from .gemini import gemini_adapter, start_gemini
from .grok import grok_adapter, start_grok

ADAPTERS: dict[str, Adapter] = {
    adapter.id: adapter
    for adapter in (claude_adapter, codex_adapter, cursor_adapter, gemini_adapter, grok_adapter)
}
STARTERS = dict(
    claude=start_claude,
    codex=start_codex,
    cursor=start_cursor,
    gemini=start_gemini,
    grok=start_grok,
)


class AdapterError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def has_adapter(harness: str) -> bool:
    return harness in ADAPTERS


def adapter_for(harness: str) -> Adapter:
    try:
        return ADAPTERS[harness]
    except KeyError:
        raise AdapterError(
            "HARNESS_UNAVAILABLE", f'no adapter for harness "{harness}" yet'
        ) from None


async def default_starter(harness: str, req: Json, on_event: EventCallback) -> AgentHandle:
    adapter_for(harness)
    return await STARTERS[harness](req, on_event)
