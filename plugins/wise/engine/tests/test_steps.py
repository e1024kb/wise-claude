from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from wise_engine.adapter_types import AgentHandle
from wise_engine.ledger import empty_usage, log_paths
from wise_engine.paths import PLUGIN_ROOT
from wise_engine.rpc import RpcError
from wise_engine.steps.agent import (
    DEFAULT_STEP_TIMEOUT_MS,
    build_run_req,
    extract_outputs,
    headline,
    outcome_of,
    start_agent_step,
)
from wise_engine.steps.bash import run_bash_step
from wise_engine.steps.gate import build_gate, decide_gate, is_gate_step


def agent_params(tmp_path: Path) -> dict[str, Any]:
    return dict(
        step=dict(id="work", type="agent", prompt="Do work"),
        resolved=dict(harness="claude", model="sonnet", effort="high"),
        cwd=str(tmp_path),
        run_dir=str(tmp_path),
        step_run_id="attempt",
        step_token="token",
    )


def result(**kwargs: Any) -> dict[str, Any]:
    return dict(exit="ok", text="", usage=empty_usage(), warnings=[], **kwargs)


def test_headline() -> None:
    assert headline("\n \n  hello\t  world  \nsecond") == "hello world"
    assert headline("x" * 300) == "x" * 199 + "…"
    assert headline(" \n\t") == ""


def test_build_request_defaults_and_overrides(tmp_path: Path) -> None:
    params = agent_params(tmp_path)
    params["step"].update(schema={"type": "object"}, max_turns=4, allowed_tools=["Read"])
    req = build_run_req(params)
    assert req == dict(
        prompt="Do work",
        model="sonnet",
        cwd=str(tmp_path),
        mode="auto",
        timeout_ms=DEFAULT_STEP_TIMEOUT_MS,
        auth="subscription",
        step_token="token",
        add_dirs=[str(tmp_path), str(PLUGIN_ROOT)],
        effort="high",
        schema={"type": "object"},
        max_turns=4,
        allowed_tools=["Read"],
    )
    params.update(cursor="session", add_dirs=["/extra"], default_timeout_ms=12)
    for resume in (None, "fresh", "unit"):
        params["step"].update(mode="plan", auth="api", timeout=90)
        if resume:
            params["step"]["resume"] = resume
        req = build_run_req(params)
        assert req["timeout_ms"] == 90000
        assert req["mode"] == "plan" and req["auth"] == "api"
        assert req["add_dirs"][-1] == "/extra"
        assert req.get("resume") == ("session" if resume == "unit" else None)
    del params["step"]["timeout"]
    assert build_run_req(params)["timeout_ms"] == 12
    for policy in ("engine-only", "inherit"):
        params["step"]["mcp"] = policy
        assert build_run_req(params)["mcp_policy"] == policy
    for harness, effort in (("gemini", "high"), ("claude", "")):
        params["resolved"].update(harness=harness, effort=effort)
        assert "effort" not in build_run_req(params)


def test_child_channel_uses_python_runtime(tmp_path: Path) -> None:
    params = agent_params(tmp_path)
    params["channel"] = dict(socket_path="/socket", data_root="/data", engine_root="/engine")
    server = build_run_req(params)["mcp_config"]["mcpServers"]["wise-engine"]
    assert server["args"] == ["-m", "wise_engine", "unit-mcp"]
    assert Path(server["command"]).is_file()
    assert server["env"] == dict(
        PYTHONPATH="/engine",
        WISE_STEP_TOKEN="token",
        WISE_ENGINE_SOCKET="/socket",
        WISE_DATA_ROOT="/data",
    )


def test_outputs_and_outcomes() -> None:
    assert extract_outputs(["a", "b"], {"a": 1, "b": None, "c": 3}) == {
        "outputs": {"a": 1, "b": None}
    }
    assert extract_outputs(["a", "b"], {"a": 1}) == {"outputs": {"a": 1}, "missing": "b"}
    assert extract_outputs(["a"], None) == {"outputs": {}, "missing": "a"}
    assert extract_outputs(None, {"a": 1}) == {"outputs": {}}
    good = outcome_of({"outputs": ["answer"]}, result(json={"answer": "yes"}, cursor="session"))
    assert good["ok"] and good["outputs"] == {"answer": "yes"} and good["cursor"] == "session"
    missing = outcome_of({"outputs": ["answer"]}, result(json={}))
    assert missing["exit"] == "missing_output" and missing["outputs"] == {}
    assert missing["error"] == "schema result lacks answer"
    assert outcome_of({}, result())["verdict"] == "ok"
    for exit in ("error", "rate_limited", "auth", "timeout", "max_turns"):
        failed = outcome_of({}, {**result(), "exit": exit})
        assert not failed["ok"] and failed["exit"] == exit and failed["error"] == exit
    assert outcome_of({}, {**result(), "text": "Done\nmore"})["verdict"] == "Done"


def test_agent_stream_logs(tmp_path: Path) -> None:
    async def run() -> None:
        params = agent_params(tmp_path)
        events = [
            {
                "parsed": {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "Read"}]},
                },
                "line": "one",
            },
            {"line": "two"},
        ]
        seen: list[dict[str, Any]] = []

        async def starter(harness, req, on_event):
            assert harness == "claude" and req["prompt"] == "Do work"
            for event in events:
                on_event(event)
            done = asyncio.get_running_loop().create_future()
            done.set_result({**result(), "text": "x" * 6000})
            return AgentHandle(done=done)

        params.update(starter=starter, on_event=seen.append)
        started = await start_agent_step(params)
        assert (await started.outcome)["ok"]
        paths = log_paths(tmp_path, "work", "attempt")
        assert [json.loads(line) for line in Path(paths["raw"]).read_text().splitlines()] == events
        log = Path(paths["log"]).read_text()
        assert "tools: Read" in log and "chars elided" in log and len(log) < 6000
        assert seen == events

    asyncio.run(run())


@pytest.mark.parametrize(
    "script,outputs,ok,verdict",
    [
        ("printf ' first\\n last\\n'", ["a", "b"], True, "last"),
        ("printf 'bad\\nlast error\\n' >&2; exit 2", ["a"], False, "failed: last error"),
        ("exit 7", [], False, "failed: exit code 7"),
        ("true", [], True, "ok"),
    ],
)
def test_bash_results(tmp_path: Path, script, outputs, ok, verdict) -> None:
    answer = asyncio.run(
        run_bash_step(dict(id="sh", run=script, outputs=outputs), {"cwd": str(tmp_path)})
    )
    assert answer["ok"] is ok and answer["verdict"] == verdict
    assert answer["outputs"] == ({"a": "first\n last"} if ok and outputs else {})


def test_bash_timeout_and_clean_environment(tmp_path: Path) -> None:
    start = time.monotonic()
    answer = asyncio.run(
        run_bash_step(
            dict(id="sh", run="sleep 20"), dict(cwd=str(tmp_path), default_timeout_ms=150)
        )
    )
    assert answer["timed_out"] and not answer["ok"] and time.monotonic() - start < 5
    assert answer["error"] == "timed out after 150 ms"
    answer = asyncio.run(
        run_bash_step(
            dict(id="sh", run='printf "%s|%s|%s|%s" "$PWD" "$CLAUDECODE" "$MY_SECRET" "$PATH"'),
            dict(
                cwd=str(tmp_path),
                parent_env={"PATH": os.environ["PATH"], "CLAUDECODE": "1", "MY_SECRET": "hidden"},
            ),
        )
    )
    assert answer["stdout"] == f"{tmp_path}|||{os.environ['PATH']}"


def test_gates() -> None:
    approval = dict(id="ship", type="approval", message=" Ship? \n")
    assert is_gate_step(approval) and not is_gate_step({"type": "bash"})
    assert build_gate(approval, "g") == dict(
        gate_id="g",
        step="ship",
        kind="approval",
        message="Ship?",
        options=[{"value": "approve", "label": "Approve"}, {"value": "reject", "label": "Reject"}],
    )
    assert decide_gate(approval, " approve ") == dict(status="completed", verdict="approved")
    assert decide_gate(approval, ["reject"]) == dict(status="failed", verdict="rejected")
    with pytest.raises(RpcError, match="takes"):
        decide_gate(approval, "yes")
    ask: dict[str, Any] = dict(id="focus", type="ask", message="Where?", options=["x", "y"])
    assert build_gate(ask, "g")["options"] == [
        {"value": "x", "label": "x"},
        {"value": "y", "label": "y"},
    ]
    assert decide_gate(ask, "x")["output"] == dict(name="focus", value="x")
    for value in ("", "z"):
        with pytest.raises(RpcError, match="takes"):
            decide_gate(ask, value)
    ask.update(allow_text=True, output="next_focus")
    assert decide_gate(ask, ["x", "y"])["output"] == dict(name="next_focus", value="x, y")
    free = dict(id="free", type="ask", message="Say")
    assert "options" not in build_gate(free, "g")
    assert decide_gate(free, "anything")["output"] == dict(name="free", value="anything")
