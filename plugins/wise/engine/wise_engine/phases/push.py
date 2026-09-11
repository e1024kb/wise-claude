from .common import (
    Json,
    NETWORK_CMD_TIMEOUT_MS,
    err_text,
    fail,
    git,
    is_protected_branch,
    ok,
    pass_,
)


async def push_phase(ctx: Json) -> Json:
    branch = ctx["unit"]["branch"]
    if is_protected_branch(branch):
        return fail(f"push: refused, {branch} is a protected branch")
    result = await git(
        ctx, ["push", "-u", "origin", branch], {"timeout_ms": NETWORK_CMD_TIMEOUT_MS}
    )
    if not ok(result):
        return fail(f"push: {err_text(result)}")
    ctx["log"](f"push: origin/{branch} updated")
    return pass_()
