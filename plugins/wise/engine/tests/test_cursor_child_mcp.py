from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from wise_engine.adapters import cursor
from wise_engine.adapters.cursor_acp import permission_result, prepare_servers
from wise_engine.models import catalog_for

ACP = r"""import json,os,subprocess,sys,time
from pathlib import Path
log=Path(os.environ['CAPTURE'])
def emit(value):
 print(json.dumps(dict(jsonrpc='2.0',**value)),flush=True)
def record(value):
 with log.open('a') as file:file.write(json.dumps(value)+'\n')
record({'argv':sys.argv[1:],'pid':os.getpid()})
children=[]
for line in sys.stdin:
 message=json.loads(line); method=message.get('method');params=message.get('params',{})
 record(message)
 if method=='initialize':
  if os.environ.get('FAIL')=='eof':sys.exit(3)
  if os.environ.get('FAIL')=='auth':
   emit({'id':message['id'],'error':{'code':-32000,'message':'Authentication required'}});continue
  result={'protocolVersion':1,'agentCapabilities':{'loadSession':True}}
 elif method in ('session/new','session/load'):
  for server in params['mcpServers']:
   record({'config_path':server['args'][-1]})
   if os.environ.get('FAIL')=='mcp':continue
   child=subprocess.Popen([server['command'],*server['args']],env={**os.environ,**{item['name']:item['value'] for item in server.get('env',[])}},stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
   children.append(child);record({'wrapper_pid':child.pid})
   child.stdin.write(json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'fixture','version':'1'}}})+'\n');child.stdin.flush()
   response=child.stdout.readline()
   if not response:sys.exit(4)
  result={'sessionId':'session-fixture','models':{'currentModelId':'fixture-model'}}
  if method=='session/load':
   emit({'method':'session/update','params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'Old history'}}}})
 elif method=='session/set_mode':result={}
 elif method=='session/prompt':
  if os.environ.get('FAIL')=='hang':time.sleep(60)
  emit({'method':'session/update','params':{'update':{'sessionUpdate':'tool_call','toolCallId':'read','title':'Read','kind':'read','rawInput':{'path':'source.py'}}}})
  emit({'id':'permission','method':'session/request_permission','params':{'toolCall':{'toolCallId':'tool','title':'`git push`','kind':'execute'},'options':[{'optionId':'yes','kind':'allow_once'},{'optionId':'no','kind':'reject_once'}]}})
  record({'permission_reply':json.loads(sys.stdin.readline())})
  emit({'method':'session/update','params':{'sessionId':'session-fixture','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'{"ok":true}'}}}})
  result={'stopReason':os.environ.get('STOP','end_turn')}
 else:result={}
 emit({'id':message['id'],'result':result})
"""

MCP = r"""import json,os,sys
from pathlib import Path
Path(os.environ['TOKEN_CAPTURE']).write_text(os.environ['WISE_STEP_TOKEN'])
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m:continue
 result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
 print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)
"""


def fixture(tmp_path: Path, *, token: str = "secret-fixture") -> tuple[dict, Path, Path]:
    executable = tmp_path / "cursor-fixture"
    executable.write_text(f"#!{sys.executable}\n" + ACP)
    executable.chmod(0o700)
    server = tmp_path / "mcp.py"
    server.write_text(MCP)
    capture = tmp_path / f"{token}.jsonl"
    req = dict(
        cwd=str(tmp_path),
        mode="auto",
        auth="subscription",
        model="fixture-model",
        timeout_ms=10_000,
        prompt="Test",
        schema={"type": "object"},
        env={"CAPTURE": str(capture)},
        mcp_config={
            "mcpServers": {
                "wise-engine": {
                    "command": sys.executable,
                    "args": [str(server)],
                    "env": {"WISE_STEP_TOKEN": token, "TOKEN_CAPTURE": str(tmp_path / token)},
                }
            }
        },
    )
    return req, executable, capture


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.anyio
@pytest.mark.parametrize("model", [entry["id"] for entry in catalog_for("cursor")])
async def test_cursor_catalog_models_reach_both_transports(tmp_path: Path, model: str) -> None:
    req, executable, capture = fixture(tmp_path)
    req["model"] = model
    argv = cursor.build_argv(req)
    assert argv[argv.index("--model") + 1] == model
    handle = await cursor.start_cursor(req, lambda _: None, bin=str(executable))
    assert (await handle.done)["exit"] == "ok"
    argv = records(capture)[0]["argv"]
    assert argv[argv.index("--model") + 1] == model


@pytest.mark.anyio
async def test_acp_child_connects_without_token_argv_and_cleans_up(tmp_path: Path) -> None:
    req, executable, capture = fixture(tmp_path)
    events = []
    handle = await cursor.start_cursor(req, events.append, bin=str(executable))
    result = await handle.done
    assert result["exit"] == "ok"
    assert result["json"] == {"ok": True}
    assert result["cursor"] == "session-fixture"
    assert result["model"] == "fixture-model"
    assert result["usage"]["pool"] == "subscription"
    rows = records(capture)
    assert (tmp_path / "secret-fixture").read_text() == "secret-fixture"
    assert "--force" not in rows[0]["argv"] and "--approve-mcps" not in rows[0]["argv"]
    assert (
        next(row for row in rows if "permission_reply" in row)["permission_reply"]["result"][
            "outcome"
        ]["optionId"]
        == "no"
    )
    assert not Path(
        next(row["config_path"] for row in rows if "config_path" in row)
    ).parent.exists()
    assert events
    from wise_engine.channel import ChildTracker

    tracker = ChildTracker(step="step")
    for event in events:
        tracker.ingest(event)
    assert tracker.snapshot()["tool"] == "Read"
    assert tracker.snapshot()["text"] == '{"ok":true}'
    assert handle.snapshot()["tool_calls"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize("failure,expected", [("mcp", "error"), ("eof", "error"), ("auth", "auth")])
async def test_startup_failure_never_submits_prompt(
    tmp_path: Path, failure: str, expected: str
) -> None:
    req, executable, capture = fixture(tmp_path)
    req["env"]["FAIL"] = failure
    handle = await cursor.start_cursor(req, lambda _: None, bin=str(executable))
    assert (await handle.done)["exit"] == expected
    rows = records(capture)
    assert all(row.get("method") != "session/prompt" for row in rows)
    assert all(not Path(row["config_path"]).parent.exists() for row in rows if "config_path" in row)


@pytest.mark.anyio
async def test_concurrent_sessions_and_resume_keep_tokens_separate(tmp_path: Path) -> None:
    one, executable, first = fixture(tmp_path, token="first")
    two, _, second = fixture(tmp_path, token="second")
    two["resume"] = "prior-session"
    handles = await asyncio.gather(
        *(cursor.start_cursor(req, lambda _: None, bin=str(executable)) for req in [one, two])
    )
    results = await asyncio.gather(*(handle.done for handle in handles))
    assert all(result["exit"] == "ok" for result in results)
    assert all(result["text"] == '{"ok":true}' for result in results)
    assert results[1]["cursor"] == "prior-session"
    assert (tmp_path / "first").read_text() == "first"
    assert (tmp_path / "second").read_text() == "second"
    paths = [
        next(row["config_path"] for row in records(path) if "config_path" in row)
        for path in [first, second]
    ]
    assert paths[0] != paths[1]
    names = [
        next(row for row in records(path) if row.get("method") in ("session/new", "session/load"))[
            "params"
        ]["mcpServers"][0]["name"]
        for path in (first, second)
    ]
    assert names[0] != names[1]
    assert (
        next(row for row in records(second) if row.get("method") == "session/load")["params"][
            "sessionId"
        ]
        == "prior-session"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", [True, False])
async def test_cancel_or_timeout_reaps_provider_and_removes_secrets(
    tmp_path: Path, cancel: bool
) -> None:
    req, executable, capture = fixture(tmp_path)
    req["env"]["FAIL"] = "hang"
    req["timeout_ms"] = 10_000 if cancel else 300
    handle = await cursor.start_cursor(req, lambda _: None, bin=str(executable))
    async with asyncio.timeout(5):
        while not capture.exists() or not any(
            row.get("method") == "session/prompt" for row in records(capture)
        ):
            await asyncio.sleep(0.01)
    if cancel:
        handle.done.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handle.done
    else:
        assert (await handle.done)["exit"] == "timeout"
    assert handle.pid
    with pytest.raises(ProcessLookupError):
        os.kill(handle.pid, 0)
    assert all(
        not Path(row["config_path"]).parent.exists()
        for row in records(capture)
        if "config_path" in row
    )


@pytest.mark.anyio
async def test_spawn_exception_removes_private_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wise_engine.adapters import cursor_acp

    directory = tmp_path / "private"
    directory.mkdir()
    monkeypatch.setattr(cursor_acp.tempfile, "mkdtemp", lambda **_: str(directory))
    req, _, _ = fixture(tmp_path)
    handle = await cursor.start_cursor(req, lambda _: None, bin=str(tmp_path / "missing"))
    assert (await handle.done)["exit"] == "error"
    assert not directory.exists()


def test_explicit_engine_only_rejected_and_config_private(tmp_path: Path) -> None:
    req, _, _ = fixture(tmp_path)
    req["mcp_policy"] = "engine-only"
    with pytest.raises(ValueError, match="cannot isolate"):
        prepare_servers(req, tmp_path)
    req["mcp_policy"] = "inherit"
    servers, _ = prepare_servers(req, tmp_path)
    assert Path(servers[0]["args"][-1]).stat().st_mode & 0o777 == 0o600


def test_child_mcp_config_redacts_environment_values(tmp_path: Path) -> None:
    req, _, _ = fixture(tmp_path)
    directory = tmp_path / "private"
    directory.mkdir()
    servers, _ = prepare_servers(req, directory)
    try:
        config = Path(servers[0]["args"][-1]).read_text()
        assert "secret-fixture" not in config
        assert servers[0]["env"]
        assert servers[0]["env"][0]["value"] == "secret-fixture"
    finally:
        import shutil

        shutil.rmtree(directory)


@pytest.mark.parametrize(
    "title,kind,mode,expected",
    [
        ("wise-engine: wise_context", "other", "auto", "no"),
        ("wise-engine: get_status", "other", "auto", "yes"),
        ("`git status`", "execute", "auto", "yes"),
        ("`git push`", "execute", "auto", "no"),
        ("`git push`", "execute", "full-access", "yes"),
    ],
)
def test_permission_matcher(
    title: str, kind: str, mode: str, expected: str, tmp_path: Path
) -> None:
    params = dict(
        toolCall=dict(title=title, kind=kind),
        options=[dict(optionId="yes", kind="allow_once"), dict(optionId="no", kind="reject_once")],
    )
    result = permission_result(params, dict(mode=mode, cwd=str(tmp_path)), {})
    assert result["outcome"]["optionId"] == expected


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize("tool", ["wise_report", "wise_ask", "wise_context", "wise_checkpoint"])
def test_only_exact_injected_child_tools_are_granted(tmp_path: Path, tool: str) -> None:
    req, _, _ = fixture(tmp_path)
    for server, allowed in [("wise-engine", True), ("wise-engine-other", False)]:
        params = dict(
            toolCall=dict(title=f"{server}: {tool}", kind="other"),
            options=[
                dict(optionId="yes", kind="allow_once"),
                dict(optionId="no", kind="reject_once"),
            ],
        )
        assert permission_result(params, req, {})["outcome"]["optionId"] == (
            "yes" if allowed else "no"
        )


@pytest.mark.anyio
async def test_event_callback_failure_cleans_private_files(tmp_path: Path) -> None:
    req, executable, capture = fixture(tmp_path)

    def fail(_: dict) -> None:
        raise RuntimeError("callback failed")

    handle = await cursor.start_cursor(req, fail, bin=str(executable))
    assert (await handle.done)["exit"] == "error"
    assert all(row.get("method") != "session/prompt" for row in records(capture))
    with pytest.raises(ProcessLookupError):
        os.kill(handle.pid, 0)


@pytest.mark.anyio
@pytest.mark.parametrize("resume", [False, True])
async def test_native_cursor_acp_loads_child_mcp_without_prompt(
    tmp_path: Path, resume: bool
) -> None:
    import shutil
    import subprocess

    executable = shutil.which("cursor-agent")
    if executable is None:
        pytest.skip("Cursor CLI is not installed")
    config = tmp_path / "config"
    config.mkdir()
    source = (
        Path(
            os.environ.get("CURSOR_CONFIG_DIR")
            or str(
                Path(os.environ["XDG_CONFIG_HOME"]) / "cursor"
                if os.environ.get("XDG_CONFIG_HOME")
                else Path.home() / ".cursor"
            )
        )
        / "cli-config.json"
    )
    if source.is_file():
        shutil.copyfile(source, config / "cli-config.json")
        (config / "cli-config.json").chmod(0o600)
    env = {
        **os.environ,
        "CURSOR_CONFIG_DIR": str(config),
        "CURSOR_DATA_DIR": str(tmp_path / "data"),
        "DISABLE_AUTOUPDATER": "1",
    }
    status = subprocess.run(
        [executable, "status", "--format", "json"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if not json.loads(status.stdout or "{}").get("isAuthenticated"):
        pytest.skip("Cursor CLI is not authenticated")
    cursor_id = None
    if resume:
        created = await asyncio.create_subprocess_exec(
            executable,
            "create-chat",
            env=env,
            cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        assert created.stdout
        try:
            cursor_id = (await asyncio.wait_for(created.stdout.readline(), 20)).decode().strip()
            await asyncio.sleep(0.5)
        finally:
            import signal

            os.killpg(created.pid, signal.SIGKILL)
            await created.wait()
        assert cursor_id
        import sqlite3
        import uuid

    req, _, _ = fixture(tmp_path)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    servers, markers = prepare_servers(req, private)
    proc = await asyncio.create_subprocess_exec(
        executable,
        "acp",
        env=env,
        cwd=tmp_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    assert proc.stdin and proc.stdout

    async def request(identifier: int, method: str, params: dict) -> dict:
        assert proc.stdin and proc.stdout
        proc.stdin.write(
            (
                json.dumps(dict(jsonrpc="2.0", id=identifier, method=method, params=params)) + "\n"
            ).encode()
        )
        await proc.stdin.drain()
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), 45)
            assert line, "Cursor ACP closed before responding"
            message = json.loads(line)
            if message.get("id") == identifier:
                return message

    try:
        assert "result" in await request(
            1, "initialize", dict(protocolVersion=1, clientCapabilities={})
        )
        params = dict(cwd=str(tmp_path), mcpServers=servers)
        if cursor_id:
            import hashlib
            from wise_engine.adapters.cursor_acp import prepare_resume

            seed_id = str(uuid.uuid4())
            seed = config / "acp-sessions" / seed_id / "store.db"
            seed.parent.mkdir(parents=True)
            seed.touch()
            loaded = await request(
                2, "session/load", dict(cwd=str(tmp_path), mcpServers=[], sessionId=seed_id)
            )
            assert "result" in loaded, loaded
            assert "result" in await request(
                3, "session/set_mode", dict(sessionId=seed_id, modeId="ask")
            )
            cwd_hash = hashlib.md5(
                str(tmp_path.resolve()).encode(), usedforsecurity=False
            ).hexdigest()
            original = config / "chats" / cwd_hash / cursor_id / "store.db"
            original.parent.mkdir(parents=True)
            with sqlite3.connect(seed.as_uri() + "?mode=ro", uri=True) as native:
                with sqlite3.connect(original) as legacy:
                    native.backup(legacy)
            before = original.read_bytes()
            imported_id = prepare_resume(dict(cwd=str(tmp_path), resume=cursor_id), env)
            assert imported_id != cursor_id
            assert original.read_bytes() == before
            params["sessionId"] = imported_id
        result = await request(4, "session/load" if resume else "session/new", params)
        assert "result" in result, result.get("error")
        if resume:
            assert result["result"]["modes"]["currentModeId"] == "ask"
        assert all(marker.is_file() for marker in markers)
        assert (tmp_path / "secret-fixture").read_text() == "secret-fixture"
    finally:
        import signal

        os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()


def test_legacy_resume_snapshot_preserves_blobs_and_concurrent_wal(tmp_path: Path) -> None:
    import hashlib
    import sqlite3
    import uuid
    from wise_engine.adapters.cursor_acp import prepare_resume

    identifier = str(uuid.uuid4())
    cwd_hash = hashlib.md5(str(tmp_path.resolve()).encode(), usedforsecurity=False).hexdigest()
    source = tmp_path / "profile" / "chats" / cwd_hash / identifier / "store.db"
    source.parent.mkdir(parents=True)
    req = dict(cwd=str(tmp_path), resume=identifier)
    env = dict(CURSOR_CONFIG_DIR=str(tmp_path / "profile"))
    with sqlite3.connect(source) as writer:
        writer.execute("pragma journal_mode=wal")
        writer.execute("create table blobs(id text primary key, data blob)")
        writer.execute("insert into blobs values(?, ?)", ("history", b"existing conversation"))
        writer.commit()
        first = prepare_resume(req, env)
        second = prepare_resume(req, env)
        assert len({identifier, first, second}) == 3
        assert writer.execute("select data from blobs").fetchone()[0] == b"existing conversation"
    for cursor_id in (first, second):
        destination = tmp_path / "profile" / "acp-sessions" / cursor_id / "store.db"
        assert destination.stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(destination) as snapshot:
            assert (
                snapshot.execute("select data from blobs").fetchone()[0] == b"existing conversation"
            )
        assert prepare_resume(dict(cwd=str(tmp_path), resume=cursor_id), env) == cursor_id
    assert prepare_resume(dict(cwd=str(tmp_path), resume="../../outside"), env) == "../../outside"


@pytest.mark.anyio
async def test_imported_resume_returns_new_persistent_cursor(tmp_path: Path) -> None:
    import hashlib
    import sqlite3
    import uuid

    req, executable, capture = fixture(tmp_path)
    identifier = str(uuid.uuid4())
    root = tmp_path / "profile"
    path = (
        root
        / "chats"
        / hashlib.md5(str(tmp_path.resolve()).encode(), usedforsecurity=False).hexdigest()
        / identifier
        / "store.db"
    )
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as database:
        database.execute("create table history(text)")
        database.execute("insert into history values('kept')")
    req["resume"] = identifier
    req["allowed_tools"] = ["Bash(git push)"]
    req["env"]["CURSOR_CONFIG_DIR"] = str(root)
    handle = await cursor.start_cursor(req, lambda _: None, bin=str(executable))
    result = await handle.done
    assert result["exit"] == "ok"
    assert result["cursor"] != identifier
    assert any("allowed_tools" in warning for warning in result["warnings"])
    assert (root / "acp-sessions" / result["cursor"] / "store.db").exists()
    assert (
        next(row for row in records(capture) if row.get("method") == "session/load")["params"][
            "sessionId"
        ]
        == result["cursor"]
    )
