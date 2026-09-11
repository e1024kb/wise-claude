import re
from pathlib import Path

from ..ledger import apply_worktree_include
from .common import (
    Json,
    NETWORK_CMD_TIMEOUT_MS,
    err_text,
    fail,
    git,
    local_branch_exists,
    ok,
    pass_,
)

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
    fetched = await git(ctx, ["fetch", "origin", base], {"timeout_ms": NETWORK_CMD_TIMEOUT_MS})
    if not ok(fetched):
        if not ok(await git(ctx, ["rev-parse", "--verify", "--quiet", f"origin/{base}"])):
            return fail(f"worktree: fetch origin/{base} failed and no local copy")
        ctx["log"](f"worktree: fetch failed, using the local origin/{base}")
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
                f"origin/{base}",
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
