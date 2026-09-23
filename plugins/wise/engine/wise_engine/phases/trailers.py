"""Strip the Co-authored-by trailer the cursor harness adds to its commits.

Cursor's agent appends `Co-authored-by: Cursor <cursoragent@cursor.com>` to
every commit it makes. After a code-changing phase on cursor, the commits it
made are rewritten without that trailer, before anything is pushed. The trees
stay identical, so the checkout needs no update; only the branch ref moves.
"""

import re
import tempfile
from pathlib import Path

from .common import Json, git, ok

CURSOR_TRAILER_RE = re.compile(r"(?im)^co-authored-by:[^\n]*\bcursor[^\n]*(?:\n|$)")


def strip_trailer(message: str) -> str:
    return CURSOR_TRAILER_RE.sub("", message).rstrip() + "\n"


async def strip_cursor_trailers(ctx: Json, before: str) -> int:
    """Rewrite `before..HEAD` without cursor trailers; the rewritten count."""
    worktree = ctx["unit"]["worktree"]
    at = {"cwd": worktree}
    branch = await git(ctx, ["symbolic-ref", "--quiet", "--short", "HEAD"], at)
    listed = await git(ctx, ["rev-list", "--reverse", "--topo-order", f"{before}..HEAD"], at)
    if not ok(branch) or not branch["stdout"].strip() or not ok(listed):
        return 0
    shas = listed["stdout"].split()
    mapping: dict[str, str] = {}
    rewritten = 0
    fmt = "%T%x00%P%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B"
    with tempfile.TemporaryDirectory() as scratch:
        for sha in shas:
            info = await git(ctx, ["log", "-1", f"--format={fmt}", sha], at)
            parts = info["stdout"].split("\x00", 8) if ok(info) else []
            if len(parts) != 9:
                return 0
            tree, parents, an, ae, ad, cn, ce, cd, body = parts
            old_parents = parents.split()
            new_parents = [mapping.get(parent, parent) for parent in old_parents]
            message = strip_trailer(body)
            if message == body.rstrip() + "\n" and new_parents == old_parents:
                mapping[sha] = sha
                continue
            path = Path(scratch) / f"{sha}.msg"
            path.write_text(message)
            env = {
                **ctx["env"],
                "GIT_AUTHOR_NAME": an,
                "GIT_AUTHOR_EMAIL": ae,
                "GIT_AUTHOR_DATE": ad,
                "GIT_COMMITTER_NAME": cn,
                "GIT_COMMITTER_EMAIL": ce,
                "GIT_COMMITTER_DATE": cd,
            }
            args = [
                "commit-tree",
                tree,
                *[x for p in new_parents for x in ("-p", p)],
                "-F",
                str(path),
            ]
            made = await git(ctx, args, {"cwd": worktree, "env": env})
            if not ok(made) or not made["stdout"].strip():
                return 0
            mapping[sha] = made["stdout"].strip()
            rewritten += 1
    head = shas[-1] if shas else None
    if head is None or mapping.get(head, head) == head:
        return 0
    ref = f"refs/heads/{branch['stdout'].strip()}"
    moved = await git(ctx, ["update-ref", ref, mapping[head], head], at)
    if not ok(moved):
        return 0
    ctx["log"](f"trailers: removed the cursor Co-authored-by trailer from {rewritten} commit(s)")
    return rewritten
