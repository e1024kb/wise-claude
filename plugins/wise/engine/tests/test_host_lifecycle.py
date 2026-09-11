from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import sysconfig
import tempfile
import venv
from pathlib import Path

import pytest

from wise_engine.bootstrap import environment_key, environment_path
from wise_engine.host_setup import apply_plan, doctor, location, plan_setup, refresh_existing

ENGINE = Path(__file__).resolve().parents[1]
WORKFLOW = """version: 2
name: host-control
inputs:
  - name: fixture
    prompt: Enter fixture label
preflight:
  control-mode: interactive
steps:
  - id: prepare
    type: bash
    run: echo harmless
  - id: approve
    type: approval
    message: Accept fixture?
    depends_on: [prepare]
  - id: question
    type: ask
    message: Choose fixture value
    options: [alpha, beta]
    depends_on: [approve]
"""


@pytest.fixture
def profile():
    with tempfile.TemporaryDirectory(prefix="w6-", dir="/tmp") as directory:
        root = Path(directory)
        home = root / "home"
        home.mkdir()
        workflow = root / "workflow.yaml"
        workflow.write_text(WORKFLOW)
        data = home / ".local/share/wise"
        target = environment_path(ENGINE / "requirements.txt", data)
        venv.EnvBuilder(with_pip=False).create(target)
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        packages = target / "lib" / version / "site-packages"
        shutil.rmtree(packages)
        packages.symlink_to(Path(sysconfig.get_path("purelib")), target_is_directory=True)
        (target / ".ready.json").write_text(
            json.dumps({"key": environment_key(ENGINE / "requirements.txt")})
        )
        env = {
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "WISE_PYTHON": sys.executable,
            "CODEX_HOME": str(home / ".codex"),
            "NO_COLOR": "1",
            "TERM": "dumb",
            "DISABLE_AUTOUPDATER": "1",
        }
        yield home, workflow, env


async def cli(home: Path, host: str, env: dict[str, str], *args: str) -> object:
    process = await asyncio.create_subprocess_exec(
        str(location(home) / "bin/wise-engine"),
        "--wise-host",
        host,
        *args,
        cwd=home,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(process.communicate(), 20)
    assert process.returncode == 0, (args, out.decode(), err.decode())
    if args[0] == "daemon":
        return None
    return json.loads(out) if out.strip() else None


async def lifecycle(call, workflow: Path, home: Path, restart) -> None:
    pending = await call(
        "preflight",
        {"workflow": str(workflow), "cwd": str(home), "answers": {}, "interactive": False},
    )
    assert "input.fixture" in [question["id"] for question in pending["questions"]]
    answers = {"control-mode": "interactive", "input.fixture": "explicit-test-answer"}
    pre = await call(
        "preflight",
        {"workflow": str(workflow), "cwd": str(home), "answers": answers, "interactive": False},
    )
    assert pre.get("questions") == [], pre
    started = await call(
        "run",
        {"workflow": str(workflow), "cwd": str(home), "answers": answers, "interactive": False},
    )
    run_id = started["run_id"]

    async def state(status):
        for _ in range(30):
            result = await call("status", {"run_id": run_id})
            if result["status"] == status:
                return result
            assert result["status"] not in ("failed", "cancelled"), result
            await asyncio.sleep(0.03)
        raise AssertionError(f"Never reached {status}")

    gated = await state("gated")
    assert gated["gate"]["kind"] == "approval"
    waited = await call("wait", {"run_id": run_id, "timeout_ms": 1})
    assert waited["status"] == "gated"
    accepted = await call(
        "answer", {"run_id": run_id, "gate_id": gated["gate"]["gate_id"], "value": "approve"}
    )
    assert accepted["accepted"] is True
    for _ in range(30):
        gated = await call("status", {"run_id": run_id})
        if gated.get("gate", {}).get("kind") == "ask":
            break
        await asyncio.sleep(0.03)
    assert gated["gate"]["kind"] == "ask", gated
    assert (
        await call(
            "answer", {"run_id": run_id, "gate_id": gated["gate"]["gate_id"], "value": "beta"}
        )
    )["accepted"]
    await state("completed")
    retry = workflow.with_name("retry.yaml")
    retry.write_text(
        WORKFLOW.replace(
            "echo harmless", "if [ ! -e restart.once ]; then touch restart.once; sleep 30; fi"
        )
    )
    started = await call(
        "run", {"workflow": str(retry), "cwd": str(home), "answers": answers, "interactive": False}
    )
    run_id = started["run_id"]
    for _ in range(100):
        if (home / "restart.once").exists():
            break
        await asyncio.sleep(0.02)
    assert (home / "restart.once").exists()
    await restart()
    assert (await call("status", {"run_id": run_id}))["status"] == "paused"
    resumed = await call("resume", {"run_id": run_id})
    assert resumed["status"] == "running"
    await state("gated")
    assert (await call("cancel", {"run_id": run_id}))["status"] == "cancelled"


@pytest.mark.parametrize("host", ["claude", "codex", "cursor", "grok"])
def test_real_engine_cli_fallback_for_each_host_binding(profile, host):
    home, workflow, env = profile
    apply_plan(plan_setup(plugin_root=ENGINE.parent, host=host, home=home, python=sys.executable))

    async def scenario():
        async def call(method, params):
            if method in ("preflight", "run"):
                return await cli(
                    home,
                    host,
                    env,
                    method,
                    params["workflow"],
                    "--cwd",
                    params["cwd"],
                    "--answers",
                    json.dumps(params["answers"]),
                )
            args = [method, params["run_id"]]
            if method == "wait":
                args.extend(["--timeout-ms", str(params["timeout_ms"])])
            if method == "answer":
                args.extend([params["gate_id"], params["value"]])
            return await cli(home, host, env, *args)

        try:

            async def restart():
                await cli(home, host, env, "daemon", "stop", "--now")
                await cli(home, host, env, "daemon", "start")

            await lifecycle(call, workflow, home, restart)
        finally:
            await cli(home, host, env, "daemon", "stop")

    asyncio.run(scenario())


def test_codex_real_engine_no_model_control(profile):
    executable = shutil.which("codex")
    if not executable:
        pytest.skip("Codex CLI is not installed")
    home, workflow, env = profile
    apply_plan(
        plan_setup(plugin_root=ENGINE.parent, host="codex", home=home, python=sys.executable)
    )

    async def scenario():
        process = await asyncio.create_subprocess_exec(
            executable,
            "app-server",
            env=env,
            cwd=home,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        counter = 0

        async def request(method, params):
            nonlocal counter
            counter += 1
            identifier = counter
            process.stdin.write(
                (json.dumps({"id": identifier, "method": method, "params": params}) + "\n").encode()
            )
            await process.stdin.drain()
            while True:
                message = json.loads(await asyncio.wait_for(process.stdout.readline(), 25))
                if message.get("id") == identifier:
                    assert "error" not in message, message
                    return message["result"]

        try:
            await request(
                "initialize",
                {
                    "clientInfo": {"name": "wise-real-control", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            process.stdin.write(b'{"method":"initialized"}\n')
            inventory = await request("mcpServerStatus/list", {})
            server = next(row for row in inventory["data"] if row["name"] == "wise-engine")
            assert len(server["tools"]) == 8
            thread = (
                await request(
                    "thread/start",
                    {
                        "cwd": str(home),
                        "ephemeral": True,
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                    },
                )
            )["thread"]
            assert thread["turns"] == []

            async def call(method, params):
                result = await request(
                    "mcpServer/tool/call",
                    {
                        "threadId": thread["id"],
                        "server": "wise-engine",
                        "tool": "wise_" + method,
                        "arguments": params,
                    },
                )
                if "result" in result:
                    result = result["result"]
                assert not result.get("isError"), result
                return json.loads(result["content"][0]["text"])

            async def restart():
                await cli(home, "codex", env, "daemon", "stop", "--now")
                await cli(home, "codex", env, "daemon", "start")

            await lifecycle(call, workflow, home, restart)
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()
            await cli(home, "codex", env, "daemon", "stop")

    asyncio.run(scenario())


@pytest.mark.parametrize("host", ["claude", "cursor", "grok"])
def test_each_native_host_connects_real_python_server(profile, host):
    name = {"claude": "claude", "cursor": "cursor-agent", "grok": "grok"}[host]
    executable = shutil.which(name)
    if executable is None and host == "grok":
        candidate = Path.home() / ".grok/bin/grok"
        if candidate.exists():
            executable = str(candidate)
    if executable is None:
        pytest.skip(f"{host} CLI is not installed")
    home, workflow, env = profile
    apply_plan(plan_setup(plugin_root=ENGINE.parent, host=host, home=home, python=sys.executable))
    args = {
        "claude": ["mcp", "get", "wise-engine"],
        "cursor": ["mcp", "list-tools", "wise-engine"],
        "grok": ["mcp", "doctor", "wise-engine", "--json"],
    }[host]

    async def scenario():
        if host == "cursor":
            approval = await asyncio.create_subprocess_exec(
                executable,
                "mcp",
                "enable",
                "wise-engine",
                cwd=home,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            enabled_out, enabled_err = await asyncio.wait_for(approval.communicate(), 15)
            assert approval.returncode == 0, (enabled_out.decode(), enabled_err.decode())
        process = await asyncio.create_subprocess_exec(
            executable,
            *args,
            cwd=home,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(process.communicate(), 25)
        assert process.returncode == 0, (out.decode(), err.decode())
        assert doctor(home=home, host=host)["registration_ok"]
        assert not refresh_existing(plugin_root=ENGINE.parent, home=home, host=host)["refreshed"]
        text = out.decode()
        if host == "cursor":
            for tool in (
                "preflight",
                "run",
                "wait",
                "status",
                "answer",
                "cancel",
                "resume",
                "nudge",
            ):
                assert "wise_" + tool in text, text
        elif host == "claude":
            assert "Connected" in text, text
        else:
            assert "8 tools" in text, text
        try:
            assert await cli(home, host, env, "status") == []
        finally:
            await cli(home, host, env, "daemon", "stop")

    asyncio.run(scenario())
