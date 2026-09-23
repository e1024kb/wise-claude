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
