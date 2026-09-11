from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from wise_engine.host_setup import (
    HOSTS,
    SetupError,
    apply_plan,
    config_path,
    doctor,
    location,
    merge_config,
    plan_setup,
    rollback,
)
from wise_engine.launcher import LaunchError, resolve_root


def installation(path: Path, version: str = "1") -> Path:
    (path / ".claude-plugin").mkdir(parents=True)
    (path / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "wise", "version": version})
    )
    (path / "engine").mkdir()
    (path / "engine/engine.sh").write_text(f'#!/bin/sh\nprintf "%s\\n" "{version}" "$@"\n')
    return path


@pytest.mark.parametrize("host", HOSTS)
def test_preview_apply_idempotent_and_rollback(tmp_path: Path, host: str) -> None:
    root = installation(tmp_path / "cache é space" / "1")
    home = tmp_path / "home space"
    config = config_path(host, home)
    config.parent.mkdir(parents=True)
    before = (
        b'# keep this comment\ntheme = "dark"\n[mcp_servers.other]\ncommand = "other"\n'
        if host in ("codex", "grok")
        else b'{"theme":"dark", "mcpServers":{"other":{"command":"other"}}}\n'
    )
    config.write_bytes(before)
    plan = plan_setup(plugin_root=root, host=host, home=home)
    assert not location(home).exists()
    assert config.read_bytes() == before
    assert "other" not in json.dumps(plan.preview())
    transaction = apply_plan(plan)
    assert transaction and stat.S_IMODE(transaction.stat().st_mode) == 0o600
    report = doctor(home=home, host=host)
    assert report["registration_ok"] and report["launcher_ok"] and not report["host_verified"]
    doc = (
        tomllib.loads(config.read_text())
        if host in ("codex", "grok")
        else json.loads(config.read_text())
    )
    assert doc["theme"] == "dark"
    assert doc["mcp_servers" if host in ("codex", "grok") else "mcpServers"]["other"] == {
        "command": "other"
    }
    if host in ("codex", "grok"):
        assert "# keep this comment" in config.read_text()
    assert apply_plan(plan_setup(plugin_root=root, host=host, home=home)) is None
    rollback(transaction)
    assert config.read_bytes() == before
    assert not (location(home) / "bin/wise-engine").exists()


def test_launch_spaces_unicode_arbitrary_cwd_and_readonly_install(tmp_path: Path) -> None:
    root = installation(tmp_path / 'cache " $ test é' / "1")
    for child in root.rglob("*"):
        child.chmod(0o555 if child.is_dir() else 0o444)
    root.chmod(0o555)
    home = tmp_path / "home unicode ü"
    apply_plan(plan_setup(plugin_root=root, host="codex", home=home))
    launcher = location(home) / "bin/wise-engine"
    env = {"HOME": str(home), "PATH": os.environ["PATH"], "WISE_PYTHON": sys.executable}
    result = subprocess.run(
        [str(launcher), "--wise-host", "codex", "one space", "$(false)", "é"],
        env=env,
        cwd="/tmp",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["1", "one space", "$(false)", "é"]
    result = subprocess.run(
        [str(launcher), "install-root"], env=env, capture_output=True, text=True
    )
    assert result.stdout.strip() == str(root)


def test_symlink_upgrade_and_explicit_refresh_after_relocation(tmp_path: Path) -> None:
    first = installation(tmp_path / "cache/1")
    second = installation(tmp_path / "cache/2", "2")
    active = tmp_path / "active"
    active.symlink_to(first, target_is_directory=True)
    home = tmp_path / "home"
    apply_plan(plan_setup(plugin_root=active, host="cursor", home=home))
    registry = location(home) / "installations.json"
    active.unlink()
    active.symlink_to(second, target_is_directory=True)
    assert resolve_root(json.loads(registry.read_text()), "cursor").resolve() == second
    active.unlink()
    with pytest.raises(LaunchError, match="refresh"):
        resolve_root(json.loads(registry.read_text()), "cursor")
    moved = tmp_path / "moved"
    second.rename(moved)
    apply_plan(plan_setup(plugin_root=moved, host="cursor", home=home))
    assert resolve_root(json.loads(registry.read_text()), "cursor") == moved


def test_active_metadata_follows_exact_scope_not_newest_cache(tmp_path: Path) -> None:
    first = installation(tmp_path / "cache/1")
    second = installation(tmp_path / "cache/2", "2")
    installation(tmp_path / "cache/99-inactive", "99")
    metadata = tmp_path / "installed_plugins.json"
    key = "wise@wise-claude"

    def update(path: Path) -> None:
        metadata.write_text(
            json.dumps(
                {
                    "version": 2,
                    "plugins": {
                        key: [
                            {"scope": "user", "installPath": str(path)},
                            {
                                "scope": "project",
                                "projectPath": "/other",
                                "installPath": "/unselected",
                            },
                        ]
                    },
                }
            )
        )

    update(first)
    home = tmp_path / "home"
    source = {"path": str(metadata), "key": key, "scope": "user"}
    apply_plan(plan_setup(plugin_root=first, host="claude", home=home, source=source))
    registry = json.loads((location(home) / "installations.json").read_text())
    assert resolve_root(registry, "claude") == first
    update(second)
    assert resolve_root(registry, "claude") == second
    metadata.write_text(json.dumps({"plugins": {key: []}}))
    with pytest.raises(LaunchError, match="metadata changed"):
        resolve_root(registry, "claude")


def test_host_bindings_remain_separate(tmp_path: Path) -> None:
    first = installation(tmp_path / "claude install")
    second = installation(tmp_path / "codex install")
    home = tmp_path / "home"
    apply_plan(plan_setup(plugin_root=first, host="claude", home=home))
    apply_plan(plan_setup(plugin_root=second, host="codex", home=home))
    registry = json.loads((location(home) / "installations.json").read_text())
    assert resolve_root(registry, "claude") == first
    assert resolve_root(registry, "codex") == second
    assert resolve_root(registry) == second


def test_preview_and_rollback_detect_edits(tmp_path: Path) -> None:
    root = installation(tmp_path / "plugin")
    home = tmp_path / "home"
    plan = plan_setup(plugin_root=root, host="codex", home=home)
    config = config_path("codex", home)
    config.parent.mkdir(parents=True)
    config.write_text('theme="dark"\n')
    with pytest.raises(SetupError, match="after preview"):
        apply_plan(plan)
    transaction = apply_plan(plan_setup(plugin_root=root, host="codex", home=home))
    config.write_text(config.read_text() + "# user edit\n")
    with pytest.raises(SetupError, match="later changes"):
        rollback(transaction)
    assert "# user edit" in config.read_text()


def test_existing_config_symlink_requires_explicit_target(tmp_path: Path) -> None:
    root = installation(tmp_path / "plugin")
    home = tmp_path / "home"
    target = tmp_path / "actual config"
    target.write_text("{}")
    config = config_path("claude", home)
    home.mkdir()
    config.symlink_to(target)
    with pytest.raises(SetupError, match="symlink"):
        plan_setup(plugin_root=root, host="claude", home=home)
    apply_plan(plan_setup(plugin_root=root, host="claude", home=home, config=str(target)))
    assert config.is_symlink()


@pytest.mark.parametrize("host", HOSTS)
def test_invalid_config_is_not_replaced(host: str) -> None:
    with pytest.raises(SetupError):
        merge_config(host, b"bad }", {"command": "fixture"})


def test_toml_multiline_and_quoted_tables_preserved() -> None:
    raw = b'''# unrelated
text = """literal [mcp_servers.wise-engine] stays"""
[mcp_servers."a.b"]
command = "untouched" # retain
[mcp_servers.wise-engine]
command = "old"
startup_timeout_sec = 90
'''
    result = merge_config("codex", raw, {"command": "/space path", "args": ["mcp"]})
    assert b"# retain" in result and b"# unrelated" in result
    doc = tomllib.loads(result.decode())
    assert doc["mcp_servers"]["wise-engine"]["startup_timeout_sec"] == 90
    assert doc["mcp_servers"]["a.b"]["command"] == "untouched"


def test_apply_failure_restores_prior_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wise_engine.host_setup as module

    root = installation(tmp_path / "plugin")
    plan = plan_setup(plugin_root=root, host="codex", home=tmp_path / "home")
    write = module.atomic_write

    def fail(path: Path, content: bytes, mode: int) -> None:
        if path == plan.changes[-1].path:
            raise OSError("fixture write failure")
        write(path, content, mode)

    monkeypatch.setattr(module, "atomic_write", fail)
    with pytest.raises(OSError, match="fixture"):
        apply_plan(plan)
    assert all(not change.path.exists() for change in plan.changes)


def test_jsonc_keeps_unrelated_bytes_and_string_comment_tokens() -> None:
    raw = b"""{
  // important setting
  "url": "https://example.test/with/*literal*/", 
  "mcpServers": {
    "other": {"command": "keep",}, // keep server
    "wise-engine": {"command": "old", "env": {"FLAG": "retained"}},
  },
}
"""
    result = merge_config("cursor", raw, {"command": "/new", "args": ["mcp"]})
    assert b"// important setting" in result
    assert b'"other": {"command": "keep",}, // keep server' in result
    assert b"https://example.test/with/*literal*/" in result
    assert b'"FLAG": "retained"' in result
    assert merge_config("cursor", result, {"command": "/new", "args": ["mcp"]}) == result


def test_json_insertion_keeps_existing_formatting() -> None:
    raw = b'{/* keep */ "setting" : [1,2,], }'
    result = merge_config("cursor", raw, {"command": "/new"})
    assert b'/* keep */ "setting" : [1,2,], ' in result


def test_setup_cli_preview_apply_doctor_and_rollback(tmp_path: Path) -> None:
    import asyncio
    from wise_engine.cli import Io, main

    root = installation(tmp_path / "plugin")
    home = tmp_path / "home"
    output: list[str] = []
    errors: list[str] = []
    io = Io(output.append, errors.append, {"HOME": str(home)})
    args = ["setup-host", "--host", "grok", "--plugin-root", str(root)]
    assert asyncio.run(main(args, io)) == 0
    assert not home.exists()
    assert asyncio.run(main([*args, "--apply"], io)) == 0
    transaction = json.loads(output[-1])["transaction"]
    assert asyncio.run(main(["host-doctor", "--host", "grok"], io)) == 0
    assert not json.loads(output[-1])["host_verified"]
    assert asyncio.run(main(["host-rollback", transaction], io)) == 0
    assert not errors


@pytest.mark.parametrize(
    "host,variable,filename",
    [
        ("codex", "CODEX_HOME", "config.toml"),
        ("claude", "CLAUDE_CONFIG_DIR", ".claude.json"),
    ],
)
def test_active_profile_env_only_applies_to_current_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    variable: str,
    filename: str,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(variable, str(tmp_path / "profile"))
    assert config_path(host, Path.home()) == tmp_path / "profile" / filename
    other = tmp_path / "other"
    assert config_path(host, other).is_relative_to(other)
    assert config_path(host, Path.home(), str(tmp_path / "override")) == tmp_path / "override"


def test_launcher_exports_selected_host_and_replaces_stale_root_override(tmp_path: Path) -> None:
    root = installation(tmp_path / "plugin")
    (root / "engine/engine.sh").write_text('printf "%s\\n" "$WISE_HOST" "$WISE_PYTHON"\n')
    home = tmp_path / "home"
    config = config_path("cursor", home)
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "wise-engine": {
                        "env": {
                            "WISE_PLUGIN_ROOT": "/retired/cache",
                            "WISE_HOST": "claude",
                            "KEEP": "yes",
                        }
                    }
                }
            }
        )
    )
    plan = plan_setup(plugin_root=root, host="cursor", home=home)
    apply_plan(plan)
    entry = json.loads(config.read_text())["mcpServers"]["wise-engine"]
    assert "WISE_PLUGIN_ROOT" not in entry["env"] and entry["env"]["KEEP"] == "yes"
    answer = subprocess.run(
        [entry["command"], *entry["args"]],
        env={"PATH": os.environ["PATH"], "HOME": str(home), **entry["env"]},
        capture_output=True,
        text=True,
    )
    assert answer.returncode == 0 and answer.stdout.splitlines() == [
        "cursor",
        entry["env"]["WISE_PYTHON"],
    ]
    direct = subprocess.run(
        [entry["command"], "--wise-host", "cursor", "mcp"],
        env={"PATH": os.environ["PATH"], "HOME": str(home), "WISE_HOST": "claude"},
        capture_output=True,
        text=True,
    )
    assert direct.returncode == 0 and direct.stdout.splitlines()[0] == "cursor"


def test_setup_repairs_nonexecutable_launcher_and_rollback_restores_mode(tmp_path: Path) -> None:
    root = installation(tmp_path / "plugin")
    home = tmp_path / "home"
    apply_plan(plan_setup(plugin_root=root, host="codex", home=home))
    launcher = location(home) / "bin/wise-engine"
    launcher.chmod(0o600)
    plan = plan_setup(plugin_root=root, host="codex", home=home)
    assert any(change.preview()["changed"] for change in plan.changes)
    transaction = apply_plan(plan)
    assert transaction and os.access(launcher, os.X_OK)
    rollback(transaction)
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o600


def test_automatic_upgrade_preserves_other_hosts_settings_and_history(tmp_path: Path) -> None:
    from wise_engine.host_setup import refresh_existing

    first = installation(tmp_path / "cache/1")
    second = installation(tmp_path / "cache/2", "2")
    home = tmp_path / "home"
    custom = home / "custom-codex.toml"
    apply_plan(plan_setup(plugin_root=first, host="codex", home=home, config=str(custom)))
    apply_plan(plan_setup(plugin_root=first, host="cursor", home=home))
    custom.write_text('# user comment\ntheme = "dark"\n' + custom.read_text())
    history = location(home) / "runs/saved/state.json"
    history.parent.mkdir(parents=True)
    history.write_text('{"status":"gated"}\n')
    upgraded = refresh_existing(plugin_root=second, host="codex", home=home)
    assert upgraded["refreshed"] and upgraded["transaction"]
    registry = json.loads((location(home) / "installations.json").read_text())
    assert registry["default_host"] == "cursor"
    assert registry["hosts"]["cursor"]["plugin_root"] == str(first)
    assert registry["hosts"]["codex"]["plugin_root"] == str(second)
    assert "# user comment" in custom.read_text()
    assert history.read_text() == '{"status":"gated"}\n'
    assert not refresh_existing(plugin_root=second, host="codex", home=home)["refreshed"]
    rollback(upgraded["transaction"])
    assert (
        resolve_root(json.loads((location(home) / "installations.json").read_text()), "codex")
        == first
    )


def test_automatic_upgrade_refuses_modified_server(tmp_path: Path) -> None:
    from wise_engine.host_setup import refresh_existing

    first = installation(tmp_path / "1")
    second = installation(tmp_path / "2", "2")
    home = tmp_path / "home"
    with pytest.raises(SetupError, match="explicit setup"):
        refresh_existing(plugin_root=first, host="cursor", home=home)
    apply_plan(plan_setup(plugin_root=first, host="cursor", home=home))
    config = config_path("cursor", home)
    config.write_text(config.read_text().replace('"mcp"', '"different"'))
    before = config.read_bytes()
    with pytest.raises(SetupError, match="explicit setup"):
        refresh_existing(plugin_root=second, host="cursor", home=home)
    assert config.read_bytes() == before


def test_host_interpreter_selection_survives_other_host_interpreter_removal(tmp_path: Path) -> None:
    root = installation(tmp_path / "plugin")
    home = tmp_path / "home"
    first = tmp_path / "python-first"
    second = tmp_path / "python-second"
    first.symlink_to(sys.executable)
    second.symlink_to(sys.executable)
    apply_plan(plan_setup(plugin_root=root, host="codex", home=home, python=str(first)))
    apply_plan(plan_setup(plugin_root=root, host="cursor", home=home, python=str(second)))
    second.unlink()
    result = subprocess.run(
        [str(location(home) / "bin/wise-engine"), "--wise-host", "codex", "install-root"],
        env={"HOME": str(home), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and result.stdout.strip() == str(root)


def test_concurrent_apply_rechecks_preview_after_lock(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    root = installation(tmp_path / "plugin")
    home = tmp_path / "home"
    plans = [plan_setup(plugin_root=root, host=host, home=home) for host in ("codex", "cursor")]

    def apply(plan):
        try:
            return apply_plan(plan)
        except SetupError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, plans))
    assert sum(result is not None for result in results) == 1
    registry = json.loads((location(home) / "installations.json").read_text())
    assert len(registry["hosts"]) == 1
    assert doctor(home=home, host=registry["default_host"])["registration_ok"]


@pytest.mark.parametrize("missing", ["launcher.py", "python"])
def test_doctor_rejects_incomplete_launch_chain(tmp_path: Path, missing: str) -> None:
    root = installation(tmp_path / "plugin")
    home = tmp_path / "home"
    interpreter = tmp_path / "python"
    interpreter.symlink_to(sys.executable)
    apply_plan(plan_setup(plugin_root=root, host="codex", home=home, python=str(interpreter)))
    assert doctor(home=home, host="codex")["launcher_ok"]
    (interpreter if missing == "python" else location(home) / missing).unlink()
    report = doctor(home=home, host="codex")
    assert report["registration_ok"]
    assert not report["launcher_ok"]
