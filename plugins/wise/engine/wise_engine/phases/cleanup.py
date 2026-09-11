from pathlib import Path

from .common import Json, err_text, git, ok, pass_


async def cleanup_phase(ctx: Json) -> Json:
    unit, ledger = ctx["unit"], ctx["ledger"]
    if ledger.get("verdict") != "merged" or "pr" not in unit:
        if Path(unit["worktree"]).exists():
            ctx["log"](f"cleanup: kept worktree {unit['worktree']}")
        return pass_({"cleaned": False})
    if Path(unit["worktree"]).exists():
        removed = await git(ctx, ["worktree", "remove", unit["worktree"]])
        if not ok(removed):
            forced = await git(ctx, ["worktree", "remove", "--force", unit["worktree"]])
            if not ok(forced):
                ctx["log"](f"cleanup: could not remove {unit['worktree']}: {err_text(forced)}")
                return pass_({"cleaned": False})
        ctx["log"](f"cleanup: removed worktree {unit['worktree']}")
    await git(ctx, ["branch", "-D", unit["branch"]])
    await git(ctx, ["worktree", "prune"])
    return pass_({"cleaned": True})
