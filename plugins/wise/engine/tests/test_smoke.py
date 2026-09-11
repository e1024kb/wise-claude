import json
import os
import re
import subprocess
import sys

from wise_engine.paths import ENGINE_ROOT, PLUGIN_ROOT
from wise_engine.version import plugin_version, runtime_version, source_build_id


def invoke(tmp_path, *args, extra_env=None):
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ENGINE_ROOT),
        "WISE_DATA_DIR": str(tmp_path / "data"),
    }
    return subprocess.run(
        [sys.executable, "-m", "wise_engine", *args],
        cwd=tmp_path,
        env=env | (extra_env or {}),
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_plugin_version_and_python_source_fingerprint():
    assert (
        plugin_version()
        == json.loads((PLUGIN_ROOT / ".claude-plugin/plugin.json").read_text())["version"]
    )
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?", plugin_version())
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?\+py\.[0-9a-f]{10}", source_build_id())
    assert source_build_id() == source_build_id()
    assert runtime_version() == sys.version.split()[0]


def test_python_entrypoint_version_and_unknown_exit(tmp_path):
    version = invoke(tmp_path, "version")
    assert version.returncode == 0 and version.stderr == ""
    assert re.fullmatch(
        r"wise-engine \d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?\+py\.[0-9a-f]{10} \(python [^)]+\)\n",
        version.stdout,
    )
    unknown = invoke(tmp_path, "bogus")
    assert unknown.returncode == 64
    assert "unknown command" in unknown.stderr


def test_python_cli_compiles_bundled_definitions_and_lists_models(tmp_path):
    definitions = sorted(str(path) for path in (PLUGIN_ROOT / "workflows").glob("*/workflow.yaml"))
    compiled = invoke(tmp_path, "compile-check", *definitions)
    assert compiled.returncode == 0, compiled.stderr
    assert len(json.loads(compiled.stdout)) == len(definitions)
    assert all(row["ok"] for row in json.loads(compiled.stdout))
    models = invoke(tmp_path, "models", "codex", "--text")
    assert models.returncode == 0
    assert "codex\tgpt-6-astra\t" in models.stdout


def test_python_cli_dispatch_uses_fake_provider_executable(tmp_path):
    binary = tmp_path / "codex"
    binary.write_text(
        f"#!{sys.executable}\n"
        + """import json, sys
assert sys.argv[1] == "exec"
assert sys.stdin.read() == ""
print(json.dumps(dict(type="thread.started", thread_id="smoke")))
print(json.dumps(dict(type="item.completed", item=dict(type="agent_message", text="fake provider completed"))))
print(json.dumps(dict(type="turn.completed", usage=dict(input_tokens=1, output_tokens=2))))
"""
    )
    binary.chmod(0o755)
    result = invoke(
        tmp_path,
        "dispatch",
        "--harness",
        "codex",
        "--prompt",
        "test prompt",
        extra_env={"PATH": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["ok"] and value["harness"] == "codex"
    assert value["text"] == "fake provider completed"
    assert value["usage"]["input"] == 1
    assert not (tmp_path / "data").exists()
