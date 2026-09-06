"""`scripts/init.sh probe-git-ssh`: classifies ssh -T output from a clean env."""

import os
import stat
import subprocess
from pathlib import Path

import pytest

INIT_SH = Path(__file__).resolve().parents[1] / "scripts" / "init.sh"


def _fake_ssh(tmp_path: Path, script: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text("#!/bin/bash\n" + script)
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
    return bindir


def _probe(tmp_path: Path, script: str, agent: bool = True) -> dict[str, str]:
    bindir = _fake_ssh(tmp_path, script)
    env = {"HOME": str(tmp_path), "PATH": f"{bindir}:/usr/bin:/bin"}
    if agent:
        env["SSH_AUTH_SOCK"] = str(tmp_path / "agent.sock")
    out = subprocess.run(
        ["bash", str(INIT_SH), "probe-git-ssh"], env=env, capture_output=True, text=True, check=True
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def test_ok_when_github_greets(tmp_path: Path) -> None:
    r = _probe(tmp_path, 'echo "Hi e1024kb! You\'ve successfully authenticated" >&2; exit 1')
    assert r["STATUS"] == "ok"
    assert r["AGENT"] == "set"
    assert r["HOST"] == "github.com"
    assert "successfully authenticated" in r["DETAIL"]


def test_denied_when_no_usable_key(tmp_path: Path) -> None:
    r = _probe(tmp_path, 'echo "git@github.com: Permission denied (publickey)." >&2; exit 255', agent=False)
    assert r["STATUS"] == "denied"
    assert r["AGENT"] == "unset"


def test_unreachable_on_dns_failure(tmp_path: Path) -> None:
    r = _probe(tmp_path, 'echo "ssh: Could not resolve hostname github.com" >&2; exit 255')
    assert r["STATUS"] == "unreachable"


def test_child_env_is_clean(tmp_path: Path) -> None:
    # ssh sees only HOME, PATH and the agent socket: the engine's child env, not the shell's.
    r = _probe(tmp_path, 'env | sort | tr "\\n" " "; exit 255')
    seen = r["DETAIL"]
    assert "HOME=" in seen and "SSH_AUTH_SOCK=" in seen
    assert "LEAK=" not in seen
    assert r["STATUS"] == "unknown"


@pytest.mark.skipif(os.environ.get("SSH_AUTH_SOCK") is None, reason="no agent in this shell")
def test_host_argument_is_echoed(tmp_path: Path) -> None:
    bindir = _fake_ssh(tmp_path, 'echo "successfully authenticated" >&2; exit 1')
    env = {"HOME": str(tmp_path), "PATH": f"{bindir}:/usr/bin:/bin", "SSH_AUTH_SOCK": "/x"}
    out = subprocess.run(
        ["bash", str(INIT_SH), "probe-git-ssh", "gitlab.com"], env=env, capture_output=True, text=True, check=True
    ).stdout
    assert "HOST=gitlab.com" in out
