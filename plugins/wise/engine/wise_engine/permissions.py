from __future__ import annotations

import errno
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .scheduler import JS_WHITESPACE

_MODE_RANK = {"approval-required": 0, "auto": 1, "full-access": 2}
READ_ONLY_BUILTINS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "LS",
        "WebFetch",
        "WebSearch",
        "ToolSearch",
        "TodoRead",
        "ListMcpResourcesTool",
        "ReadMcpResourceTool",
        "ReadMcpResourceDirTool",
    }
)
MUTATING_VERBS = frozenset(
    {
        "create",
        "update",
        "delete",
        "remove",
        "send",
        "post",
        "write",
        "edit",
        "set",
        "add",
        "move",
        "archive",
        "reply",
        "forward",
        "trash",
        "untrash",
        "label",
        "unlabel",
        "mark",
        "unmark",
        "merge",
        "push",
        "comment",
        "assign",
        "transition",
        "resolve",
        "close",
        "reopen",
        "schedule",
        "invite",
        "upload",
        "complete",
        "escalate",
        "acknowledge",
        "apply",
        "change",
        "toggle",
        "link",
        "unlink",
        "import",
        "respond",
        "execute",
        "run",
        "autofill",
        "enter",
        "release",
        "request",
        "cancel",
        "nudge",
        "answer",
        "resume",
        "start",
        "stop",
        "kill",
        "submit",
        "publish",
        "deploy",
        "install",
        "rename",
        "clear",
        "reset",
        "sign",
        "fill",
        "pay",
        "transfer",
        "trigger",
        "put",
        "patch",
    }
)
READ_VERBS = frozenset(
    {
        "get",
        "list",
        "read",
        "search",
        "fetch",
        "view",
        "find",
        "query",
        "describe",
        "show",
        "lookup",
        "browse",
        "preview",
        "check",
        "count",
        "explain",
        "summarize",
        "summarise",
        "inspect",
        "peek",
        "load",
        "retrieve",
        "poll",
        "watch",
        "status",
        "health",
        "is",
        "has",
        "who",
        "what",
    }
)
_DENY_HINT = (
    "not granted to this step by wise; a read-shaped tool would be allowed, add the rule to the "
    "step's `allowed_tools` or select Bypass permissions for this provider"
)
_SHELL_CONTROL_RE = re.compile(r"[\n\r;&|`<>]|\$\(")
_SPACE_CHARS = re.escape(JS_WHITESPACE)
_SPACE_PATTERN = f"[{_SPACE_CHARS}]"
_OUTSIDE_WORKSPACE_PATH_RE = re.compile(
    r"(^|[\s'\"])(?:~/|/)|(^|[/\s'\"])\.\.(?=/|[\s'\"]|$)".replace(r"\s", _SPACE_CHARS)
)
_RG_EXEC_RE = re.compile(r"(^|\s)--pre(?:-glob)?(?:=|\s|$)".replace(r"\s", _SPACE_PATTERN))
_GIT_OUTPUT_OPTION_RE = re.compile(
    r"^git\s+[^\n\r\u2028\u2029]*\s--output(?:=|\s|$)".replace(r"\s", _SPACE_PATTERN)
)
_AUTO_MUTATING_PATH_FIELDS = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
_AUTO_BASH_PATTERNS = (
    re.compile(
        r"^git\s+(?:status|diff|show|log|rev-parse|merge-base|ls-files|ls-tree|cat-file)(?:\s|$)".replace(
            r"\s", _SPACE_PATTERN
        )
    ),
    re.compile(
        r"^git\s+branch(?:\s+(?:--show-current|--list|-l|-a|-r|-v|-vv))*\s*$".replace(
            r"\s", _SPACE_PATTERN
        )
    ),
    re.compile(
        r"^(?:pwd|ls|rg|grep|jq|cat|head|tail|wc|test)(?:\s|$)".replace(r"\s", _SPACE_PATTERN)
    ),
)


def effective_mode(step: str | None, floor: str) -> str:
    wanted = step if step is not None else floor
    return wanted if _MODE_RANK[wanted] >= _MODE_RANK[floor] else floor


def provider_permission(state: Mapping[str, Any], harness: str) -> str:
    provider = state.get("provider_permissions") or {}
    if harness in provider:
        return provider[harness]
    legacy = state.get("permissions")
    if legacy == "full":
        return "full-access"
    return "approval-required" if legacy == "allowlist" else "auto"


def name_tokens(tool_part: str) -> list[str]:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", tool_part)
    name = re.sub(f"[-.{_SPACE_CHARS}]+", "_", name).lower()
    return [token for token in name.split("_") if token]


def mcp_tool_part(tool_name: str) -> str | None:
    if not tool_name.startswith("mcp__"):
        return None
    return tool_name[5:].partition("__")[2]


def is_read_mcp_tool(tool_name: str) -> bool:
    part = mcp_tool_part(tool_name)
    if not part:
        return False
    tokens = name_tokens(part)
    if not tokens or tokens[0] in MUTATING_VERBS:
        return False
    if tokens[0] in READ_VERBS:
        return True
    return len(tokens) > 1 and tokens[1] not in MUTATING_VERBS and tokens[1] in READ_VERBS


def is_auto_bash_command(command: str) -> bool:
    value = command.strip(JS_WHITESPACE)
    if not value or any(
        pattern.search(value)
        for pattern in (
            _SHELL_CONTROL_RE,
            _OUTSIDE_WORKSPACE_PATH_RE,
            _RG_EXEC_RE,
            _GIT_OUTPUT_OPTION_RE,
        )
    ):
        return False
    return any(pattern.search(value) for pattern in _AUTO_BASH_PATTERNS)


def _string_field(value: Any, field: str) -> str:
    if not isinstance(value, dict):
        return ""
    item = value.get(field)
    return item if isinstance(item, str) else ""


def _nearest_real_path(path: str) -> Path | None:
    candidate = Path(path)
    while True:
        try:
            return candidate.resolve(strict=True)
        except ValueError:
            return None
        except (OSError, RuntimeError):
            try:
                candidate.lstat()
                return None
            except ValueError:
                return None
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ENOTDIR):
                    return None
                if candidate.parent == candidate:
                    return None
                candidate = candidate.parent


def _is_within_workspace(path: str, roots: Sequence[str | os.PathLike[str]]) -> bool:
    if not path or not roots:
        return False
    candidate = _nearest_real_path(os.path.abspath(os.path.join(roots[0], path)))
    if candidate is None:
        return False
    for root in roots:
        try:
            real_root = Path(root).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
        if candidate == real_root or real_root in candidate.parents:
            return True
    return False


def decide_permission(
    tool_name: str,
    input: Any,
    *,
    mode: str | None = None,
    workspace_roots: Sequence[str | os.PathLike[str]] = (),
) -> dict[str, Any]:
    mode = mode if mode is not None else "approval-required"
    if mode == "full-access" or tool_name in READ_ONLY_BUILTINS or is_read_mcp_tool(tool_name):
        return {"behavior": "allow", "updatedInput": input}
    if mode == "auto":
        if tool_name == "TodoWrite":
            return {"behavior": "allow", "updatedInput": input}
        path_field = _AUTO_MUTATING_PATH_FIELDS.get(tool_name)
        if path_field is not None:
            if _is_within_workspace(_string_field(input, path_field), workspace_roots):
                return {"behavior": "allow", "updatedInput": input}
            return {
                "behavior": "deny",
                "message": f"{tool_name} path blocked by wise auto mode; select Bypass permissions to edit outside the workspace",
            }
        if tool_name == "Bash":
            if is_auto_bash_command(_string_field(input, "command")):
                return {"behavior": "allow", "updatedInput": input}
            return {
                "behavior": "deny",
                "message": "Bash command blocked by wise auto mode; select Bypass permissions to run it",
            }
    return {"behavior": "deny", "message": f"{tool_name} {_DENY_HINT}"}
