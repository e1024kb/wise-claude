import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

from wise_engine.adapters import AdapterError, adapter_for, has_adapter
from wise_engine.constants import EFFORTS, HARNESSES

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}


def fake_binary(tmp_path, body, name="fake-provider"):
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)
    return str(path)


def request(tmp_path, **overrides):
    return dict(
        prompt="ping",
        model="inherit",
        cwd=str(tmp_path),
        mode="auto",
        timeout_ms=3000,
        auth="subscription",
        **overrides,
    )


def provider(harness):
    module = importlib.import_module("wise_engine.adapters." + harness)
    return module, getattr(module, "start_" + harness)


ROUND_TRIP = """import json, os, sys
from pathlib import Path
harness = HARNESS
argv = sys.argv[1:]
stdin = sys.stdin.read()
value = dict(argv=argv, stdin=stdin, cwd=os.getcwd(), env=dict(os.environ))
if "--output-schema" in argv:
    path = argv[argv.index("--output-schema") + 1]
    value.update(schema=json.loads(Path(path).read_text()), schema_path=path)
if "--prompt-file" in argv:
    path = argv[argv.index("--prompt-file") + 1]
    value.update(prompt=Path(path).read_text(), prompt_path=path)
def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)
if harness == "codex":
    emit(dict(type="thread.started", thread_id="fake-thread"))
    emit(dict(type="item.completed", item=dict(type="agent_message", text=json.dumps(value))))
    emit(dict(type="turn.completed", usage=dict(input_tokens=10, output_tokens=20)))
elif harness == "cursor":
    emit(dict(type="system", subtype="init", model="fake-model"))
    emit(dict(type="result", subtype="success", is_error=False, session_id="fake-session", result=json.dumps(value)))
elif harness == "gemini":
    emit(dict(type="init", session_id="fake-session"))
    emit(dict(type="message", role="assistant", content="answer: " + json.dumps(value)))
    emit(dict(type="result", status="success", stats=dict(input_tokens=10, output_tokens=20)))
else:
    print(json.dumps(dict(text=json.dumps(value), structuredOutput=value, sessionId="fake-session", usage=dict(input_tokens=10, output_tokens=20)), indent=2))
"""


@pytest.mark.parametrize("harness", ["codex", "cursor", "gemini", "grok"])
@pytest.mark.parametrize("large", [False, True])
def test_provider_roundtrip_argv_stdin_schema_env_and_cleanup(tmp_path, harness, large):
    async def run():
        module, start = provider(harness)
        binary = fake_binary(tmp_path, ROUND_TRIP.replace("HARNESS", repr(harness)))
        req = request(tmp_path, schema=SCHEMA, system="system")
        req["prompt"] = "🙂" * 30000 if large else "ping"
        events = []
        handle = await start(
            req,
            events.append,
            bin=binary,
            parent_env={
                "PATH": os.environ.get("PATH", ""),
                "ANTHROPIC_API_KEY": "dummy-never-inherited",
                "UNRELATED_SECRET": "dummy",
            },
        )
        assert handle.pid > 0
        assert handle.nudge is None
        result = await asyncio.wait_for(handle.done, 5)
        assert result["exit"] == "ok", result
        value = result["json"]
        assert value["cwd"] == str(tmp_path)
        assert "UNRELATED_SECRET" not in value["env"]
        assert "ANTHROPIC_API_KEY" not in value["env"]
        assert events and handle.snapshot()
        if harness == "codex":
            assert value["schema"] == module.strict_schema(SCHEMA)
            assert not Path(value["schema_path"]).exists()
            assert value["argv"][-1] == ("-" if large else module.compose_prompt(req))
            assert value["stdin"] == (module.compose_prompt(req) if large else "")
        elif harness == "cursor":
            assert value["stdin"] == module.compose_prompt(req)
            assert "--workspace" in value["argv"]
            assert result["model"] == "fake-model"
        elif harness == "gemini":
            assert ("-p" in value["argv"]) is not large
            assert value["stdin"] == (module.compose_prompt(req) if large else "")
            assert result["warnings"] == ["JSON extracted from surrounding prose"]
        else:
            assert value["stdin"] == ""
            if large:
                assert value["prompt"] == req["prompt"]
                assert not Path(value["prompt_path"]).exists()
            else:
                assert value["argv"][1] == "ping"

    asyncio.run(run())


@pytest.mark.parametrize("harness", HARNESSES)
@pytest.mark.parametrize("action", ["timeout", "kill", "missing", "cancel"])
def test_provider_process_failures_settle_and_cleanup(tmp_path, harness, action):
    async def run():
        _, start = provider(harness)
        binary = fake_binary(tmp_path, "import time\nprint('ready', flush=True)\ntime.sleep(20)\n")
        if action == "missing":
            binary += ".missing"
        req = request(tmp_path)
        if harness == "codex":
            req["schema"] = SCHEMA
        if harness == "grok":
            req["prompt"] = "x" * 100001
        if action == "timeout":
            req["timeout_ms"] = 50
        ready = asyncio.Event()
        handle = await start(req, lambda event: ready.set(), bin=binary, parent_env={})
        if action in ("kill", "cancel"):
            await asyncio.wait_for(ready.wait(), 2)
        if action == "kill":
            handle.kill("SIGTERM")
        if action == "cancel":
            handle.done.cancel()
            with pytest.raises(asyncio.CancelledError):
                await handle.done
            with pytest.raises(ProcessLookupError):
                os.kill(handle.pid, 0)
            return
        result = await asyncio.wait_for(handle.done, 3)
        assert result["exit"] == ("timeout" if action == "timeout" else "error")
        if action == "missing":
            assert "ENOENT" in result["error"]
        if action == "kill":
            assert "SIGTERM" in result["error"] or "stdout: ready" in result["error"]

    asyncio.run(run())


CLAUDE_ECHO = """import json, sys
from pathlib import Path
first = json.loads(sys.stdin.readline())
assert first == dict(type="control_request", request_id="wise-init", request=dict(subtype="initialize", hooks={}))
print(json.dumps(dict(type="system", subtype="init", session_id="fake-session", tools=[])), flush=True)
for index, line in enumerate(sys.stdin, 1):
    value = json.loads(line)
    assert value["type"] == "user"
    text = "got " + value["message"]["content"][0]["text"]
    print(json.dumps(dict(type="assistant", message=dict(content=[dict(type="text", text=text)]))), flush=True)
    print(json.dumps(dict(type="result", subtype="success", is_error=False, session_id="fake-session", result=text, usage=dict(input_tokens=index, output_tokens=index*2))), flush=True)
"""


@pytest.mark.parametrize("coalesce", [False, True])
def test_claude_handshake_nudges_and_eof(tmp_path, coalesce):
    async def run():
        from wise_engine.adapters.claude import start_claude

        if coalesce:
            body = """import json, sys
json.loads(sys.stdin.readline())
a = json.loads(sys.stdin.readline())["message"]["content"][0]["text"]
b = json.loads(sys.stdin.readline())["message"]["content"][0]["text"]
print(json.dumps(dict(type="result", subtype="success", is_error=False, result="got " + a + "+" + b, queued_turn_count=0)), flush=True)
assert sys.stdin.read() == ""
"""
        else:
            body = CLAUDE_ECHO
        binary = fake_binary(tmp_path, body)
        events = []
        handle = await start_claude(request(tmp_path), events.append, bin=binary, parent_env={})
        handle.nudge("again")
        result = await asyncio.wait_for(handle.done, 3)
        assert result["exit"] == "ok"
        assert result["text"] == ("got ping+again" if coalesce else "got again")
        assert handle.snapshot()["results"] == (1 if coalesce else 2)
        if not coalesce:
            assert result["usage"]["input"] == 2
            assert handle.snapshot()["turns"] == 2
            assert result["cursor"] == "fake-session"
        with pytest.raises(RuntimeError, match="stdin is closed"):
            handle.nudge("late")

    asyncio.run(run())


@pytest.mark.parametrize(
    "mode,tool,input,behavior",
    [
        ("approval-required", "Bash", {"command": "git status"}, "deny"),
        ("full-access", "Bash", {"command": "git status"}, "allow"),
        ("auto", "Read", {"file_path": "/tmp/example"}, "allow"),
    ],
)
def test_claude_permission_control_roundtrip(tmp_path, mode, tool, input, behavior):
    async def run():
        from wise_engine.adapters.claude import start_claude

        body = """import json, sys
assert json.loads(sys.stdin.readline())["request"]["subtype"] == "initialize"
assert json.loads(sys.stdin.readline())["type"] == "user"
print(json.dumps(dict(type="control_request", request_id="permission", request=dict(subtype="can_use_tool", tool_name=TOOL, input=INPUT))), flush=True)
response = json.loads(sys.stdin.readline())
print(json.dumps(dict(type="result", subtype="success", is_error=False, result="permission answered", structured_output=response)), flush=True)
assert sys.stdin.read() == ""
""".replace("TOOL", repr(tool)).replace("INPUT", repr(input))
        binary = fake_binary(tmp_path, body)
        req = request(tmp_path)
        req["mode"] = mode
        handle = await start_claude(req, lambda event: None, bin=binary, parent_env={})
        result = await asyncio.wait_for(handle.done, 3)
        assert result["exit"] == "ok"
        response = result["json"]["response"]
        assert response["request_id"] == "permission"
        assert response["subtype"] == "success"
        assert response["response"]["behavior"] == behavior
        assert handle.snapshot()["permissions"] == [f"{behavior}:{tool}"]

    asyncio.run(run())


@pytest.mark.parametrize("harness", ["codex", "cursor", "gemini", "grok"])
def test_nonstring_resume_warns(tmp_path, harness):
    async def run():
        _, start = provider(harness)
        binary = fake_binary(tmp_path, ROUND_TRIP.replace("HARNESS", repr(harness)))
        handle = await start(
            request(tmp_path, resume={"thread": "bad"}, max_turns=5),
            lambda event: None,
            bin=binary,
            parent_env={},
        )
        result = await handle.done
        assert "ignored non-string resume cursor" in result["warnings"]
        if harness == "cursor":
            assert "cursor does not support max_turns" in result["warnings"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "harness,key",
    [
        ("claude", "ANTHROPIC_API_KEY"),
        ("codex", "OPENAI_API_KEY"),
        ("cursor", "CURSOR_API_KEY"),
        ("gemini", "GEMINI_API_KEY"),
        ("gemini", "GOOGLE_API_KEY"),
        ("grok", "XAI_API_KEY"),
    ],
)
def test_api_key_probes_require_provider_key(harness, key):
    async def run():
        module, _ = provider(harness)
        assert not (await module.probe_auth("api-key", parent_env={}))["ok"]
        assert (await module.probe_auth("api-key", parent_env={key: "dummy-test-key"}))["ok"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "harness,output,code,expected",
    [
        ("claude", '{"loggedIn":true}', 0, True),
        ("claude", '{"loggedIn":true}', 1, True),
        ("claude", '{"loggedIn":false}', 0, False),
        ("claude", "bad json", 0, False),
        ("codex", "Logged in using ChatGPT", 0, True),
        ("codex", "Not logged in", 0, False),
        ("codex", "Logged in using ChatGPT", 1, False),
        ("cursor", '{"isAuthenticated":true}', 0, True),
        ("cursor", '{"isAuthenticated":true}', 1, False),
        ("cursor", '{"isAuthenticated":false}', 0, False),
        ("cursor", "invalid", 0, False),
    ],
)
def test_subscription_cli_probes(tmp_path, harness, output, code, expected):
    async def run():
        module, _ = provider(harness)
        expected_argv = {
            "claude": ["auth", "status"],
            "codex": ["login", "status"],
            "cursor": ["status", "--format", "json"],
        }[harness]
        binary = fake_binary(
            tmp_path,
            f"import sys\nassert sys.argv[1:] == {expected_argv!r}\nprint({output!r}, file={'sys.stderr' if harness == 'codex' else 'sys.stdout'})\nsys.exit({code})\n",
        )
        result = await module.probe_auth("subscription", bin=binary, parent_env={})
        assert result["ok"] is expected
        assert "login" in result["login_cmd"]
        assert not (
            await module.probe_auth("subscription", bin=binary + ".missing", parent_env={})
        )["ok"]

    asyncio.run(run())


@pytest.mark.parametrize("harness", ["gemini", "grok"])
def test_file_login_probes_and_paths(tmp_path, harness):
    async def run():
        module, _ = provider(harness)
        env = {"HOME": str(tmp_path / "user")}
        home = str(tmp_path / "override")
        expected = Path(home) / (
            ".gemini/oauth_creds.json" if harness == "gemini" else ".grok/auth.json"
        )
        assert module.auth_file_path(env, home) == str(expected)
        assert not (await module.probe_auth("subscription", parent_env=env, home=home))["ok"]
        expected.parent.mkdir(parents=True)
        for value, wanted in [
            (None, False),
            ({}, False),
            ([], False),
            ({"refresh_token": "dummy"}, True),
            ({"unrelated": 1}, harness == "grok"),
        ]:
            expected.write_text(json.dumps(value))
            assert (await module.probe_auth("subscription", parent_env=env, home=home))[
                "ok"
            ] is wanted
        expected.write_text("invalid")
        assert not (await module.probe_auth("subscription", parent_env=env, home=home))["ok"]
        config = "GEMINI_CLI_HOME" if harness == "gemini" else "GROK_HOME"
        env[config] = str(tmp_path / "configured")
        assert module.auth_file_path(env, home) == str(
            tmp_path
            / "configured"
            / (".gemini/oauth_creds.json" if harness == "gemini" else "auth.json")
        )

    asyncio.run(run())


def test_registry_and_efforts():
    for harness in HARNESSES:
        assert has_adapter(harness)
        assert adapter_for(harness).id == harness
        for effort in EFFORTS:
            adapter_for(harness).effort_map(effort)
    assert adapter_for("cursor").bin == "cursor-agent"
    assert not has_adapter("unknown")
    with pytest.raises(AdapterError) as raised:
        adapter_for("unknown")
    assert raised.value.code == "HARNESS_UNAVAILABLE"


@pytest.mark.parametrize("harness", HARNESSES)
def test_immediate_done_cancellation_closes_child_and_temp_files(tmp_path, monkeypatch, harness):
    async def run():
        from wise_engine.adapters import _common

        original = _common.temporary_file
        files = []

        def capture_file(*args):
            result = original(*args)
            files.append(result.path)
            return result

        module, start = provider(harness)
        if harness in ("codex", "grok"):
            monkeypatch.setattr(module, "temporary_file", capture_file)
        binary = fake_binary(tmp_path, "import time\ntime.sleep(20)\n")
        req = request(tmp_path)
        if harness == "codex":
            req["schema"] = SCHEMA
        if harness == "grok":
            req["prompt"] = "x" * 100001
        handle = await start(req, lambda event: None, bin=binary, parent_env={})
        handle.done.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handle.done
        with pytest.raises(ProcessLookupError):
            os.kill(handle.pid, 0)
        assert all(not Path(path).exists() for path in files)

    asyncio.run(run())


@pytest.mark.parametrize("harness", HARNESSES)
def test_raw_events_reassemble_unicode_and_reject_non_json_constants(harness):
    from wise_engine.spawn import SpawnExit

    module, _ = provider(harness)
    parser = module.create_stream_parser(pool="subscription", now=lambda: "T")
    wire = '{"notice":"🙂é"}\nNaN\n'.encode()
    events = []
    for byte in wire:
        events += parser.feed(bytes([byte]))
    assert events[0]["parsed"] == {"notice": "🙂é"}
    assert events[0]["line"] == '{"notice":"🙂é"}'
    assert "parsed" not in events[1]
    assert parser.finish(SpawnExit(0, None, False, ""))["exit"] == "error"


def test_schema_by_instruction_uses_strict_json_and_rounds_large_numbers():
    from wise_engine.adapters.gemini import extract_json

    assert not extract_json("NaN")["ok"]
    assert not extract_json("Infinity")["ok"]
    assert extract_json("9007199254740993")["json"] == 9007199254740992


def test_claude_messages_escape_isolated_surrogates():
    from wise_engine.adapters.claude import user_message

    line = user_message("clipped \ud83d")
    assert line.encode("utf-8")
    assert json.loads(line)["message"]["content"][0]["text"] == "clipped \ud83d"


def test_grok_pretty_json_reassembles_unicode_bytes():
    from wise_engine.adapters.grok import create_stream_parser
    from wise_engine.spawn import SpawnExit

    parser = create_stream_parser(pool="subscription")
    wire = json.dumps(
        {"text": "🙂é", "structuredOutput": {"value": "🙂é"}}, ensure_ascii=False, indent=2
    ).encode()
    for byte in wire:
        parser.feed(bytes([byte]))
    result = parser.finish(SpawnExit(0, None, False, ""))
    assert result["exit"] == "ok"
    assert result["text"] == "🙂é"
    assert result["json"] == {"value": "🙂é"}
