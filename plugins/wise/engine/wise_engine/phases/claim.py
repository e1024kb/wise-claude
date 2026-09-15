from pathlib import Path

from ..constants import ATTACHED_PIPELINES
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
from .pr import view_pr

OWNED = "owned"


def is_owned(ctx: Json) -> bool:
    return ctx["ledger"]["cursors"].get("claim") == OWNED


async def attach_phase(ctx: Json) -> Json:
    """Claim for the `pr` and `implement` pipelines: bind to the checked-out branch."""
    unit, pipeline = ctx["unit"], ctx["config"]["pipeline"]
    if pipeline == "implement" and not Path(unit.get("plan_path", "")).exists():
        return fail(f"missing: plan file {unit.get('plan_path', '?')} not found")
    head = await git(ctx, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    branch = head["stdout"].strip() if ok(head) else ""
    if not branch:
        return fail("claim: the checkout is detached; check out a named branch first")
    if branch in ("main", "master") or branch.startswith("release"):
        return fail(f"claim: refusing to work on protected branch {branch}")
    if pipeline == "pr" and branch != unit["branch"]:
        return fail(f"claim: expected branch {unit['branch']}, the checkout is on {branch}")
    attached = {**unit, "branch": branch, "worktree": str(Path(ctx["cwd"]).resolve())}
    if pipeline == "pr":
        pr = await view_pr(ctx, branch)
        if pr is None:
            return fail(f"claim: no pull request for {branch}; create one first")
        if pr["state"] == "MERGED":
            return fail(f"pr-merged: #{pr['number']}", "merged", {"unit": {**attached, "pr": pr}})
        if pr["state"] != "OPEN":
            return fail(f"claim: pull request #{pr['number']} is {pr['state'].lower()}", "skipped")
        attached["base"] = unit["base"] or pr.get("base") or await resolve_base(ctx)
        attached["pr"] = {"number": pr["number"], "url": pr["url"]}
        ctx["log"](f"claim: watching #{pr['number']} on {branch} (base {attached['base']})")
    else:
        attached["base"] = unit["base"] or await resolve_base(ctx)
        ctx["log"](f"claim: implementing on {branch} (base {attached['base']})")
    return pass_({"unit": attached, "cursors": {**ctx["ledger"]["cursors"], "claim": OWNED}})


async def claim_phase(ctx: Json) -> Json:
    unit = ctx["unit"]
    if ctx["config"]["pipeline"] in ATTACHED_PIPELINES:
        return await attach_phase(ctx)
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
