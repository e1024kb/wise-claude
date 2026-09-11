from pathlib import Path

from unittest.mock import patch

import pytest

from wise_engine.bootstrap import (
    BootstrapError,
    ensure_environment,
    environment_key,
    environment_path,
)
from wise_engine.paths import cwd_slug, plugin_data_root, wise_data_root


def test_probe_does_not_create_data(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("")
    data = tmp_path / "absent"
    with pytest.raises(BootstrapError, match="not installed"):
        ensure_environment(requirements, data, probe=True)
    assert not data.exists()


def test_failed_install_can_retry_without_publishing_environment(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("")
    data = tmp_path / "data"
    target = environment_path(requirements, data)

    def create(path):
        (path / "bin").mkdir(parents=True)
        (path / "bin/python").touch(mode=0o700)

    with patch("wise_engine.bootstrap.venv.EnvBuilder.create", side_effect=create):
        with patch("wise_engine.bootstrap._run_installer") as run:
            run.side_effect = BootstrapError("Dependency installation failed (exit 1)")
            with pytest.raises(BootstrapError, match="exit 1"):
                ensure_environment(requirements, data)
            assert not target.exists()
            run.side_effect = None
            result = ensure_environment(requirements, data)
            assert result == target / "bin/python"
            assert ensure_environment(requirements, data, probe=True) == result
            assert run.call_count == 3


def test_lock_content_change_invalidates_environment(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("first")
    before = environment_key(requirements)
    requirements.write_text("second")
    assert environment_key(requirements) != before


def test_symlink_environment_is_not_deleted(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("")
    data = tmp_path / "data"
    target = environment_path(requirements, data)
    target.parent.mkdir(parents=True)
    victim = tmp_path / "existing"
    victim.mkdir()
    target.symlink_to(victim)
    with pytest.raises(BootstrapError, match="symlink"):
        ensure_environment(requirements, data)
    assert victim.is_dir()
    assert target.is_symlink()


def test_roots_match_current_precedence(tmp_path):
    env = {"HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "xdg")}
    assert wise_data_root(env) == tmp_path / "xdg/wise"
    env["WISE_DATA_DIR"] = str(tmp_path / "wise")
    assert plugin_data_root(env) == tmp_path / "wise"
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "plugin")
    assert plugin_data_root(env) == tmp_path / "plugin"
    assert cwd_slug(tmp_path) == str(tmp_path.resolve()).replace("/", "-")


def test_concurrent_startup_publishes_one_complete_environment(tmp_path):
    import concurrent.futures
    import subprocess
    import sys

    requirements = tmp_path / "requirements.txt"
    requirements.write_text("")
    data = tmp_path / "data"
    code = (
        "from pathlib import Path; from wise_engine.bootstrap import ensure_environment; "
        "import sys; print(ensure_environment(Path(sys.argv[1]), Path(sys.argv[2])))"
    )

    def start():
        return subprocess.run(
            [sys.executable, "-c", code, str(requirements), str(data)],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(), range(2)))
    assert results[0].stdout == results[1].stdout
    assert sum("installing Python dependencies" in r.stderr for r in results) == 1
    interpreter = results[0].stdout.strip()
    check = subprocess.run(
        [interpreter, "-c", "import sys; print(sys.prefix)"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert str(environment_path(requirements, data)) == check.stdout.strip()


def test_termination_reaps_installer_before_retry(tmp_path):
    import os
    import subprocess
    import sys
    import time

    requirements = tmp_path / "requirements.txt"
    requirements.write_text("")
    data = tmp_path / "data"
    pid_file = tmp_path / "installer.pid"
    code = """
import sys
from pathlib import Path
import wise_engine.bootstrap as bootstrap
requirements, data, pid_file = map(Path, sys.argv[1:])
def create(target):
    (target / 'bin').mkdir(parents=True)
    child = target / 'bin/python'
    child.write_text('#!' + sys.executable + '\\nimport os,time\\n'
                     + 'from pathlib import Path\\n'
                     + 'Path(' + repr(str(pid_file)) + ').write_text(str(os.getpid()))\\n'
                     + 'time.sleep(60)\\n')
    child.chmod(0o700)
bootstrap.venv.EnvBuilder.create = lambda self, target: create(target)
bootstrap.ensure_environment(requirements, data)
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", code, str(requirements), str(data), str(pid_file)],
        cwd=Path(__file__).parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.exists()
        child_pid = int(pid_file.read_text())
        parent.terminate()
        _, stderr = parent.communicate(timeout=10)
        assert parent.returncode != 0
        assert "interrupted" in stderr
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        assert not environment_path(requirements, data).exists()
        assert ensure_environment(requirements, data).exists()
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()


def test_bootstrap_script_arguments_and_prepare_share_environment(tmp_path, monkeypatch, capsys):
    from wise_engine import bootstrap

    interpreter = tmp_path / "managed/bin/python"
    script = tmp_path / "catalog with spaces.py"
    calls = []
    monkeypatch.setattr(bootstrap, "ensure_environment", lambda *args, **kwargs: interpreter)
    monkeypatch.setattr(bootstrap.os, "execve", lambda *args: calls.append(args))
    assert bootstrap.main(["--prepare"]) == 0
    assert capsys.readouterr().out == str(interpreter) + "\n"
    assert calls == []
    assert bootstrap.main(["--script", str(script), "--", "literal $(no-shell)", "--text"]) == 0
    assert calls[0][0] == str(interpreter)
    assert calls[0][1] == [str(interpreter), str(script), "literal $(no-shell)", "--text"]
    assert calls[0][2]["PYTHONPATH"] == str(bootstrap.ENGINE_ROOT)
    assert calls[0][2]["WISE_ENGINE_BASE_PYTHON"] == bootstrap.sys.executable
