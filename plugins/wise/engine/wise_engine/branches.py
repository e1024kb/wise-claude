from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from typing import Any

from .spawn import clean_env

Json = dict[str, Any]

BASE_BRANCH_RE = re.compile(r"^(main|master|release.*)$")
BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]*$")
RELEASE_LIMIT = 5


def is_base_branch(name: str) -> bool:
    return BASE_BRANCH_RE.match(name) is not None


def is_branch_name(name: str) -> bool:
    """A plain git branch name: safe to interpolate into a prompt or command unquoted."""
    return (
        BRANCH_NAME_RE.match(name) is not None
        and ".." not in name
        and "//" not in name
        and "/." not in name
        and not name.endswith(("/", ".", ".lock"))
        and "@{" not in name
    )


def _git(cwd: str, args: list[str], env: Mapping[str, str] | None = None) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=cwd,
            env=clean_env(parent=env),
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def branch_choices(cwd: str, env: Mapping[str, str] | None = None) -> Json | None:
    """Base-branch options for a checkout: current base-looking branch, default, releases."""
    if _git(cwd, ["rev-parse", "--is-inside-work-tree"], env) != "true":
        return None
    current = _git(cwd, ["symbolic-ref", "--quiet", "--short", "HEAD"], env) or ""
    default = (
        _git(cwd, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], env) or ""
    ).removeprefix("origin/")
    if not default:
        default = next(
            (
                name
                for name in ("main", "master")
                if _git(cwd, ["show-ref", "--verify", "--quiet", f"refs/heads/{name}"], env)
                is not None
                or _git(
                    cwd, ["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{name}"], env
                )
                is not None
            ),
            "",
        )
    listing = (
        _git(
            cwd,
            [
                "for-each-ref",
                "--sort=-committerdate",
                "--format=%(refname:short)",
                "refs/heads/release*",
                "refs/remotes/origin/release*",
            ],
            env,
        )
        or ""
    )
    releases: list[str] = []
    for line in listing.splitlines():
        name = line.strip().removeprefix("origin/")
        if name and name not in releases:
            releases.append(name)
    releases = releases[:RELEASE_LIMIT]
    options: list[Json] = []
    seen: set[str] = set()

    def add(name: str, description: str) -> None:
        if name and name not in seen:
            seen.add(name)
            options.append(dict(value=name, label=name, description=description))

    if current and is_base_branch(current):
        add(current, "the branch checked out now")
    add(default, "the repository's default branch")
    for name in releases:
        add(name, "recent release branch")
    if current and not is_base_branch(current):
        add(current, "the branch checked out now (not a usual base)")
    if not options:
        return None
    return dict(current=current, default=options[0]["value"], options=options)
