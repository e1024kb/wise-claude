import os
from pathlib import Path

from ..constants import ATTACHED_PIPELINES
from .common import (
    NETWORK_CMD_TIMEOUT_MS,
    Json,
    base_ref,
    fail,
    gh,
    git,
    json_of,
    local_branch_exists,
    ok,
    pass_,
    remote_branch_exists,
    resolve_base,
    worktree_slug,
)
from .pr import view_pr
from .remote import remote_of, watch_skip_reason

OWNED = "owned"
# How many `<branch>-N` names a fresh unit tries before giving up.
BRANCH_SUFFIX_LIMIT = 20


async def _branch_taken(ctx: Json, branch: str) -> bool | None:
    """True when the branch exists on origin, locally, or in a registered
    worktree; None when origin is unreachable."""
    # Without an origin remote a branch is "taken" only when it exists
    # locally or in a worktree; never probe a remote that is not there.
    if remote_of(ctx)["kind"] == "none":
        remote: bool | None = False
    else:
        remote = await remote_branch_exists(ctx, branch)
        if remote is None:
            return None
    if remote or await local_branch_exists(ctx, branch):
        return True
    result = await git(ctx, ["worktree", "list", "--porcelain"])
    return ok(result) and f"\nbranch refs/heads/{branch}\n" in result["stdout"]


class OriginUnreachable(Exception):
    """`git ls-remote` against origin failed while probing a branch name."""


async def free_branch(ctx: Json, wanted: str) -> str | None:
    """`wanted` when nothing holds it, else the first free `<wanted>-N` (N from
    2); None when every candidate is taken. Raises OriginUnreachable when a
    probe fails, so a transient failure never reads as "all names taken"."""
    for n in range(1, BRANCH_SUFFIX_LIMIT + 1):
        candidate = wanted if n == 1 else f"{wanted}-{n}"
        taken = await _branch_taken(ctx, candidate)
        if taken is None:
            raise OriginUnreachable(candidate)
        if not taken:
            return candidate
    return None


def is_owned(ctx: Json) -> bool:
    return ctx["ledger"]["cursors"].get("claim") == OWNED


async def attach_phase(ctx: Json) -> Json:
    """Claim for the `pr` and `implement` pipelines: bind to the checked-out branch."""
    unit, pipeline = ctx["unit"], ctx["config"]["pipeline"]
    if pipeline == "implement" and not Path(unit.get("plan_path", "")).is_file():
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
        remote = remote_of(ctx)
        if remote["kind"] != "github":
            return fail(watch_skip_reason(remote), "skipped")
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
    # Fetch the base so the diff range exists even in a shallow or
    # single-branch checkout; a local-only base resolves to its local ref.
    # Without an origin remote there is nothing to fetch.
    if remote_of(ctx)["kind"] != "none":
        await git(
            ctx, ["fetch", "origin", attached["base"]], {"timeout_ms": NETWORK_CMD_TIMEOUT_MS}
        )
    ref = await base_ref(ctx, attached["base"])
    if ref is None:
        return fail(f"claim: base {attached['base']} exists neither on origin nor locally")
    attached["base_ref"] = ref
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
    # The merged-PR probe only makes sense against a GitHub origin.
    if remote_of(ctx)["kind"] == "github":
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
    # A branch another run, a person, or a stale checkout already holds is
    # never reused: the unit takes the first free `<branch>-N` instead.
    try:
        branch = await free_branch(ctx, unit["branch"])
    except OriginUnreachable:
        return fail("claim: origin unreachable (git ls-remote failed)")
    if branch is None:
        return fail(
            f"already-claimed: {unit['branch']} and {BRANCH_SUFFIX_LIMIT - 1} suffixed names exist",
            "skipped",
        )
    if branch != unit["branch"]:
        ctx["log"](f"claim: {unit['branch']} already exists, using {branch}")
        with_base["branch"] = branch
        if os.path.abspath(with_base["worktree"]) != os.path.abspath(ctx["cwd"]):
            with_base["worktree"] = os.path.join(ctx["run_dir"], "worktrees", worktree_slug(branch))
    ctx["log"](f"claim: {unit['ref']} -> {branch} (base {base})")
    return pass_({"unit": with_base, "cursors": {**ctx["ledger"]["cursors"], "claim": OWNED}})
