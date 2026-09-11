from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from wise_engine.adapters.gemini import (
    GEMINI_SYSTEM_DEFAULTS,
    GEMINI_SYSTEM_SETTINGS,
    child_env,
    prepare_mcp,
    start_gemini,
)


def request(tmp_path, **changes):
    return dict(
        prompt="ping",
        model="inherit",
        cwd=str(tmp_path),
        mode="approval-required",
        timeout_ms=3000,
        auth="subscription",
        mcp_config={
            "mcpServers": {
                "wise-engine": {
                    "command": sys.executable,
                    "args": ["-m", "wise_engine", "unit-mcp"],
                    "env": {
                        "WISE_STEP_TOKEN": "private-token",
                        "WISE_ENGINE_SOCKET": "/tmp/socket $literal é",
                    },
                }
            }
        },
        **changes,
    )


def test_private_overlay_preserves_settings_defaults_and_environment(tmp_path):
    original = tmp_path / "system.json"
    original.write_text(
        '{// keep\n"security":{"disableYoloMode":true},"mcpServers":{"other":{"command":"other"}}}'
    )
    env = child_env(
        request(tmp_path),
        {GEMINI_SYSTEM_SETTINGS: str(original), "GEMINI_CLI_HOME": "/original/auth"},
    )
    before = original.read_bytes()
    first = prepare_mcp(request(tmp_path), env)
    assert first
    try:
        settings = json.loads(Path(first.path).read_text())
        assert settings["security"] == {"disableYoloMode": True}
        assert settings["mcpServers"]["other"] == {"command": "other"}
        alias = next(name for name in settings["mcpServers"] if name != "other")
        server = settings["mcpServers"][alias]
        assert server["trust"] is True
        assert "private-token" not in Path(first.path).read_text()
        assert {key: env[value[2:-1]] for key, value in server["env"].items()} == request(tmp_path)[
            "mcp_config"
        ]["mcpServers"]["wise-engine"]["env"]
        assert env["GEMINI_CLI_HOME"] == "/original/auth"
        assert env[GEMINI_SYSTEM_DEFAULTS] == str(tmp_path / "system-defaults.json")
        assert stat.S_IMODE(Path(first.path).stat().st_mode) == 0o600
        assert stat.S_IMODE(Path(first.path).parent.stat().st_mode) == 0o700
        second = prepare_mcp(request(tmp_path), {GEMINI_SYSTEM_SETTINGS: str(original)})
        assert second and second.path != first.path
        second.cleanup()
        assert Path(first.path).exists()
    finally:
        first.cleanup()
    assert not Path(first.path).exists()
    assert original.read_bytes() == before


@pytest.mark.parametrize(
    "text", ["[]", '{"mcpServers":[]}', '{"mcp":{"allowed":["corporate"]}}', "{bad"]
)
def test_invalid_or_restrictive_settings_fail_before_spawn(tmp_path, text):
    original = tmp_path / "settings.json"
    original.write_text(text)
    with pytest.raises(ValueError):
        prepare_mcp(request(tmp_path), {GEMINI_SYSTEM_SETTINGS: str(original)})
    assert original.read_text() == text


@pytest.mark.parametrize("outcome", ["success", "timeout", "cancel", "spawn-error"])
def test_overlay_cleanup_on_every_process_exit(tmp_path, monkeypatch, outcome):
    import wise_engine.adapters.gemini as module

    paths = []
    real_prepare = module.prepare_mcp

    def capture(req, env):
        result = real_prepare(req, env)
        paths.append(Path(result.path))
        return result

    monkeypatch.setattr(module, "prepare_mcp", capture)
    binary = tmp_path / "fake-gemini"
    binary.write_text(
        f"#!{sys.executable}\nimport json,time\n"
        + (
            'print(json.dumps({"type":"result","status":"success"}),flush=True)\n'
            if outcome == "success"
            else "time.sleep(30)\n"
        )
    )
    binary.chmod(0o700)

    async def run():
        req = request(tmp_path)
        if outcome == "timeout":
            req["timeout_ms"] = 60
        if outcome == "spawn-error":
            monkeypatch.setattr(
                module, "build_argv", lambda req: (_ for _ in ()).throw(ValueError("bad argv"))
            )
            with pytest.raises(ValueError, match="bad argv"):
                await start_gemini(req, lambda event: None, bin=str(binary), parent_env={})
            return
        handle = await start_gemini(req, lambda event: None, bin=str(binary), parent_env={})
        if outcome == "cancel":
            handle.done.cancel()
            with pytest.raises(asyncio.CancelledError):
                await handle.done
        else:
            result = await asyncio.wait_for(handle.done, 5)
            assert result["exit"] == ("ok" if outcome == "success" else "timeout")

    asyncio.run(run())
    assert paths and all(not path.exists() for path in paths)


def test_native_gemini_loads_private_child_server_without_model_call(tmp_path):
    binary = shutil.which("gemini")
    if not binary:
        pytest.skip("Gemini CLI is not installed")
    home = tmp_path / "home"
    home.mkdir()
    capture = tmp_path / "capture.json"
    fixture = tmp_path / "server.py"
    fixture.write_text("""import json,os,sys
from pathlib import Path
for line in sys.stdin:
    msg=json.loads(line)
    if msg.get('method')=='initialize':
        Path(os.environ['CAPTURE']).write_text(json.dumps({'token':os.environ['WISE_STEP_TOKEN']}))
        result={'protocolVersion':msg['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'wise-child-test','version':'1'}}
    elif msg.get('method')=='tools/list':
        result={'tools':[{'name':name,'description':'Fixture','inputSchema':{'type':'object'}} for name in ['wise_report','wise_ask','wise_context','wise_checkpoint']]}
    elif 'id' not in msg: continue
    else: result={}
    print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}),flush=True)
""")
    req = request(tmp_path)
    req["mcp_config"]["mcpServers"]["wise-engine"]["args"] = [str(fixture)]
    req["mcp_config"]["mcpServers"]["wise-engine"]["env"]["CAPTURE"] = str(capture)
    env = {
        "HOME": str(home),
        "GEMINI_CLI_TRUST_WORKSPACE": "true",
        "PATH": os.environ["PATH"],
        GEMINI_SYSTEM_SETTINGS: str(tmp_path / "absent.json"),
    }
    overlay = prepare_mcp(req, env)
    assert overlay
    try:
        result = subprocess.run(
            [binary, "mcp", "list"],
            env=env,
            cwd=tmp_path,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert capture.exists(), result.stdout + result.stderr
        assert json.loads(capture.read_text()) == {"token": "private-token"}
        assert "Connected" in result.stdout + result.stderr
    finally:
        overlay.cleanup()


@pytest.mark.parametrize(
    "config",
    [
        [],
        {"mcpServers": []},
        {"mcpServers": {"child": None}},
        {"mcpServers": {"child": {"command": "python", "env": {"TOKEN": 123}}}},
    ],
)
def test_malformed_child_config_is_rejected(tmp_path, config):
    req = request(tmp_path)
    req["mcp_config"] = config
    with pytest.raises(ValueError, match="gemini:"):
        prepare_mcp(req, {})
