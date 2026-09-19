import asyncio

import pytest

from wise_engine.phases.remote import (
    classify_host,
    detect_remote,
    is_ssh_style,
    remote_host,
)


@pytest.mark.parametrize(
    "url,host",
    [
        ("https://github.com/a/b.git", "github.com"),
        ("https://user:token@gitlab.com:8443/a/b.git", "gitlab.com"),
        ("ssh://git@github.com:22/a/b.git", "github.com"),
        ("git@github.com:a/b.git", "github.com"),
        ("git@gitlab.com:a/b.git", "gitlab.com"),
        ("/srv/repo.git", ""),
        ("file:///srv/repo.git", ""),
        ("../repo", ""),
        ("", ""),
    ],
)
def test_remote_host(url, host):
    assert remote_host(url) == host


@pytest.mark.parametrize(
    "host,gh_knows,kind",
    [
        ("github.com", False, "github"),
        ("acme.ghe.com", False, "github"),
        ("gitlab.com", False, "other"),
        ("ghes.corp.example", True, "github"),
        ("ghes.corp.example", False, "other"),
    ],
)
def test_classify_host(host, gh_knows, kind):
    assert classify_host(host, gh_knows_host=gh_knows) == kind


def test_is_ssh_style():
    assert is_ssh_style("git@github.com:a/b.git")
    assert is_ssh_style("ssh://git@github.com/a/b.git")
    assert not is_ssh_style("https://github.com/a/b.git")
    assert not is_ssh_style("/srv/repo.git")


class FakeCtx:
    def __init__(self, origin_url, *, gh_hosts=(), ssh_hosts=None):
        self.origin_url = origin_url
        self.gh_hosts = set(gh_hosts)
        self.ssh_hosts = ssh_hosts or {}
        self.calls = []
        self.logs = []
        self.ctx = {
            "cwd": ".",
            "env": {},
            "exec": self.execute,
            "log": self.logs.append,
        }

    async def execute(self, cmd, args, opts):
        self.calls.append((cmd, args))
        result = {"code": 0, "stdout": "", "stderr": "", "timed_out": False}
        if cmd == "git" and args[:3] == ["remote", "get-url", "origin"]:
            if self.origin_url is None:
                return {**result, "code": 1}
            return {**result, "stdout": self.origin_url + "\n"}
        if cmd == "gh" and args[:2] == ["auth", "status"]:
            host = args[args.index("--hostname") + 1]
            return {**result, "code": 0 if host in self.gh_hosts else 1}
        if cmd == "ssh" and args[0] == "-G":
            resolved = self.ssh_hosts.get(args[1])
            return {**result, "stdout": f"hostname {resolved}\n" if resolved else ""}
        return result


def test_detect_no_origin():
    fake = FakeCtx(None)
    remote = asyncio.run(detect_remote(fake.ctx))
    assert remote == {"kind": "none", "host": ""}


def test_detect_github_skips_gh():
    fake = FakeCtx("https://github.com/a/b.git")
    remote = asyncio.run(detect_remote(fake.ctx))
    assert remote == {"kind": "github", "host": "github.com"}
    assert not any(cmd == "gh" for cmd, _ in fake.calls)


def test_detect_ghes_via_gh_auth():
    fake = FakeCtx("https://ghes.corp.example/a/b.git", gh_hosts=["ghes.corp.example"])
    remote = asyncio.run(detect_remote(fake.ctx))
    assert remote == {"kind": "github", "host": "ghes.corp.example"}
    assert any(cmd == "gh" for cmd, _ in fake.calls)


def test_detect_gitlab_is_other():
    fake = FakeCtx("git@gitlab.com:a/b.git")
    remote = asyncio.run(detect_remote(fake.ctx))
    assert remote == {"kind": "other", "host": "gitlab.com"}


def test_detect_resolves_ssh_alias():
    fake = FakeCtx("github-work:a/b.git", ssh_hosts={"github-work": "github.com"})
    remote = asyncio.run(detect_remote(fake.ctx))
    assert remote == {"kind": "github", "host": "github.com"}
    assert any(cmd == "ssh" for cmd, _ in fake.calls)


def test_detect_resolves_dotted_ssh_alias():
    # A dotted alias (e.g. `github.work` in ~/.ssh/config) still resolves.
    fake = FakeCtx("git@github.work:a/b.git", ssh_hosts={"github.work": "github.com"})
    remote = asyncio.run(detect_remote(fake.ctx))
    assert remote == {"kind": "github", "host": "github.com"}
    assert any(cmd == "ssh" for cmd, _ in fake.calls)


def test_detect_known_github_skips_ssh_resolution():
    # A known GitHub host over SSH needs no alias lookup.
    fake = FakeCtx("git@github.com:a/b.git")
    remote = asyncio.run(detect_remote(fake.ctx))
    assert remote == {"kind": "github", "host": "github.com"}
    assert not any(cmd == "ssh" for cmd, _ in fake.calls)


@pytest.mark.parametrize(
    "failure",
    [
        {"code": 1, "timed_out": True},
        {"code": 1, "error": "spawn ENOENT"},
        {"code": 128},
    ],
)
def test_detect_failure_assumes_github(failure):
    # A detection failure must not read as a missing origin (which would
    # silently skip push/pr). It falls back to the GitHub default instead.
    fake = FakeCtx("https://github.com/a/b.git")

    async def failing(cmd, args, opts):
        fake.calls.append((cmd, args))
        base = {"code": 0, "stdout": "", "stderr": "", "timed_out": False}
        if cmd == "git" and args[:3] == ["remote", "get-url", "origin"]:
            return {**base, **failure}
        return base

    fake.ctx["exec"] = failing
    remote = asyncio.run(detect_remote(fake.ctx))
    assert remote == {"kind": "github", "host": ""}


def test_detect_never_logs_credentials():
    fake = FakeCtx("https://user:s3cr3t-token@gitlab.com/a/b.git")
    asyncio.run(detect_remote(fake.ctx))
    assert all("s3cr3t-token" not in line for line in fake.logs)
