"""Classify the `origin` remote once so the units loop can skip the GitHub
phases (push / pr / request-review / watch) when the project has no GitHub
remote. Pure helpers (`remote_host`, `classify_host`) are unit-tested without
git; `detect_remote` gathers the inputs through the phase `exec`/`run` helpers.

The remote URL is never logged: it can embed credentials
(`https://user:token@host/...`). Only the classified host appears in any log,
reason, or skill message.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .common import Json, err_text, ok, run

GITHUB_HOSTS = ("github.com", "www.github.com", "ssh.github.com")
GITHUB_DEFAULT: Json = {"kind": "github", "host": ""}
# scp-like `[user@]host:path`: a colon that is not the `://` of a scheme.
_SCP = re.compile(r"^(?:[^@/]+@)?([^:/]+):(?!//)")
# How long to wait on `ssh -G <alias>` (no connection is made).
SSH_RESOLVE_TIMEOUT_MS = 10_000


def remote_host(url: str) -> str:
    """The lowercase host of a git remote URL, or `""` for a local path.

    Handles `scheme://[userinfo@]host[:port]/path`, scp-like
    `[user@]host:path`, and local paths / `file://` (host-less). Never raises.
    """
    if not url:
        return ""
    if "://" not in url:
        match = _SCP.match(url)
        if match:
            return match.group(1).lower()
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return ""
    return host.lower() if host else ""


def is_ssh_style(url: str) -> bool:
    """True for `ssh://` / `git+ssh://` URLs and scp-like `[user@]host:path`."""
    if url.startswith(("ssh://", "git+ssh://")):
        return True
    return "://" not in url and bool(_SCP.match(url))


def classify_host(host: str, *, gh_knows_host: bool) -> str:
    """`github` for a GitHub host (`github.com`, `*.ghe.com`, or a GHES host
    the local `gh` is logged into); otherwise `other`."""
    if host in GITHUB_HOSTS or host.endswith(".ghe.com") or gh_knows_host:
        return "github"
    return "other"


async def _resolve_alias(ctx: Json, alias: str) -> str:
    """Resolve an SSH host alias to its real hostname via `ssh -G` (no
    connection). Returns the alias unchanged when resolution yields nothing."""
    result = await run(ctx, "ssh", ["-G", alias], {"timeout_ms": SSH_RESOLVE_TIMEOUT_MS})
    if not ok(result):
        return alias
    for line in result["stdout"].splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] == "hostname":
            return parts[1].strip().lower() or alias
    return alias


async def detect_remote(ctx: Json) -> Json:
    """Classify `origin` as `github`, `other`, or `none`, logging one line.

    Calls `ctx["exec"]` directly for `git remote get-url origin` so the URL
    (which may carry credentials) never reaches a log path.
    """
    result = await ctx["exec"](
        "git", ["remote", "get-url", "origin"], {"cwd": ctx["cwd"], "env": ctx["env"]}
    )
    if ok(result):
        url = result["stdout"].strip()
    elif result.get("timed_out") or "error" in result or result["code"] == 128:
        # A timeout, spawn failure, or `not a git repository` (128) is a
        # detection failure, not a confirmed missing origin. Do not silently
        # skip the GitHub phases for those: assume GitHub so push/pr still run
        # (and fail loudly if the remote is truly unusable). Git reports a
        # genuinely absent origin with a non-zero exit that is none of these.
        ctx["log"](f"remote: could not classify origin ({err_text(result, 120)}); assuming GitHub")
        return {"kind": "github", "host": ""}
    else:
        url = ""
    if not url:
        remote = {"kind": "none", "host": ""}
        ctx["log"](skip_line(remote))
        return remote
    host = remote_host(url)
    # Resolve an SSH host alias to its real hostname, unless it is already a
    # known GitHub host (which never needs a lookup). This covers dotted
    # aliases such as `github.work` that map to `github.com` in ~/.ssh/config.
    if host and is_ssh_style(url) and host not in GITHUB_HOSTS and not host.endswith(".ghe.com"):
        host = await _resolve_alias(ctx, host)
    if not host:
        remote = {"kind": "other", "host": ""}
        ctx["log"](skip_line(remote))
        return remote
    if host in GITHUB_HOSTS or host.endswith(".ghe.com"):
        remote = {"kind": "github", "host": host}
        ctx["log"](skip_line(remote))
        return remote
    status = await run(ctx, "gh", ["auth", "status", "--hostname", host])
    remote = {"kind": classify_host(host, gh_knows_host=ok(status)), "host": host}
    ctx["log"](skip_line(remote))
    return remote


def remote_of(ctx: Json) -> Json:
    """The detected remote, defaulting to `github` so phase tests and callers
    that never ran detection keep today's GitHub behavior."""
    return ctx["config"].get("remote", GITHUB_DEFAULT)


def _where(remote: Json) -> str:
    if remote["kind"] == "none":
        return "no origin remote"
    return f"origin is {remote['host'] or 'a local path'}"


def skip_line(remote: Json) -> str:
    """The one detection line logged at the start of a units step."""
    if remote["kind"] == "github":
        return f"remote: origin is GitHub ({remote['host']})"
    if remote["kind"] == "none":
        return "remote: no origin remote; push, pr, request-review and watch are skipped"
    host = remote["host"] or "a local path"
    return f"remote: origin is {host}, not GitHub; pr, request-review and watch are skipped"


def no_pr_reason(remote: Json, branch: str, worktree: str) -> str:
    """The `no-pr` verdict reason for a ticket / plan unit with no GitHub remote."""
    if remote["kind"] == "none":
        return (
            f"no-github-remote: no origin remote; {branch} committed locally in "
            f"{worktree}, no PR opened"
        )
    host = remote["host"]
    if not host:
        return (
            f"no-github-remote: origin is a local path, not GitHub; {branch} pushed, no PR opened"
        )
    return (
        f"no-github-remote: origin is {host}, not GitHub; {branch} pushed, "
        f"no PR opened (open a merge request on {host})"
    )


def watch_skip_reason(remote: Json) -> str:
    """The `skipped` reason for a `pr` unit (pr-watch) with no GitHub remote."""
    return f"no-github-remote: {_where(remote)}; nothing to watch"
