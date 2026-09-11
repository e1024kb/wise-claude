from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence, Set
from pathlib import Path

from .adapter_types import Adapter, Json
from .constants import HARNESSES
from .rpc import RpcError, domain_error

LOGIN_CMDS = {
    "claude": "claude auth login",
    "codex": "codex login",
    "cursor": "cursor-agent login",
    "gemini": "gemini (interactive, then /auth)",
    "grok": "grok login",
}
AdapterLookup = Callable[[str], Adapter | None]


def collect_needs(definition: Json, enabled: Set[str], resolved: Json) -> list[Json]:
    seen: set[tuple[str, str]] = set()
    out = []

    def add(harness: str, auth: str) -> None:
        key = harness, auth
        if key not in seen:
            seen.add(key)
            out.append(dict(harness=harness, auth=auth))

    for step in definition["steps"]:
        if step["id"] not in enabled:
            continue
        if step["type"] == "agent":
            add(
                resolved.get(step["id"], {}).get("harness", step.get("harness", "claude")),
                step.get("auth", "subscription"),
            )
        elif step["type"] == "units":
            phases = [key for key in resolved if key.startswith(step["id"] + ".")]
            if not phases:
                add(step.get("harness", "claude"), step.get("auth", "subscription"))
            for key in phases:
                add(resolved[key].get("harness", "claude"), step.get("auth", "subscription"))
    return out


def bin_on_path(bin: str, env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    candidates = (
        [Path(bin)]
        if os.path.isabs(bin)
        else [Path(directory or ".") / bin for directory in env.get("PATH", "").split(os.pathsep)]
    )
    return any(path.is_file() and os.access(path, os.X_OK) for path in candidates)


def installed_harnesses(
    definition: Json, lookup: AdapterLookup, env: Mapping[str, str] | None = None
) -> list[str]:
    groups = [
        group for group in definition.get("tuning", {}).get("groups", []) if not group.get("locked")
    ]
    result = []
    for harness in HARNESSES:
        if all(group["default"].get("harness", "claude") == harness for group in groups):
            continue
        adapter = lookup(harness)
        if adapter is None:
            continue
        binary = getattr(adapter, "bin", None)
        if binary is not None and not bin_on_path(binary, env):
            continue
        result.append(harness)
    return result


def auth_required(harness: str, login_cmd: str, detail: str | None = None) -> RpcError:
    return domain_error(
        "AUTH_REQUIRED",
        f"{harness}: not logged in{f' ({detail})' if detail else ''}; run `{login_cmd}`",
        dict(harness=harness, login_cmd=login_cmd),
    )


async def probe_one(harness: str, auth: str, lookup: AdapterLookup) -> Json:
    adapter = lookup(harness)
    if adapter is None:
        return dict(ok=False, login_cmd=LOGIN_CMDS[harness], detail="no adapter in this build")
    try:
        probe = await adapter.probe_auth(auth)
        return dict(ok=probe["ok"], login_cmd=probe.get("login_cmd", LOGIN_CMDS[harness]))
    except Exception as error:
        return dict(ok=False, login_cmd=LOGIN_CMDS[harness], detail=str(error))


async def logged_out_harnesses(harnesses: Sequence[str], lookup: AdapterLookup) -> list[str]:
    result = []
    for harness in harnesses:
        if not (await probe_one(harness, "subscription", lookup))["ok"]:
            result.append(harness)
    return result


async def probe_harnesses(needs: Sequence[Json], lookup: AdapterLookup) -> None:
    for need in needs:
        harness = need["harness"]
        adapter = lookup(harness)
        if adapter is None:
            raise auth_required(harness, LOGIN_CMDS[harness], "no adapter in this build")
        try:
            probe = await adapter.probe_auth(need["auth"])
        except Exception as error:
            raise auth_required(harness, LOGIN_CMDS[harness], str(error)) from error
        if not probe["ok"]:
            raise auth_required(harness, probe.get("login_cmd", LOGIN_CMDS[harness]), need["auth"])
