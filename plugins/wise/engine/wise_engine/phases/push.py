import re
from pathlib import PurePosixPath

from .common import (
    Json,
    NETWORK_CMD_TIMEOUT_MS,
    err_text,
    fail,
    git,
    is_protected_branch,
    ok,
    pass_,
    remote_branch_exists,
    unit_base_ref,
)

# A file whose name starts with a sequence number: `0042_add_users.sql`,
# `V42__add_users.sql` (Flyway), `0007-record-decision.md` (ADR).
NUMBERED_RE = re.compile(r"^V?(\d+)(?:__|[_.-])")
DATED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def sequence_number(name: str) -> str | None:
    """The sequence digits a file name starts with; None for dated names
    (`2024-05-01-post.md`) and all-digit stems (`404.svg`), which are not
    sequences."""
    if DATED_RE.match(name) or PurePosixPath(name).stem.isdigit():
        return None
    match = NUMBERED_RE.match(name)
    return match[1] if match else None


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


async def rebase_onto_base(ctx: Json) -> Json:
    """Rebase a branch origin does not have yet onto the freshly fetched base.

    A branch already on origin is left alone: rewriting it would need a force
    push. A conflicting rebase is aborted and fails the unit with the path to
    resolve by hand."""
    unit = ctx["unit"]
    worktree = {"cwd": unit["worktree"]}
    if await remote_branch_exists(ctx, unit["branch"]):
        return pass_()
    base = unit.get("base") or "main"
    fetched = await git(ctx, ["fetch", "origin", base], {"timeout_ms": NETWORK_CMD_TIMEOUT_MS})
    if not ok(fetched):
        ctx["log"](f"push: fetch origin {base} failed; rebasing onto the last fetched base")
    ref = unit_base_ref(unit)
    if ok(await git(ctx, ["merge-base", "--is-ancestor", ref, "HEAD"], worktree)):
        return pass_()
    rebased = await git(ctx, ["rebase", ref], worktree)
    if ok(rebased):
        ctx["log"](f"push: rebased {unit['branch']} onto {ref}")
        return pass_()
    await git(ctx, ["rebase", "--abort"], worktree)
    return fail(
        f"push: rebase onto {ref} conflicts ({err_text(rebased, 160)}); "
        f"resolve it in {unit['worktree']} and resume"
    )


NON_FAST_FORWARD = "non-fast-forward"


async def ensure_fast_forward(ctx: Json) -> Json:
    """Make the next push a fast-forward of `origin/<branch>`.

    A fix pass after the first push may rebase or amend commits origin
    already has, and a plain push of that history is rejected. Rewritten
    history is rebuilt without force: back to `origin/<branch>`, the base
    merged in when the fix pass brought it in (a conflicted path takes the
    fix pass's own resolution), then each commit the fix pass made that
    origin lacks (by patch id) cherry-picked on top. When that fails, the
    rewritten head is restored and the unit fails `non-fast-forward`."""
    unit = ctx["unit"]
    branch, path = unit["branch"], unit["worktree"]
    worktree = {"cwd": path}
    remote = f"origin/{branch}"
    base = unit.get("base") or "main"
    fetched = await git(
        ctx, ["fetch", "origin", branch, base], {"timeout_ms": NETWORK_CMD_TIMEOUT_MS}
    )
    if not ok(fetched):
        ctx["log"](f"push: fetch origin {branch} failed; checking the last fetched {remote}")
    tracked = await git(
        ctx, ["show-ref", "--verify", "--quiet", f"refs/remotes/{remote}"], worktree
    )
    if not ok(tracked):
        return pass_()
    if ok(await git(ctx, ["merge-base", "--is-ancestor", remote, "HEAD"], worktree)):
        return pass_()
    head = await git(ctx, ["rev-parse", "HEAD"], worktree)
    diverged = head["stdout"].strip() if ok(head) else ""
    if not diverged:
        return fail(f"{NON_FAST_FORWARD}: cannot read the head of {path}", NON_FAST_FORWARD)
    ctx["log"](f"push: HEAD {diverged[:12]} rewrote {remote}; replaying the fix on top of it")

    def failed(why: str) -> Json:
        return fail(
            f"{NON_FAST_FORWARD}: the fix pass rewrote commits already on {remote} "
            f"(HEAD {diverged[:12]} does not contain it) and replaying them failed: {why}. "
            f"The rewritten head stays in {path}: merge the base into {remote}, re-apply "
            "the fix commits and push without force",
            NON_FAST_FORWARD,
        )

    dirty = await git(ctx, ["status", "--porcelain", "--untracked-files=no"], worktree)
    if not ok(dirty) or dirty["stdout"].strip():
        return failed("the worktree has uncommitted changes")
    ref = unit_base_ref(unit)
    listed = await git(
        ctx,
        [
            "rev-list",
            "--reverse",
            "--no-merges",
            "--right-only",
            "--cherry-pick",
            f"{remote}...HEAD",
            f"^{ref}",
        ],
        worktree,
    )
    if not ok(listed):
        return failed(f"cannot list the fix commits ({err_text(listed, 160)})")
    commits = listed["stdout"].split()
    brings_base = ok(
        await git(ctx, ["merge-base", "--is-ancestor", ref, "HEAD"], worktree)
    ) and not ok(await git(ctx, ["merge-base", "--is-ancestor", ref, remote], worktree))
    # The tree the fix pass resolved the base into, before its own commits.
    resolution = f"{commits[0]}^" if commits else diverged

    async def restore(why: str) -> Json:
        await git(ctx, ["cherry-pick", "--abort"], worktree)
        await git(ctx, ["merge", "--abort"], worktree)
        await git(ctx, ["reset", "--hard", diverged], worktree)
        return failed(why)

    async def unmerged() -> list[str]:
        result = await git(ctx, ["diff", "--name-only", "--diff-filter=U"], worktree)
        return result["stdout"].split("\n") if ok(result) and result["stdout"].strip() else []

    reset = await git(ctx, ["reset", "--hard", remote], worktree)
    if not ok(reset):
        return await restore(f"cannot check out {remote} ({err_text(reset, 160)})")
    if brings_base:
        merged = await git(ctx, ["merge", "--no-edit", ref], worktree)
        if not ok(merged):
            conflicted = [name for name in await unmerged() if name]
            if not conflicted:
                return await restore(f"merging {ref} failed ({err_text(merged, 160)})")
            for name in conflicted:
                present = ok(await git(ctx, ["cat-file", "-e", f"{resolution}:{name}"], worktree))
                taken = await git(
                    ctx,
                    ["checkout", resolution, "--", name] if present else ["rm", "-q", "--", name],
                    worktree,
                )
                if not ok(taken):
                    return await restore(f"cannot resolve {name} from the fix pass's rebase")
            committed = await git(ctx, ["commit", "--no-edit"], worktree)
            if not ok(committed):
                return await restore(
                    f"cannot commit the merge of {ref} ({err_text(committed, 160)})"
                )
            ctx["log"](
                f"push: merged {ref} into {branch}, {len(conflicted)} conflicted path(s) "
                "resolved as the fix pass resolved them"
            )
        else:
            ctx["log"](f"push: merged {ref} into {branch}")
    for commit in commits:
        picked = await git(ctx, ["cherry-pick", commit], worktree)
        if ok(picked):
            continue
        staged = await git(ctx, ["diff", "--cached", "--quiet"], worktree)
        if not await unmerged() and ok(staged):
            # Already applied by the merge: nothing left of this commit.
            await git(ctx, ["cherry-pick", "--skip"], worktree)
            continue
        return await restore(f"cherry-pick {commit[:12]} conflicts ({err_text(picked, 160)})")
    if not ok(await git(ctx, ["merge-base", "--is-ancestor", remote, "HEAD"], worktree)):
        return await restore(f"the replayed head does not contain {remote}")
    ctx["log"](f"push: replayed {len(commits)} fix commit(s) on {remote}")
    return pass_()


async def sequence_collisions(ctx: Json) -> list[str]:
    """Numbered files this branch adds whose number the base already uses in
    the same directory, one finding line each."""
    unit = ctx["unit"]
    worktree = {"cwd": unit["worktree"]}
    ref = unit_base_ref(unit)
    added = await git(ctx, ["diff", "--name-only", "--diff-filter=A", f"{ref}...HEAD"], worktree)
    if not ok(added):
        return []
    by_dir: dict[str, list[tuple[str, str]]] = {}
    for line in added["stdout"].splitlines():
        path = PurePosixPath(line.strip())
        number = sequence_number(path.name)
        if number is not None:
            by_dir.setdefault(str(path.parent), []).append((path.name, number))
    findings = []
    for directory, files in by_dir.items():
        prefix = "" if directory == "." else directory + "/"
        listed = await git(ctx, ["ls-tree", "--name-only", ref, "--", prefix or "."], worktree)
        if not ok(listed):
            continue
        taken: dict[int, str] = {}
        for entry in listed["stdout"].splitlines():
            name = PurePosixPath(entry.strip()).name
            number = sequence_number(name)
            if number is not None:
                taken.setdefault(int(number), name)
        for name, number in files:
            other = taken.get(int(number))
            if other is not None and other != name:
                findings.append(
                    f"{prefix}{name}: number {number} is already used by {prefix}{other} "
                    f"on {ref}; renumber it to the next free number and update every reference"
                )
    return findings
