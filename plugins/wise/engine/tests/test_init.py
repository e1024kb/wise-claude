import importlib.util
import json
import shlex
import shutil
import subprocess
import sys

import pytest

from wise_engine import bootstrap
from wise_engine.paths import ENGINE_ROOT, PLUGIN_ROOT


def module_from(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def installation(tmp_path):
    plugin = tmp_path / "plugin with spaces"
    scripts = plugin / "scripts"
    package = plugin / "engine/wise_engine"
    scripts.mkdir(parents=True)
    package.mkdir(parents=True)
    for name in ("init.sh", "bootstrap-deps.sh", "init-registry.py", "engine.sh", "engine.py"):
        shutil.copyfile(PLUGIN_ROOT / "scripts" / name, scripts / name)
    for name in ("__init__.py", "bootstrap.py", "paths.py", "yaml_compat.py"):
        shutil.copyfile(ENGINE_ROOT / "wise_engine" / name, package / name)
    (plugin / ".claude-plugin").mkdir()
    (plugin / ".claude-plugin/plugin.json").write_text('{"version":"5.0.0"}')
    requirements = plugin / "engine/requirements.txt"
    requirements.write_text("")
    data = tmp_path / "data"
    target = bootstrap.environment_path(requirements, data)
    binary = target / "bin/python"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    binary.chmod(0o755)
    (target / ".ready.json").write_text(
        json.dumps({"key": bootstrap.environment_key(requirements)})
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("gh", "node", "bun", "claude", "codex", "cursor-agent", "gemini", "grok"):
        path = bindir / name
        path.write_text("#!/bin/sh\necho optional-tool-must-not-run >&2\nexit 99\n")
        path.chmod(0o755)
    env = dict(
        HOME=str(tmp_path),
        PATH=f"{bindir}:/usr/bin:/bin",
        WISE_PYTHON=sys.executable,
        CLAUDE_PLUGIN_ROOT=str(plugin),
        CLAUDE_PLUGIN_DATA=str(data),
        PIP_NO_INDEX="1",
    )
    return dict(
        plugin=plugin,
        scripts=scripts,
        requirements=requirements,
        target=target,
        binary=binary,
        data=data,
        env=env,
    )


def call(installation, script, *args, env=None):
    interpreter = sys.executable if script.endswith(".py") else "/bin/bash"
    return subprocess.run(
        [interpreter, str(installation["scripts"] / script), *args],
        env=installation["env"] | (env or {}),
        capture_output=True,
        text=True,
        timeout=10,
    )


def registry(installation):
    module = module_from(installation["scripts"] / "init-registry.py", "test_init_registry")
    module.PLUGIN_ROOT = installation["plugin"]
    module.REGISTRY_PATH = installation["plugin"] / ".wise-init-registry.yaml"
    return module


def test_python_and_engine_probes_are_read_only(installation):
    before = set(installation["plugin"].rglob("*"))
    python = call(installation, "init.sh", "probe-python")
    assert python.returncode == 0 and "STATUS=ok" in python.stdout
    assert "MODULE_" not in python.stdout
    engine = call(installation, "init.sh", "probe-engine")
    assert engine.returncode == 0 and "STATUS=ok" in engine.stdout
    assert str(installation["binary"]) in engine.stdout
    probe = call(installation, "bootstrap-deps.sh", "--probe")
    assert probe.returncode == 0 and probe.stdout == f"READY:{installation['binary']}\n"
    assert "optional-tool" not in probe.stderr
    assert set(installation["plugin"].rglob("*")) == before
    assert not (installation["plugin"] / ".wise-init-registry.yaml").exists()


def test_absent_environment_probe_does_not_install(installation):
    shutil.rmtree(installation["data"])
    result = call(installation, "bootstrap-deps.sh", "--probe")
    assert result.returncode == 3 and result.stdout == "BOOTSTRAP:missing-engine\n"
    assert not installation["data"].exists()
    assert not (installation["plugin"] / ".wise-init-registry.yaml").exists()


@pytest.mark.parametrize("old", [False, True])
def test_missing_or_old_python_reports_requirement(installation, old):
    binary = installation["scripts"] / "old-python"
    if old:
        binary.write_text("#!/bin/sh\necho 3.10.9\nexit 1\n")
        binary.chmod(0o755)
    result = call(installation, "bootstrap-deps.sh", env={"WISE_PYTHON": str(binary)})
    assert result.returncode == 2 and "BOOTSTRAP:need-python" in result.stdout
    assert "3.11" in result.stdout
    assert "optional-tool" not in result.stderr


def test_runtime_refresh_preserves_optional_skips_and_checks_requirements(installation):
    reg = registry(installation)
    reg.save_registry(
        dict(
            version=1,
            deps=dict(
                python={"status": "ok", "modules": {"yaml": "ok"}},
                node={"status": "ok"},
                gh={"status": "missing", "skipped": True},
                mcp={"status": "partial", "skipped": True, "failed": ["optional-server"]},
            ),
        )
    )
    result = call(installation, "bootstrap-deps.sh")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"READY:{installation['binary']}\n"
    data = reg.load_registry()
    assert data["version"] == 2 and data["deps"]["engine"]["runtime"] == "python"
    assert "node" not in data["deps"]
    assert data["deps"]["gh"] == {"status": "missing", "skipped": True}
    assert data["deps"]["mcp"]["failed"] == ["optional-server"]
    assert data["deps"]["mcp"]["skipped"] is True
    assert call(installation, "init-registry.py", "check").stdout == "INIT:ok\n"
    installation["requirements"].write_text("# new lock fingerprint\n")
    invalid = call(installation, "init-registry.py", "check")
    assert invalid.returncode == 2 and invalid.stdout == "INIT:stale:engine\n"
    assert reg.load_registry()["deps"]["gh"]["skipped"] is True


def test_registry_check_rejects_legacy_runtime_and_missing_environment(installation):
    reg = registry(installation)
    reg.save_registry(dict(version=1, deps={"python": {"status": "ok"}}))
    assert call(installation, "init-registry.py", "check").stdout == "INIT:stale:runtime-schema\n"
    assert call(installation, "bootstrap-deps.sh").returncode == 0
    installation["binary"].unlink()
    assert call(installation, "init-registry.py", "check").stdout == "INIT:stale:engine\n"


def test_registry_json_fast_path_has_no_site_package_dependency(installation):
    assert call(installation, "bootstrap-deps.sh").returncode == 0
    result = subprocess.run(
        [sys.executable, "-S", str(installation["scripts"] / "init-registry.py"), "check"],
        env=installation["env"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0 and result.stdout == "INIT:ok\n"


def test_legacy_yaml_optional_decisions_survive_runtime_refresh(installation):
    reg = registry(installation)
    reg.REGISTRY_PATH.write_text(
        "version: 1\ndeps:\n  markitdown: {status: missing, skipped: true}\n  gh: {status: missing, skipped: true}\n"
    )
    result = call(installation, "bootstrap-deps.sh")
    assert result.returncode == 0, result.stderr
    data = json.loads(reg.REGISTRY_PATH.read_text())
    assert data["deps"]["markitdown"] == {"status": "missing", "skipped": True}
    assert data["deps"]["gh"]["skipped"] is True


def test_save_registry_atomic_replace_and_temporary_cleanup(installation, monkeypatch):
    reg = registry(installation)
    reg.save_registry({"a": 1})
    reg.save_registry({"a": 2})
    assert reg.load_registry() == {"a": 2}
    assert list(reg.REGISTRY_PATH.parent.glob("*.tmp")) == []
    monkeypatch.setattr(
        reg.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("replace failed"))
    )
    with pytest.raises(OSError, match="replace failed"):
        reg.save_registry({"a": 3})
    assert reg.load_registry() == {"a": 2}
    assert list(reg.REGISTRY_PATH.parent.glob("*.tmp")) == []


def test_optional_write_merges_and_unreadable_registry_is_not_erased(installation):
    reg = registry(installation)
    assert call(installation, "bootstrap-deps.sh").returncode == 0
    assert (
        call(
            installation,
            "init-registry.py",
            "write",
            json.dumps({"deps": {"gh": {"status": "missing", "skipped": True}}}),
        ).returncode
        == 0
    )
    assert reg.load_registry()["deps"]["engine"]["status"] == "ok"
    assert call(installation, "init-registry.py", "check").returncode == 0
    reg.REGISTRY_PATH.write_text("invalid: [yaml")
    result = call(installation, "init-registry.py", "write", '{"deps":{}}')
    assert result.returncode == 1
    assert "optional setup decisions are preserved" in result.stderr
    assert reg.REGISTRY_PATH.read_text() == "invalid: [yaml"


def test_catalog_launcher_uses_managed_yaml_and_preserves_contract(installation):
    skills = installation["plugin"] / "skills"
    (skills / "action").mkdir(parents=True)
    (skills / "action/SKILL.md").write_text(
        '---\nname: action\ndescription: "A useful action."\nargument-hint: "[path]"\n---\n'
    )
    (skills / "reference").mkdir()
    (skills / "reference/SKILL.md").write_text(
        '---\nname: reference\ndescription: "Some guidance."\n---\n'
    )
    (skills / "malformed").mkdir()
    (skills / "malformed/SKILL.md").write_text("---\nname: [broken\n---\n")
    result = call(installation, "engine.sh", "list-skills")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == dict(
        standalone=[
            dict(
                name="action",
                plugin="wise",
                description="A useful action.",
                **{"argument-hint": "[path]"},
            )
        ],
        reference=[dict(name="reference", plugin="wise", description="Some guidance.")],
        siblings_installed={},
    )
    assert not (installation["plugin"] / "engine/node_modules").exists()
    assert "optional-tool" not in result.stderr
