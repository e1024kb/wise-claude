import asyncio
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from wise_engine.adapters.grok import prepare_mcp_overlay, start_grok


def request(tmp_path, **extra):
    return dict(
        prompt="deterministic test",
        cwd=str(tmp_path),
        mode="approval-required",
        auth="subscription",
        timeout_ms=2000,
        mcp_config={
            "mcpServers": {
                "wise-engine": {
                    "command": sys.executable,
                    "args": ["-m", "wise_engine", "unit-mcp"],
                    "env": {
                        "WISE_STEP_TOKEN": "synthetic-step-token",
                        "WISE_ENGINE_SOCKET": "/tmp/synthetic.sock",
                    },
                }
            }
        },
        **extra,
    )


def provider(tmp_path):
    source = tmp_path / "grok source"
    source.mkdir()
    (source / "config.toml").write_text("""# user settings
[models]
default = "user-model"
[permission]
rules = [{action="deny", tool="bash", pattern="rm *"}]
[mcp_servers.parent]
command = "parent-server"
""")
    (source / "auth.json").write_text('{"credential":"synthetic-old"}')
    (source / "sessions").mkdir()
    (source / "sessions/old-session").write_text("retained history")
    return source, {"HOME": str(tmp_path), "GROK_HOME": str(source), "PATH": os.environ["PATH"]}


def test_overlay_preserves_config_auth_sessions_and_grants_only_child_tools(tmp_path):
    source, env = provider(tmp_path)
    before = (source / "config.toml").read_bytes()
    overlay = prepare_mcp_overlay(request(tmp_path), env)
    try:
        config = tomllib.loads(Path(overlay.config.path).read_text())
        alias = next(name for name in config["mcp_servers"] if name != "parent")
        assert config["models"] == {"default": "user-model"}
        assert config["permission"]["rules"][0]["action"] == "deny"
        assert config["mcp_servers"]["parent"]["command"] == "parent-server"
        placeholder = config["mcp_servers"][alias]["env"]["WISE_STEP_TOKEN"]
        assert placeholder.startswith("${WISE_GROK_MCP_") and placeholder.endswith("}")
        assert overlay.env[placeholder[2:-1]] == "synthetic-step-token"
        assert "synthetic-step-token" not in Path(overlay.config.path).read_text()
        assert "synthetic-step-token" not in " ".join(overlay.allowed_tools)
        assert overlay.allowed_tools == [
            f"MCPTool({alias}__{name})"
            for name in ("wise_report", "wise_ask", "wise_context", "wise_checkpoint")
        ]
        assert Path(overlay.config.path).stat().st_mode & 0o777 == 0o600
        directory = Path(overlay.env["GROK_HOME"])
        assert directory.stat().st_mode & 0o777 == 0o700
        assert overlay.env["GROK_AUTH_PATH"] == str(source / "auth.json")
        assert (directory / "sessions/old-session").read_text() == "retained history"
        refresh = source / "auth.refreshed"
        refresh.write_text('{"credential":"synthetic-refreshed"}')
        os.replace(refresh, overlay.env["GROK_AUTH_PATH"])
        (directory / "sessions/new-session").write_text("new history")
    finally:
        overlay.cleanup()
    assert not directory.exists()
    assert (source / "config.toml").read_bytes() == before
    assert json.loads((source / "auth.json").read_text())["credential"] == "synthetic-refreshed"
    assert (source / "sessions/new-session").read_text() == "new history"


def test_concurrent_overlays_have_distinct_aliases_and_independent_cleanup(tmp_path):
    _, env = provider(tmp_path)
    first = prepare_mcp_overlay(request(tmp_path), env)
    second = prepare_mcp_overlay(request(tmp_path), env)
    try:
        assert first.env["GROK_HOME"] != second.env["GROK_HOME"]
        assert set(first.allowed_tools).isdisjoint(second.allowed_tools)
        first_names = {key for key in first.env if key.startswith("WISE_GROK_MCP_")}
        second_names = {key for key in second.env if key.startswith("WISE_GROK_MCP_")}
        assert first_names and second_names and first_names.isdisjoint(second_names)
        first.cleanup()
        assert Path(second.config.path).exists()
    finally:
        first.cleanup()
        second.cleanup()


def test_engine_only_fails_before_writing_and_unrelated_mcp_is_not_approved(tmp_path):
    source, env = provider(tmp_path)
    before = set(source.rglob("*"))
    with pytest.raises(ValueError, match="engine-only MCP policy is unsupported"):
        prepare_mcp_overlay(request(tmp_path, mcp_policy="engine-only"), env)
    assert set(source.rglob("*")) == before
    req = request(tmp_path)
    req["mcp_config"] = {"mcpServers": {"unrelated": {"command": "/bin/false"}}}
    overlay = prepare_mcp_overlay(req, env)
    try:
        assert overlay.allowed_tools == []
    finally:
        overlay.cleanup()


@pytest.mark.parametrize("ending", ["success", "timeout", "kill", "cancel"])
def test_process_lifetime_cleans_private_config_and_keeps_resume(tmp_path, ending):
    async def scenario():
        source, env = provider(tmp_path)
        ready = tmp_path / "ready.json"
        executable = tmp_path / "fake-grok"
        executable.write_text(
            f"#!{sys.executable}\n"
            + """import json, os, sys, time
from pathlib import Path
root = Path(os.environ["GROK_HOME"])
assert "synthetic-step-token" not in " ".join(sys.argv)
assert "synthetic-step-token" not in (root / "config.toml").read_text()
assert "--resume" in sys.argv and sys.argv[sys.argv.index("--resume")+1] == "old-session"
assert (root / "sessions/old-session").read_text() == "retained history"
assert sys.argv[sys.argv.index("--permission-mode")+1] == "dontAsk"
assert "--always-approve" not in sys.argv
assert sys.argv[sys.argv.index("--leader-socket")+1] == str(root / "leader.sock")
Path(os.environ["READY_FILE"]).write_text(json.dumps({"root":str(root), "argv":sys.argv}))
if os.environ["ENDING"] != "success": time.sleep(20)
(root / "sessions/new-session").write_text("continued")
print(json.dumps({"type":"end", "text":"ok", "sessionId":"new-session"}), flush=True)
"""
        )
        executable.chmod(0o755)
        req = request(
            tmp_path, resume="old-session", env={"READY_FILE": str(ready), "ENDING": ending}
        )
        if ending == "timeout":
            req["timeout_ms"] = 150
        handle = await start_grok(req, lambda event: None, bin=str(executable), parent_env=env)
        async with asyncio.timeout(3):
            while not ready.exists():
                await asyncio.sleep(0.005)
        recorded = json.loads(ready.read_text())
        assert recorded["root"] != str(source)
        if ending == "kill":
            handle.kill("SIGTERM")
        if ending == "cancel":
            handle.done.cancel()
            with pytest.raises(asyncio.CancelledError):
                await handle.done
        else:
            result = await handle.done
            assert (
                result["exit"] == {"success": "ok", "timeout": "timeout", "kill": "error"}[ending]
            )
        assert not Path(recorded["root"]).exists()
        assert (source / "sessions/old-session").read_text() == "retained history"
        if ending == "success":
            assert (source / "sessions/new-session").read_text() == "continued"

    asyncio.run(scenario())


def test_native_grok_discovers_overlay_without_model_call(tmp_path):
    binary = shutil.which("grok")
    if not binary:
        pytest.skip("native Grok CLI is not installed")
    _, env = provider(tmp_path)
    overlay = prepare_mcp_overlay(request(tmp_path), env)
    try:
        result = subprocess.run(
            [binary, "--cwd", str(tmp_path), "inspect", "--json"],
            env={**overlay.env, "GROK_DISABLE_AUTOUPDATER": "1"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        found = json.loads(result.stdout)["mcpServers"]
        child = [item for item in found if item["name"].startswith("wise-child-")]
        assert len(child) == 1
        assert child[0]["target"] == sys.executable
        assert child[0]["source"]["path"] == overlay.config.path
        assert any(item["name"] == "parent" for item in found)
    finally:
        overlay.cleanup()


def test_native_grok_doctor_connects_scoped_stdio_server_without_model(tmp_path):
    binary = shutil.which("grok")
    if not binary:
        pytest.skip("native Grok CLI is not installed")
    _, env = provider(tmp_path)
    server = tmp_path / "server.py"
    marker = tmp_path / "server-requests.jsonl"
    server.write_text("""import json, os, sys
assert os.environ["WISE_STEP_TOKEN"] == "synthetic-step-token"
for line in sys.stdin:
    request = json.loads(line)
    with open(os.environ["RECEIPT"], "a") as receipt:
        receipt.write(json.dumps({"method":request["method"]})+"\\n")
    if "id" not in request: continue
    if request["method"] == "initialize":
        result = {"protocolVersion":request["params"]["protocolVersion"],"capabilities":{"tools":{}},"serverInfo":{"name":"deterministic-child","version":"1"}}
    elif request["method"] == "tools/list":
        result = {"tools":[{"name":name,"inputSchema":{"type":"object"}} for name in ("wise_report","wise_ask","wise_context","wise_checkpoint")]}
    else: result = {}
    print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result}),flush=True)
""")
    req = request(tmp_path)
    req["mcp_config"]["mcpServers"]["wise-engine"]["args"] = [str(server)]
    req["mcp_config"]["mcpServers"]["wise-engine"]["env"]["RECEIPT"] = str(marker)
    overlay = prepare_mcp_overlay(req, env)
    try:
        assert "synthetic-step-token" not in Path(overlay.config.path).read_text()
        alias = overlay.allowed_tools[0].removeprefix("MCPTool(").split("__")[0]
        result = subprocess.run(
            [binary, "--cwd", str(tmp_path), "mcp", "doctor", alias, "--json"],
            env={**overlay.env, "GROK_DISABLE_AUTOUPDATER": "1"},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr + result.stdout
        methods = [json.loads(line)["method"] for line in marker.read_text().splitlines()]
        assert "initialize" in methods and "tools/list" in methods
        assert "synthetic-step-token" not in result.stdout + result.stderr
    finally:
        overlay.cleanup()


def test_spawn_failure_removes_overlay(tmp_path, monkeypatch):
    from wise_engine.adapters import grok

    _, env = provider(tmp_path)
    observed = []
    prepare = grok.prepare_mcp_overlay

    def track(req, child_env):
        overlay = prepare(req, child_env)
        observed.append(overlay)
        return overlay

    async def failure(*args, **kwargs):
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(grok, "prepare_mcp_overlay", track)
    monkeypatch.setattr(grok, "spawn_clean", failure)
    with pytest.raises(OSError, match="synthetic spawn failure"):
        asyncio.run(grok.start_grok(request(tmp_path), lambda event: None, parent_env=env))
    assert len(observed) == 1 and not Path(observed[0].config.path).parent.exists()


def test_auth_path_override_survives_overlay_and_probe(tmp_path):
    from wise_engine.adapters.grok import child_env, probe_auth

    _, env = provider(tmp_path)
    credentials = tmp_path / "selected-auth.json"
    credentials.write_text('{"credential":"synthetic-profile"}')
    env["GROK_AUTH_PATH"] = str(credentials)
    req = request(tmp_path)
    overlay = prepare_mcp_overlay(req, child_env(req, env))
    try:
        assert overlay.env["GROK_AUTH_PATH"] == str(credentials)
        assert asyncio.run(probe_auth("subscription", parent_env=overlay.env))["ok"]
    finally:
        overlay.cleanup()
