from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from wise_engine.host_setup import apply_plan, plan_setup

FIXTURE = """import json,os,sys
from pathlib import Path
log = Path(os.environ['HOME']) / 'capture.jsonl'
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if method == 'initialize':
        with log.open('a') as file: file.write(json.dumps(message['params'])+'\\n')
        result = {'protocolVersion': message['params']['protocolVersion'],
                  'capabilities': {'tools': {}}, 'serverInfo': {'name': 'wise-fixture', 'version': '1'}}
    elif method == 'tools/list':
        result = {'tools': [{'name':'wise_status','description':'Local fixture control',
                            'inputSchema':{'type':'object','properties':{}}}]}
    elif method == 'tools/call':
        result = {'content':[{'type':'text','text':'fixture status'}]}
    elif method == 'resources/list': result = {'resources':[]}
    elif method == 'resources/templates/list': result = {'resourceTemplates':[]}
    elif method == 'prompts/list': result = {'prompts':[]}
    elif 'id' not in message: continue
    else: result = {}
    print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':result}), flush=True)
"""


def native_binary(host: str) -> str:
    name = {"claude": "claude", "codex": "codex", "cursor": "cursor-agent", "grok": "grok"}[host]
    executable = shutil.which(name)
    if not executable and host == "grok":
        candidate = Path.home() / ".grok/bin/grok"
        if candidate.exists():
            executable = str(candidate)
    if executable is None:
        pytest.skip(f"{host} CLI is not installed")
    return executable


def prepare(tmp_path: Path, host: str) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "plugin space é"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text('{"name":"wise","version":"fixture"}')
    (root / "engine").mkdir()
    (root / "engine/fixture.py").write_text(FIXTURE)
    (root / "engine/engine.sh").write_text(
        '#!/bin/sh\nexec "$WISE_PYTHON" "${0%/*}/fixture.py" "$@"\n'
    )
    home = tmp_path / "isolated home"
    apply_plan(plan_setup(plugin_root=root, host=host, home=home))
    (home / "work").mkdir()
    env = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "WISE_PYTHON": sys.executable,
        "CODEX_HOME": str(home / ".codex"),
        "NO_COLOR": "1",
        "TERM": "dumb",
        "DISABLE_AUTOUPDATER": "1",
    }
    return home, env


@pytest.mark.parametrize("host", ["claude", "cursor", "grok"])
def test_native_cli_loads_managed_registration(tmp_path: Path, host: str) -> None:
    executable = native_binary(host)
    home, env = prepare(tmp_path, host)
    args = {
        "claude": ["mcp", "get", "wise-engine"],
        "cursor": ["mcp", "list-tools", "wise-engine"],
        "grok": ["mcp", "doctor", "wise-engine", "--json"],
    }[host]
    result = subprocess.run(
        [executable, *args], env=env, cwd=home / "work", capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    captures = [json.loads(line) for line in (home / "capture.jsonl").read_text().splitlines()]
    assert captures and all("protocolVersion" in capture for capture in captures)
    if host == "claude":
        assert "elicitation" in captures[-1]["capabilities"]
    if host == "cursor":
        assert captures[-1]["capabilities"]["elicitation"]["form"] == {}


def test_codex_native_app_server_lists_and_calls_without_model_turn(tmp_path: Path) -> None:
    executable = native_binary("codex")
    home, env = prepare(tmp_path, "codex")

    async def control() -> None:
        process = await asyncio.create_subprocess_exec(
            executable,
            "app-server",
            env=env,
            cwd=home / "work",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdin and process.stdout

        async def call(identifier: int, method: str, params: object) -> dict:
            process.stdin.write(
                (json.dumps({"id": identifier, "method": method, "params": params}) + "\n").encode()
            )
            await process.stdin.drain()
            while True:
                message = json.loads(await asyncio.wait_for(process.stdout.readline(), 20))
                if message.get("id") == identifier:
                    assert "error" not in message, message
                    return message["result"]

        try:
            await call(
                1,
                "initialize",
                {
                    "clientInfo": {"name": "wise-fixture", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            process.stdin.write(b'{"method":"initialized"}\n')
            status = await call(2, "mcpServerStatus/list", {})
            assert any(row["name"] == "wise-engine" for row in status["data"])
            result = await call(
                3,
                "thread/start",
                {
                    "cwd": str(home / "work"),
                    "ephemeral": True,
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                },
            )
            assert result["thread"]["turns"] == []
            called = await call(
                4,
                "mcpServer/tool/call",
                {
                    "threadId": result["thread"]["id"],
                    "server": "wise-engine",
                    "tool": "wise_status",
                    "arguments": {},
                },
            )
            assert "fixture status" in json.dumps(called)
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()

    asyncio.run(control())


def test_codex_literal_plugin_root_fails_after_restart_then_managed_setup_repairs(tmp_path: Path):
    import tomlkit

    executable = native_binary("codex")
    home, env = prepare(tmp_path, "codex")
    plugin = tmp_path / "plugin space é"
    config = home / ".codex/config.toml"
    literal = "${CLAUDE_PLUGIN_ROOT}/engine/engine.sh"
    config.write_text(
        tomlkit.dumps(
            {
                "mcp_servers": {
                    "wise-engine": {
                        "command": "/bin/bash",
                        "args": [literal, "mcp"],
                        "env": {"CLAUDE_PLUGIN_ROOT": str(plugin)},
                    }
                }
            }
        )
    )
    configured = subprocess.run(
        [executable, "mcp", "get", "wise-engine", "--json"],
        env=env,
        cwd=home / "work",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert configured.returncode == 0, configured.stderr
    assert json.loads(configured.stdout)["transport"]["args"][0] == literal
    processes = []

    async def attempt(expect_success: bool) -> None:
        process = await asyncio.create_subprocess_exec(
            executable,
            "app-server",
            env=env,
            cwd=home / "work",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        processes.append(process.pid)
        assert process.stdin and process.stdout

        async def request(identifier: int, method: str, params: object) -> dict:
            process.stdin.write(
                (json.dumps({"id": identifier, "method": method, "params": params}) + "\n").encode()
            )
            await process.stdin.drain()
            while True:
                message = json.loads(await asyncio.wait_for(process.stdout.readline(), 20))
                if message.get("id") == identifier:
                    return message

        try:
            initialized = await request(
                1,
                "initialize",
                {
                    "clientInfo": {"name": "wise-literal-regression", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            assert "error" not in initialized
            process.stdin.write(b'{"method":"initialized"}\n')
            inventory = await request(2, "mcpServerStatus/list", {})
            server = next(
                row for row in inventory["result"]["data"] if row["name"] == "wise-engine"
            )
            assert bool(server["tools"]) is expect_success
            started = await request(
                3,
                "thread/start",
                {
                    "cwd": str(home / "work"),
                    "ephemeral": True,
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                },
            )
            thread = started["result"]["thread"]
            assert thread["turns"] == []
            called = await request(
                4,
                "mcpServer/tool/call",
                {
                    "threadId": thread["id"],
                    "server": "wise-engine",
                    "tool": "wise_status",
                    "arguments": {},
                },
            )
            if expect_success:
                assert "error" not in called and "fixture status" in json.dumps(called)
            else:
                assert called["error"]["code"] == -32603
                assert "startup failed" in called["error"]["message"].lower()
                assert not (home / "capture.jsonl").exists()
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()

    async def scenario() -> None:
        await attempt(False)
        await attempt(False)
        apply_plan(plan_setup(plugin_root=plugin, host="codex", home=home))
        assert literal not in config.read_text()
        await attempt(True)
        assert len(set(processes)) == 3

    asyncio.run(scenario())
