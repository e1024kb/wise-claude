from pathlib import Path

from .common import (
    Json,
    fail,
    gh,
    git,
    json_of,
    local_branch_exists,
    ok,
    pass_,
    remote_branch_exists,
    resolve_base,
)

OWNED = "owned"


def is_owned(ctx: Json) -> bool:
    return ctx["ledger"]["cursors"].get("claim") == OWNED


async def claim_phase(ctx: Json) -> Json:
    unit = ctx["unit"]
    if ctx["config"]["pipeline"] == "plan" and (
        "plan_path" not in unit or not Path(unit["plan_path"]).exists()
    ):
        return fail(f"missing: plan file {unit.get('plan_path', '?')} not found")
    base = unit["base"] or await resolve_base(ctx)
    with_base = {**unit, "base": base}
    if is_owned(ctx):
        ctx["log"](f"claim: {unit['ref']} owned by this run (resume)")
        return pass_({"unit": with_base})
    rows = json_of(
        await gh(
            ctx,
            [
                "pr",
                "list",
                "--head",
                unit["branch"],
                "--state",
                "merged",
                "--json",
                "number,url",
                "--limit",
                "1",
            ],
        )
    )
    if (
        isinstance(rows, list)
        and rows
        and isinstance(rows[0], dict)
        and type(rows[0].get("number")) in (int, float)
        and isinstance(rows[0].get("url"), str)
    ):
        pr = {"number": rows[0]["number"], "url": rows[0]["url"]}
        return fail(f"pr-merged: #{pr['number']}", "merged", {"unit": {**with_base, "pr": pr}})
    remote = await remote_branch_exists(ctx, unit["branch"])
    if remote is None:
        return fail("claim: origin unreachable (git ls-remote failed)")
    if remote:
        return fail(f"already-claimed: origin/{unit['branch']} exists", "skipped")
    if await local_branch_exists(ctx, unit["branch"]):
        return fail(f"already-claimed: local branch {unit['branch']} exists", "skipped")
    result = await git(ctx, ["worktree", "list", "--porcelain"])
    if ok(result) and f"\nbranch refs/heads/{unit['branch']}\n" in result["stdout"]:
        return fail(f"already-claimed: a worktree is on {unit['branch']}", "skipped")
    ctx["log"](f"claim: {unit['ref']} -> {unit['branch']} (base {base})")
    return pass_({"unit": with_base, "cursors": {**ctx["ledger"]["cursors"], "claim": OWNED}})
