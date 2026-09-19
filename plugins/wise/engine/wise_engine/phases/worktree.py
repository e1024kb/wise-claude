import re
from pathlib import Path

from ..ledger import apply_worktree_include
from .common import (
    Json,
    NETWORK_CMD_TIMEOUT_MS,
    base_ref,
    err_text,
    fail,
    git,
    local_branch_exists,
    ok,
    pass_,
)
from .remote import remote_of

INCLUDES_DONE = "includes-done"


def parse_worktrees(porcelain: str) -> list[Json]:
    rows = []
    for block in re.split(r"\n\n+", porcelain):
        row = {}
        for line in block.split("\n"):
            if line.startswith("worktree "):
                row["path"] = line[len("worktree ") :]
            elif line.startswith("branch refs/heads/"):
                row["branch"] = line[len("branch refs/heads/") :]
        if "path" in row:
            rows.append(row)
    return rows


async def _registered(ctx: Json, path: str) -> Json | None:
    result = await git(ctx, ["worktree", "list", "--porcelain"])
    if not ok(result):
        return None
    want = Path(path).resolve()
    return next(
        (row for row in parse_worktrees(result["stdout"]) if Path(row["path"]).resolve() == want),
        None,
    )


async def worktree_phase(ctx: Json) -> Json:
    unit = ctx["unit"]
    path = Path(unit["worktree"])
    base = unit["base"] or "main"
    remote = remote_of(ctx)
    fetched = (
        None
        if remote["kind"] == "none"
        else await git(ctx, ["fetch", "origin", base], {"timeout_ms": NETWORK_CMD_TIMEOUT_MS})
    )
    ref = await base_ref(ctx, base)
    if ref is None:
        return fail(f"worktree: base {base} exists neither on origin nor locally")
    # A GitHub PR cannot target a branch origin does not have; without a
    # GitHub remote no PR is opened, so a local-only base is fine.
    if remote["kind"] == "github" and not ref.startswith("origin/"):
        return fail(f"worktree: base {base} exists only locally; push it to origin first")
    if fetched is not None and not ok(fetched):
        ctx["log"](f"worktree: fetch origin {base} failed, using the last fetched {ref}")
    unit = {**unit, "base_ref": ref}
    if path.resolve() == Path(ctx["cwd"]).resolve():
        head = await git(ctx, ["symbolic-ref", "--quiet", "--short", "HEAD"])
        if not ok(head) or head["stdout"].strip() != unit["branch"]:
            status = await git(ctx, ["status", "--porcelain"])
            if not ok(status) or status["stdout"].strip():
                return fail("worktree: current tree has uncommitted or untracked changes")
            args = (
                ["checkout", unit["branch"]]
                if await local_branch_exists(ctx, unit["branch"])
                else ["checkout", "--no-track", "-b", unit["branch"], ref]
            )
            switched = await git(ctx, args)
            if not ok(switched):
                return fail(f"worktree: checkout failed: {err_text(switched)}")
        return pass_({"unit": {**unit, "worktree": str(path), "base": base}})
    reg = await _registered(ctx, str(path))
    if reg is None and path.exists():
        await git(ctx, ["worktree", "prune"])
        reg = await _registered(ctx, str(path))
        if reg is None:
            try:
                empty = not list(path.iterdir())
            except OSError:
                empty = False
            if not empty:
                return fail(f"worktree-corrupt: {path} exists but is not a worktree")
            path.rmdir()
    if reg is not None:
        if reg.get("branch") != unit["branch"]:
            return fail(f"worktree-conflict: {path} is on {reg.get('branch', 'detached HEAD')}")
        ctx["log"](f"worktree: reuse {path}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        args = (
            ["worktree", "add", str(path), unit["branch"]]
            if await local_branch_exists(ctx, unit["branch"])
            else [
                "worktree",
                "add",
                "--no-track",
                str(path),
                "-b",
                unit["branch"],
                ref,
            ]
        )
        added = await git(ctx, args)
        if not ok(added):
            return fail(f"worktree: git worktree add failed: {err_text(added)}")
        ctx["log"](f"worktree: created {path} on {unit['branch']}")
    cursors = dict(ctx["ledger"]["cursors"])
    if cursors.get("worktree") != INCLUDES_DONE:
        result = apply_worktree_include(ctx["cwd"], str(path))
        for notice in result["notices"]:
            ctx["log"](notice)
        if result["copied"] > 0:
            ctx["log"](f"worktree: copied {result['copied']} include path(s)")
        cursors["worktree"] = INCLUDES_DONE
    return pass_({"unit": {**unit, "worktree": str(path), "base": base}, "cursors": cursors})
