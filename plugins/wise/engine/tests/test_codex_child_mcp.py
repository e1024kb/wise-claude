from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest

from wise_engine.adapters.codex import build_argv, child_mcp_overrides, start_codex
from wise_engine.daemon import start_daemon
from wise_engine.steps.agent import child_mcp_config

ENGINE = Path(__file__).resolve().parents[1]


def request(root: Path, token: str = "fixture-token") -> dict:
    return {
        "prompt": "Use child tools",
        "cwd": str(root),
        "mode": "auto",
        "auth": "subscription",
        "timeout_ms": 10000,
        "env": {"WISE_STEP_TOKEN": "wrong-token"},
        "mcp_config": child_mcp_config(
            {
                "engine_root": str(ENGINE),
                "socket_path": str(root / "socket"),
                "data_root": str(root),
            },
            token,
        ),
    }


def test_overrides_keep_tokens_out_of_argv_and_use_distinct_names(tmp_path: Path):
    req = request(tmp_path)
    first, env = child_mcp_overrides(req)
    second, _ = child_mcp_overrides(req)
    assert first != second
    assert "fixture-token" not in json.dumps(first)
    table = tomllib.loads(first[1])["mcp_servers"]
    name, server = next(iter(table.items()))
    assert name.startswith("wise-step-") and name != "wise-engine"
    assert server["args"] == ["-m", "wise_engine", "unit-mcp"]
    assert set(server["env_vars"]) == set(env)
    assert env["WISE_STEP_TOKEN"] == "fixture-token"
    assert "env" not in server
    for resume in (None, "session-id"):
        running = {**req, **({"resume": resume} if resume else {})}
        argv = build_argv(running, mcp_args=first)
        assert first[1] in argv and argv[-1] == req["prompt"]
        assert "fixture-token" not in json.dumps(argv)
    assert child_mcp_overrides({}) == ([], {})


def test_spawn_receives_child_environment_without_mutating_parent(tmp_path: Path):
    capture = tmp_path / "capture.json"
    executable = tmp_path / "fake-codex"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """import json,os,sys
from pathlib import Path
Path(os.environ['CAPTURE_PATH']).write_text(json.dumps({'args':sys.argv[1:],
    'token':os.environ.get('WISE_STEP_TOKEN'),'home':os.environ.get('CODEX_HOME')}))
print(json.dumps({'type':'thread.started','thread_id':'fixture'}))
print(json.dumps({'type':'turn.completed','usage':{}}))
"""
    )
    executable.chmod(0o700)
    req = request(tmp_path)
    req["env"]["CAPTURE_PATH"] = str(capture)
    parent = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "CODEX_HOME": str(tmp_path / "user-codex"),
    }
    before = dict(parent)

    async def run():
        handle = await start_codex(req, lambda event: None, bin=str(executable), parent_env=parent)
        assert (await handle.done)["exit"] == "ok"

    asyncio.run(run())
    observed = json.loads(capture.read_text())
    assert observed["token"] == "fixture-token"
    assert observed["home"] == parent["CODEX_HOME"]
    assert "fixture-token" not in json.dumps(observed["args"])
    assert parent == before and req["env"]["WISE_STEP_TOKEN"] == "wrong-token"


@pytest.mark.parametrize(
    "config",
    [
        [],
        {},
        {"mcpServers": []},
        {"mcpServers": {"bad": {"url": "https://invalid.example"}}},
        {"mcpServers": {"bad": {"command": "python", "env": {"TOKEN": 1}}}},
    ],
)
def test_invalid_child_config_fails_closed(config):
    with pytest.raises(ValueError):
        child_mcp_overrides({"mcp_config": config})


def test_native_codex_child_tools_and_token_forwarding_without_model_call():
    executable = shutil.which("codex")
    if not executable:
        pytest.skip("Codex CLI is not installed")

    async def scenario(root: Path):
        profile = root / "codex"
        profile.mkdir()
        config = profile / "config.toml"
        before = '# preserve user configuration\nmodel_reasoning_effort="low"\n[mcp_servers.wise-engine]\ncommand="false"\nenabled=false\n'
        config.write_text(before)
        seen = []

        def context(params, call):
            seen.append(params)
            return {"value": "fixture child context"}

        daemon = await start_daemon(
            data_root=str(root),
            socket_path=str(root / "socket"),
            env={},
            handlers={"child_context": context},
        )
        req = request(root)
        overrides, child_env = child_mcp_overrides(req)
        name = next(iter(tomllib.loads(overrides[1])["mcp_servers"]))
        env = {
            "HOME": str(root),
            "CODEX_HOME": str(profile),
            "PATH": os.environ["PATH"],
            "NO_COLOR": "1",
            "TERM": "dumb",
            **child_env,
        }
        process = await asyncio.create_subprocess_exec(
            executable,
            "app-server",
            *overrides,
            cwd=root,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdin and process.stdout
        counter = 0

        async def call(method, params):
            nonlocal counter
            counter += 1
            process.stdin.write(
                (json.dumps({"id": counter, "method": method, "params": params}) + "\n").encode()
            )
            await process.stdin.drain()
            while True:
                message = json.loads(await asyncio.wait_for(process.stdout.readline(), 20))
                if message.get("id") == counter:
                    assert "error" not in message, message
                    return message["result"]

        try:
            await call(
                "initialize",
                {
                    "clientInfo": {"name": "wise-child-proof", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            process.stdin.write(b'{"method":"initialized"}\n')
            inventory = await call("mcpServerStatus/list", {})
            child = next(row for row in inventory["data"] if row["name"] == name)
            assert set(child["tools"]) == {
                "wise_report",
                "wise_ask",
                "wise_context",
                "wise_checkpoint",
            }
            thread = (
                await call(
                    "thread/start",
                    {
                        "cwd": str(root),
                        "ephemeral": True,
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                    },
                )
            )["thread"]
            assert thread["turns"] == []
            result = await call(
                "mcpServer/tool/call",
                {
                    "threadId": thread["id"],
                    "server": name,
                    "tool": "wise_context",
                    "arguments": {"key": "fixture"},
                },
            )
            assert "fixture child context" in json.dumps(result)
            assert seen == [{"key": "fixture", "token": "fixture-token"}]
            assert config.read_text() == before
            assert "fixture-token" not in config.read_text()
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()
            await daemon.close()

    with tempfile.TemporaryDirectory(prefix="wc-", dir="/tmp") as temporary:
        asyncio.run(scenario(Path(temporary)))
