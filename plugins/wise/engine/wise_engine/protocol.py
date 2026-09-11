from __future__ import annotations

RPC_PARSE_ERROR = -32700
RPC_INVALID_REQUEST = -32600
RPC_METHOD_NOT_FOUND = -32601
RPC_INVALID_PARAMS = -32602
RPC_INTERNAL_ERROR = -32603
RPC_DOMAIN_ERROR = -32000
RPC_CLIENT_TIMEOUT = -32800
RPC_CLIENT_DISCONNECTED = -32801
METHOD_NAMES = (
    "hello",
    "shutdown",
    "preflight",
    "run",
    "wait",
    "answer",
    "status",
    "cancel",
    "resume",
    "report",
    "nudge",
    "child_report",
    "child_ask",
    "child_context",
    "child_checkpoint",
)
WAIT_DEFAULT_MS = 110_000
WAIT_MAX_MS = 600_000
WAIT_PROGRESS_MS = 30_000
WAIT_POLL_MS = 250
