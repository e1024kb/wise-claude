from __future__ import annotations

import asyncio
import codecs
import json
import os
import re
from pathlib import Path
from typing import Any

from ..spawn import SpawnOptions, spawn_clean
from ..yaml_compat import js_string

DEFAULT_CMD_TIMEOUT_MS = 120_000
NETWORK_CMD_TIMEOUT_MS = 300_000
STDOUT_CAP = 1024 * 1024
Json = dict[str, Any]


async def spawn_runner(cmd: str, args: list[str], opts: Json) -> Json:
    proc = await spawn_clean(
        cmd,
        args,
        SpawnOptions(
            cwd=opts["cwd"],
            env=opts["env"],
            timeout_ms=opts.get("timeout_ms", DEFAULT_CMD_TIMEOUT_MS),
        ),
    )
    proc.stdin.end()

    async def capture() -> str:
        out = ""
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while chunk := await proc.stdout.read(65536):
            text = decoder.decode(chunk)
            if len(out) < STDOUT_CAP:
                out += text[: STDOUT_CAP - len(out)]
        return out + decoder.decode(b"", final=True)[: max(0, STDOUT_CAP - len(out))]

    reader = asyncio.create_task(capture())

    async def abort() -> None:
        await opts["signal"].wait()
        proc.kill("SIGTERM")

    watcher = asyncio.create_task(abort()) if opts.get("signal") is not None else None
    try:
        result = await asyncio.shield(proc.exited)
        out = {
            "code": result.code,
            "stdout": await reader,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
        }
        if result.error is not None:
            out["error"] = result.error
        return out
    except BaseException:
        proc.kill("SIGTERM")
        await asyncio.shield(proc.exited)
        await reader
        raise
    finally:
        if watcher:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


def ok(result: Json) -> bool:
    return result["code"] == 0 and not result.get("timed_out") and "error" not in result


def err_text(result: Json, max_chars: int = 300) -> str:
    if "error" in result:
        return f"spawn failed: {result['error']}"
    if result.get("timed_out"):
        return "timed out"
    text = re.sub(r"\s+", " ", result["stderr"].strip() or result["stdout"].strip())
    return (
        text[: max_chars - 1] + "…"
        if len(text) > max_chars
        else text or f"exit code {js_string(result['code'])}"
    )


def fail(
    reason: str, verdict: str | None = None, patch: Json | None = None, extra: Json | None = None
) -> Json:
    return {
        "ok": False,
        "reason": reason,
        **({"verdict": verdict} if verdict is not None else {}),
        **({"patch": patch} if patch is not None else {}),
        **(extra or {}),
    }


def pass_(patch: Json | None = None, extra: Json | None = None) -> Json:
    return {"ok": True, **({"patch": patch} if patch is not None else {}), **(extra or {})}


async def run(ctx: Json, cmd: str, args: list[str], opts: Json | None = None) -> Json:
    options = {"cwd": ctx["cwd"], "env": ctx["env"], **(opts or {})}
    if ctx.get("signal") is not None:
        options["signal"] = ctx["signal"]
    result = await ctx["exec"](cmd, args, options)
    ctx["log"](f"$ {cmd} {' '.join(args)} -> {'ok' if ok(result) else err_text(result, 120)}")
    return result


async def git(ctx: Json, args: list[str], opts: Json | None = None) -> Json:
    return await run(ctx, "git", args, opts)


async def gh(ctx: Json, args: list[str], opts: Json | None = None) -> Json:
    return await run(ctx, "gh", args, opts)


def json_of(result: Json) -> Any:
    if ok(result):
        try:
            return json.loads(result["stdout"])
        except ValueError:
            pass
    return None


def is_protected_branch(name: str) -> bool:
    return name in ("main", "master") or name.startswith("release")


async def local_branch_exists(ctx: Json, branch: str) -> bool:
    return ok(await git(ctx, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]))


async def remote_branch_exists(ctx: Json, branch: str) -> bool | None:
    result = await git(
        ctx, ["ls-remote", "--heads", "origin", branch], {"timeout_ms": NETWORK_CMD_TIMEOUT_MS}
    )
    return bool(result["stdout"].strip()) if ok(result) else None


async def resolve_base(ctx: Json) -> str:
    if ctx["config"].get("base"):
        return ctx["config"]["base"]
    parsed = json_of(await gh(ctx, ["repo", "view", "--json", "defaultBranchRef"]))
    name = (
        parsed.get("defaultBranchRef", {}).get("name")
        if isinstance(parsed, dict) and isinstance(parsed.get("defaultBranchRef"), dict)
        else None
    )
    if isinstance(name, str) and name:
        return name
    result = await git(ctx, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    short = result["stdout"].strip().removeprefix("origin/")
    return short if ok(result) and short else "main"


def sanitize_ref(value: str) -> str:
    value = re.sub(r"\.{2,}", "-", re.sub(r"[^A-Za-z0-9._-]+", "-", value)).strip("-.")
    return value.removesuffix(".lock")


def ticket_ref(item: str) -> str:
    value = item.strip()
    if re.match(r"[a-z]+://", value, re.I) or "/" in value:
        parts = [part for part in re.split(r"[/?#]", value) if part]
        value = next(
            (
                part
                for part in reversed(parts)
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*-[0-9]+|[0-9]+", part)
            ),
            parts[-1] if parts else value,
        )
    return value.removeprefix("#").strip()


def ticket_branch(ref: str) -> str:
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*-[0-9]+", ref):
        return ref
    if re.fullmatch(r"[0-9]+", ref):
        return "abstract-task-" + ref
    return sanitize_ref(ref) or "abstract-task-0"


def ticket_context(tickets: list[Json], unit: Json) -> Json | None:
    value = unit.get("ticket_ref", unit["ref"])
    native_ref = value if isinstance(value, str) else str(unit["ref"])
    exact = next(
        (
            ticket
            for ticket in tickets
            if ticket.get("ref") == native_ref or ticket.get("url") == native_ref
        ),
        None,
    )
    if exact is not None:
        return exact
    if "/" not in native_ref and native_ref.removeprefix("#") == unit["ref"]:
        return next((ticket for ticket in tickets if ticket.get("ref") == unit["ref"]), None)
    return None


def plan_branch(plan_path: str) -> str:
    slug = sanitize_ref(
        re.sub(r"\.md$", "", Path(plan_path).name, flags=re.I).removeprefix("PLAN-")
    )
    return "plan-" + (slug or "0") if not slug or re.fullmatch(r"[0-9]+", slug) else slug


def worktree_slug(branch: str) -> str:
    return sanitize_ref(branch) or "unit"


def make_unit(pipeline: str, item: str, cwd: str, run_dir: str, base: str = "") -> Json:
    plan_path = os.path.abspath(os.path.join(cwd, item.strip())) if pipeline == "plan" else None
    ref = plan_branch(plan_path) if plan_path is not None else ticket_ref(item)
    branch = ref if plan_path is not None else ticket_branch(ref)
    unit = {
        "ref": ref,
        "branch": branch,
        "worktree": os.path.join(run_dir, "worktrees", worktree_slug(branch)),
        "base": base,
    }
    if plan_path is not None:
        unit["plan_path"] = plan_path
    else:
        unit["ticket_ref"] = item.strip()
    return unit


def parse_items(text: str) -> list[str]:
    trimmed = text.strip()
    raw = []
    if trimmed.startswith("["):
        try:
            values = json.loads(trimmed)
            if isinstance(values, list):
                raw = [
                    value if isinstance(value, str) else value["ref"]
                    for value in values
                    if isinstance(value, str)
                    or isinstance(value, dict)
                    and isinstance(value.get("ref"), str)
                ]
        except ValueError:
            pass
    if not raw:
        raw = re.split(r"[,;\n]", trimmed)
    return list(dict.fromkeys(item.strip() for item in raw if item.strip()))
