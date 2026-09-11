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
        with patch("wise_engine.bootstrap.subprocess.run") as run:
            run.return_value.returncode = 1
            with pytest.raises(BootstrapError, match="exit 1"):
                ensure_environment(requirements, data)
            assert not target.exists()
            run.return_value.returncode = 0
            result = ensure_environment(requirements, data)
            assert result == target / "bin/python"
            assert ensure_environment(requirements, data, probe=True) == result
            assert run.call_count == 2


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
