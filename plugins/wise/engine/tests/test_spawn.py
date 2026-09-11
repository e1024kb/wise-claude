from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from wise_engine.spawn import (
    PASSTHROUGH_VARS,
    SpawnOptions,
    clean_env,
    create_line_splitter,
    is_blocked_var,
    spawn_clean,
)


def test_clean_env_routes_only_requested_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    parent = {name: f"value-{name}" for name in PASSTHROUGH_VARS}
    parent.update({
        "XDG_DATA_HOME": "/xdg", "CLAUDE_CONFIG_DIR": "/config",
        "ANTHROPIC_API_KEY": "fixture-key", "GH_TOKEN": "fixture-token",
        "EDITOR": "vim", "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CLAUDE_SESSION_ID": "session", "CLAUDE_FOO_SESSION_BAR": "session",
    })
    expected = {name: parent[name] for name in PASSTHROUGH_VARS}
    expected["XDG_DATA_HOME"] = "/xdg"
    assert clean_env(parent=parent) == expected
    assert len(PASSTHROUGH_VARS) == 25
    assert "GH_TOKEN" not in PASSTHROUGH_VARS
    assert clean_env(parent={}) == {}
    assert clean_env(parent={"HOME": None}) == {}
    assert clean_env(parent=parent, keep=["CLAUDE_CONFIG_DIR", "UNSET"]) == {
        **expected, "CLAUDE_CONFIG_DIR": "/config",
    }
    blocked = [name for name in parent if is_blocked_var(name)]
    assert clean_env(parent=parent, keep=blocked, secrets=blocked) == expected
    assert clean_env(parent=parent, secrets=["ANTHROPIC_API_KEY"]) == {
        **expected, "ANTHROPIC_API_KEY": "fixture-key",
    }
    assert clean_env(parent=parent, extra={"PATH": "/override", "CLAUDECODE": "explicit"}) == {
        **expected, "PATH": "/override", "CLAUDECODE": "explicit",
    }
    monkeypatch.setenv("PATH", "/test-path")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert clean_env()["PATH"] == "/test-path"
    assert "CLAUDECODE" not in clean_env()


@pytest.mark.parametrize("name", [
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_SESSION_ID", "CLAUDE_X_SESSION_Y",
])
def test_blocked_names(name: str) -> None:
    assert is_blocked_var(name)


@pytest.mark.parametrize("name", [
    "CLAUDE_CONFIG_DIR", "HOME", "ANTHROPIC_API_KEY", "CLAUDE", "XDG_DATA_HOME",
])
def test_allowed_names(name: str) -> None:
    assert not is_blocked_var(name)


def test_line_splitter_handles_crlf_unicode_and_unterminated_lines() -> None:
    splitter = create_line_splitter()
    output = []
    for byte in '{"text":"café 🐍"}\r\n\n{"last":true}'.encode():
        output.extend(splitter.feed(bytes([byte])))
    assert output == ['{"text":"café 🐍"}', ""]
    assert splitter.finish() == ['{"last":true}']
    assert splitter.finish() == []
    assert splitter.feed("a\rb\r\ntrailing\r") == ["a\rb"]
    assert splitter.finish() == ["trailing\r"]
    assert splitter.feed(b"\xf0\x9f") == []
    assert splitter.finish() == ["\ufffd"]


def test_spawn_exact_environment_cwd_and_exit(tmp_path: Path) -> None:
    async def check() -> None:
        env = {"WISE_X": "1", "PYTHONCOERCECLOCALE": "0"}
        child = await spawn_clean(sys.executable, ["-c", (
            "import json,os,sys; print(json.dumps({'env':dict(os.environ),"
            "'cwd':os.getcwd()})); sys.exit(3)"
        )], SpawnOptions(tmp_path, env))
        assert child.pid > 0
        assert child.process is not None
        child.stdin.close()
        output, result = await asyncio.gather(child.stdout.read(), child.exited)
        data = json.loads(output)
        actual = {key: value for key, value in data["env"].items() if not key.startswith("__CF_")}
        assert actual == env
        assert data["cwd"] == str(tmp_path.resolve())
        assert result.code == 3
        assert result.signal is None
        assert not result.timed_out
        assert result.error is None

    asyncio.run(check())


@pytest.mark.parametrize(("cap", "expected"), [(0, ""), (10, "x" * 10), (100, "x" * 100)])
def test_stderr_is_drained_after_cap(tmp_path: Path, cap: int, expected: str) -> None:
    async def check() -> None:
        child = await spawn_clean(sys.executable, ["-c", (
            "import sys; sys.stderr.write('x' * 300000); sys.exit(1)"
        )], SpawnOptions(tmp_path, {}, stderr_cap=cap, timeout_ms=5000))
        child.stdin.end()
        result = await child.exited
        assert result.stderr == expected
        assert result.code == 1
        assert not result.timed_out

    asyncio.run(check())


def test_stderr_cap_uses_utf16_units(tmp_path: Path) -> None:
    async def check() -> None:
        child = await spawn_clean(sys.executable, ["-c", (
            "import os; os.write(2, bytes.fromhex('61f09f908d62'))"
        )], SpawnOptions(tmp_path, {}, stderr_cap=2))
        child.stdin.close()
        result = await child.exited
        assert result.stderr == "a\ud83d"

    asyncio.run(check())


def test_stdin_delivery_and_close(tmp_path: Path) -> None:
    async def check() -> None:
        child = await spawn_clean(sys.executable, ["-c", (
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read().upper())"
        )], SpawnOptions(tmp_path, {}))
        child.stdin.write("hello\n")
        await child.stdin.drain()
        child.stdin.end(b"world")
        await child.stdin.wait_closed()
        output, result = await asyncio.gather(child.stdout.read(), child.exited)
        assert output == b"HELLO\nWORLD"
        assert result.code == 0

    asyncio.run(check())


def test_child_closing_stdin_does_not_fail_caller(tmp_path: Path) -> None:
    async def check() -> None:
        child = await spawn_clean(sys.executable, ["-c", (
            "import os,time; os.close(0); print('closed', flush=True); time.sleep(0.1)"
        )], SpawnOptions(tmp_path, {}))
        assert await child.stdout.readline() == b"closed\n"
        child.stdin.write(b"x" * 1000000)
        await child.stdin.drain()
        child.stdin.close()
        await child.stdin.wait_closed()
        assert (await child.exited).code == 0

    asyncio.run(check())


@pytest.mark.parametrize("ignore_term", [False, True])
def test_timeout_signals_and_escalation(tmp_path: Path, ignore_term: bool) -> None:
    async def check() -> None:
        script = "import signal,time; "
        if ignore_term:
            script += "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        script += "print('ready', flush=True); time.sleep(60)"
        child = await spawn_clean(sys.executable, ["-c", script], SpawnOptions(
            tmp_path, {}, timeout_ms=800, kill_grace_ms=50,
        ))
        assert await child.stdout.readline() == b"ready\n"
        result = await child.exited
        assert result.timed_out
        assert result.code is None
        assert result.signal == ("SIGKILL" if ignore_term else "SIGTERM")

    asyncio.run(check())


def test_kill_signals_child_and_grandchild(tmp_path: Path) -> None:
    async def check() -> None:
        script = (
            "import os,signal,subprocess,sys,time\n"
            "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "def stop(sig, frame):\n"
            "    grandchild.wait(timeout=3)\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_DFL)\n"
            "    os.kill(os.getpid(), signal.SIGTERM)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "print(grandchild.pid, flush=True)\n"
            "time.sleep(60)\n"
        )
        child = await spawn_clean(sys.executable, ["-c", script], SpawnOptions(tmp_path, {}))
        grandchild = int(await child.stdout.readline())
        try:
            child.kill()
            result = await asyncio.wait_for(child.exited, 5)
            assert result.signal == "SIGTERM"
            assert not result.timed_out
            with pytest.raises(ProcessLookupError):
                os.kill(grandchild, 0)
        finally:
            child.kill("SIGKILL")
            try:
                os.kill(grandchild, signal.SIGKILL)
            except ProcessLookupError:
                pass

    asyncio.run(check())


@pytest.mark.parametrize("wait_until_ready", [False, True])
def test_cancelled_exit_wait_reaps_process(tmp_path: Path, wait_until_ready: bool) -> None:
    async def check() -> None:
        child = await spawn_clean(sys.executable, ["-c", (
            "import time; print('ready', flush=True); time.sleep(60)"
        )], SpawnOptions(tmp_path, {}))
        if wait_until_ready:
            assert await child.stdout.readline() == b"ready\n"
        child.exited.cancel()
        with pytest.raises(asyncio.CancelledError):
            await child.exited
        assert child.process is not None
        assert child.process.returncode == -signal.SIGKILL
        with pytest.raises(ProcessLookupError):
            os.kill(child.pid, 0)

    asyncio.run(check())


@pytest.mark.parametrize("failure", ["missing", "permission", "cwd"])
def test_spawn_failure_settles(tmp_path: Path, failure: str) -> None:
    async def check() -> None:
        cmd = sys.executable
        cwd = tmp_path
        expected = "ENOENT"
        if failure == "missing":
            cmd = str(tmp_path / "missing")
        elif failure == "permission":
            executable = tmp_path / "not-executable"
            executable.write_text("not executable")
            cmd = str(executable)
            expected = "EACCES"
        else:
            cwd = tmp_path / "missing"
        child = await spawn_clean(cmd, [], SpawnOptions(cwd, {}))
        result = await child.exited
        assert child.pid == -1
        assert child.process is None
        assert result.code is None
        assert result.signal is None
        assert result.error is not None and expected in result.error
        assert not result.timed_out
        assert await child.stdout.read() == b""
        child.stdin.write("ignored")
        child.stdin.end()
        await child.stdin.drain()
        await child.stdin.wait_closed()
        child.kill()

    asyncio.run(check())
