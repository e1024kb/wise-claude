from __future__ import annotations

import asyncio
import copy
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import wise_engine.cli_client as cli
from wise_engine.client import connect, stop_daemon
from wise_engine.daemon import start_daemon
from wise_engine.rpc import RpcError, domain_error

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


QUESTIONS = [
    {
        "id": "permissions.claude",
        "kind": "choice",
        "label": "Minimum permissions?",
        "options": [
            {"value": "auto", "label": "Auto (recommended)"},
            {"value": "approval-required", "label": "Approval required"},
        ],
        "default": "auto",
    },
    {
        "id": "model.plan",
        "kind": "choice",
        "label": "Which model?",
        "options": [{"value": "opus", "label": "Opus"}, {"value": "sonnet", "label": "Sonnet"}],
        "default": "opus",
    },
    {
        "id": "tuning.plan",
        "kind": "choice",
        "label": "Plan tuning",
        "default": "default",
        "locked": True,
    },
    {"id": "input.ticket", "kind": "text", "label": "Ticket ref?"},
    {"id": "input.notes", "kind": "text", "label": "Notes?", "optional": True},
]
APPROVAL = {
    "gate_id": "g1",
    "step": "approve",
    "kind": "approval",
    "message": "Ship it?",
    "options": [{"value": "approve", "label": "Approve"}, {"value": "reject", "label": "Reject"}],
}
SUMMARY = {
    "run_id": "01RUN",
    "workflow": "wf",
    "status": "running",
    "started_at": "2026-09-05T10:00:00.000Z",
    "last_activity_at": "2026-09-05T10:00:01.000Z",
    "cwd": "/tmp/x",
}
USAGE = {"input": 1500, "output": 200, "cache_read": 0, "cache_write": 0, "pool": "subscription"}
REPORT = {
    "units": [],
    "usage": {
        "subscription": USAGE,
        "api-key": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "pool": "api-key"},
        "by_harness": {"claude": USAGE},
        "by_step": {"classify": USAGE},
    },
    "usage_total": USAGE,
    "resolved": {"classify": {"harness": "claude", "model": "haiku", "effort": ""}},
    "verdicts": {"classify": "bug"},
}


def event(seq, type, **extra):
    return {
        "seq": seq,
        "ts": f"2026-09-05T10:00:0{seq}.000Z",
        "run_id": "01RUN",
        "type": type,
        **extra,
    }


async def invoke(argv, *, env=None, stdin=""):
    output, errors = [], []
    code = await cli.client_command(
        argv,
        SimpleNamespace(
            out=output.append,
            err=errors.append,
            env={} if env is None else env,
            stdin=io.StringIO(stdin),
        ),
    )
    return SimpleNamespace(
        code=code, out="".join(output), err="".join(errors), lines="".join(output).splitlines()
    )


@pytest.fixture
async def fake(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="wc-", dir="/tmp") as root:
        state = SimpleNamespace(
            calls=[],
            waits=[],
            fail={},
            preflight={
                "workflow": "wf",
                "version": 2,
                "questions": copy.deepcopy(QUESTIONS),
                "defaults": {},
                "requires_missing": [],
            },
            root=root,
        )

        def handler(method):
            def call(params, ctx):
                state.calls.append((method, params))
                if method in state.fail:
                    raise state.fail[method]
                if method == "preflight":
                    return state.preflight
                if method == "run":
                    return {"run_id": "01RUN", "status": "running"}
                if method == "wait":
                    return state.waits.pop(0)
                if method == "answer":
                    return {"accepted": True}
                if method == "status":
                    return (
                        {**SUMMARY, "run_id": params["run_id"]}
                        if params.get("run_id")
                        else [SUMMARY]
                    )
                if method == "cancel":
                    return {"status": "cancelled"}
                if method == "resume":
                    return {"run_id": params["run_id"], "status": "running"}
                if method == "report":
                    return REPORT

            return call

        daemon = await start_daemon(
            data_root=root,
            env={},
            version="test",
            handlers={method: handler(method) for method in ("preflight", *cli.CLIENT_COMMANDS)},
        )

        async def connector(**options):
            return await connect(**{**options, "version": "test"})

        monkeypatch.setattr(cli, "connect", connector)
        monkeypatch.setattr(cli, "ensure_daemon", connector)

        async def run(argv, stdin=""):
            return await invoke([*argv, "--data-root", root, "--no-start"], stdin=stdin)

        state.run = run
        state.method = lambda method: [params for name, params in state.calls if name == method]
        try:
            yield state
        finally:
            await daemon.close()


async def test_usage_help_missing_and_bad_arguments(fake):
    for argv in (
        ["unknown"],
        ["run"],
        ["answer", "x"],
        ["wait"],
        ["run", "wf", "--answers", "{bad"],
        ["run", "wf", "--answers", "[]"],
        ["run", "wf", "--input", "missing"],
        ["wait", "r", "--after", "-1"],
    ):
        assert (await fake.run(argv)).code == 64
    assert not fake.calls
    for argv in (["run", "--help"], ["help"], ["--help"], ["status", "--h"]):
        result = await fake.run(argv)
        assert result.code == 0 and result.out.startswith("wise-engine")
    assert not fake.calls


async def test_missing_answers_prevents_run(fake):
    result = await fake.run(["run", "wf"])
    assert result.code == 64
    error = json.loads(result.out)["error"]
    assert error["code"] == "MISSING_ANSWERS"
    assert error["missing"] == ["input.ticket"]
    assert not fake.method("run")
    result = await fake.run(["run", "wf", "--text"])
    assert "input.ticket" in result.err and result.out == ""


async def test_interactive_asks_every_open_question_and_eof(fake):
    result = await fake.run(["run", "wf", "--interactive"], "\n2\nREF\n\n")
    assert result.code == 0, result.err + result.out
    assert all(
        label in result.err
        for label in ("Minimum permissions?", "Which model?", "Ticket ref?", "Notes?")
    )
    assert fake.method("run")[0]["answers"] == {
        "permissions.claude": "auto",
        "model.plan": "sonnet",
        "input.ticket": "REF",
        "input.notes": "",
    }
    assert fake.method("run")[0]["inputs"] == {"ticket": "REF", "notes": ""}
    assert len(fake.method("preflight")) == 5
    fake.calls.clear()
    result = await fake.run(["run", "wf", "--interactive"])
    assert result.code == 64 and "PREFLIGHT_UNANSWERED" in result.out
    assert not fake.method("run")


async def test_answers_inputs_defaults_and_staged_preflight(fake):
    result = await fake.run(
        [
            "run",
            "wf",
            "--cwd",
            "/tmp/x",
            "--answers",
            '{"model.plan":"sonnet"}',
            "--input",
            "ticket=REF",
            "--context",
            '{"guidance":"brief"}',
        ]
    )
    assert result.code == 0
    params = fake.method("run")[0]
    assert params == {
        "workflow": "wf",
        "cwd": "/tmp/x",
        "answers": {"model.plan": "sonnet", "input.ticket": "REF", "permissions.claude": "auto"},
        "inputs": {"ticket": "REF"},
        "context": {"guidance": "brief"},
    }
    assert len(fake.method("preflight")) == 2
    assert "permissions.claude" not in fake.method("preflight")[0]["answers"]
    assert json.loads(result.out)["run_id"] == "01RUN"
    fake.calls.clear()
    result = await fake.run(["run", "wf", "--answers", '{"input.ticket":"X","input.notes":"n"}'])
    assert result.code == 0
    assert fake.method("run")[0]["answers"]["input.notes"] == "n"


@pytest.mark.parametrize("text", [False, True])
async def test_follow_events_gate_answer_cursor_and_done(fake, text):
    fake.waits = [
        {
            "events": [
                event(1, "run.started"),
                event(2, "step.done", step="classify", verdict="bug"),
            ],
            "status": "gated",
            "gate": APPROVAL,
            "done": False,
        },
        {
            "events": [event(3, "gate.answered", step="approve"), event(4, "run.done")],
            "status": "completed",
            "done": True,
        },
    ]
    result = await fake.run(
        [
            "run",
            "--follow",
            "wf",
            "--input",
            "ticket=X",
            "--timeout-ms",
            "5000",
            *(["--text"] if text else []),
        ],
        "approve\n",
    )
    assert result.code == 0, result.err + result.out
    assert fake.method("answer") == [{"run_id": "01RUN", "gate_id": "g1", "value": "approve"}]
    assert [params["after"] for params in fake.method("wait")] == [0, 2]
    assert fake.method("wait")[0]["timeout_ms"] == 5000
    if text:
        assert "10:00:02  step.done     classify  bug" in result.lines
        assert "GATE g1 [approval] step approve" in result.lines
        assert result.lines[-1] == "run completed"
    else:
        rows = [json.loads(line) for line in result.lines]
        assert rows[0]["run_id"] == "01RUN"
        assert rows[-1] == {"done": True, "run_id": "01RUN", "status": "completed"}


async def test_follow_free_text_failed_repeated_gate_and_unanswered(fake):
    ask = {**APPROVAL, "kind": "ask", "gate_id": "g2", "options": [], "allow_text": True}
    fake.waits = [
        {"events": [], "status": "gated", "gate": ask, "done": False},
        {"events": [], "status": "gated", "gate": ask, "done": False},
        {"events": [], "status": "failed", "done": True},
    ]
    result = await fake.run(["run", "wf", "--follow", "--input", "ticket=X"], "free form\n")
    assert result.code == 1
    assert len(fake.method("answer")) == 1
    assert fake.method("answer")[0]["value"] == "free form"
    fake.calls.clear()
    fake.waits = [{"events": [], "status": "gated", "gate": APPROVAL, "done": False}]
    result = await fake.run(["run", "wf", "--follow", "--text", "--input", "ticket=X"])
    assert result.code == 1
    assert "wise-engine answer 01RUN g1 <value>" in result.err
    assert not fake.method("answer")


async def test_follow_invalid_answer_reprompts(fake):
    gate = {
        **APPROVAL,
        "kind": "ask",
        "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}],
    }
    fake.waits = [
        {"events": [], "status": "gated", "gate": gate, "done": False},
        {"events": [], "status": "completed", "done": True},
    ]
    result = await fake.run(
        ["run", "wf", "--follow", "--text", "--input", "ticket=X"], "bad\n\nb\n"
    )
    assert result.code == 0 and "expected one of: a, b" in result.out
    assert fake.method("answer")[0]["value"] == "b"


async def test_wait_bounds_and_direct_commands(fake):
    fake.waits = [
        {
            "events": [event(7, "step.started", step="plan", model="opus", harness="claude")],
            "status": "running",
            "done": False,
        },
        {"events": [], "status": "gated", "gate": APPROVAL, "done": False},
    ]
    result = await fake.run(["wait", "01RUN", "--after", "6", "--timeout-ms", "1000"])
    assert result.code == 0
    assert fake.method("wait")[0] == {"run_id": "01RUN", "after": 6, "timeout_ms": 1000}
    result = await fake.run(["wait", "01RUN", "--text", "--timeout-ms", "999999"])
    assert "GATE g1" in result.out and "status: gated" in result.out
    assert fake.method("wait")[1]["timeout_ms"] == 600000
    assert json.loads((await fake.run(["status"])).out) == [SUMMARY]
    assert (await fake.run(["status", "OTHER", "--text"])).out.startswith("OTHER  wf  running")
    assert (
        await fake.run(["answer", "01RUN", "g1", "approve", "--text"])
    ).out == "answer accepted for g1\n"
    assert json.loads((await fake.run(["cancel", "01RUN", "--reason", "changed"])).out) == {
        "status": "cancelled"
    }
    assert fake.method("cancel")[0]["reason"] == "changed"
    assert (await fake.run(["resume", "01RUN", "--text"])).out == "run 01RUN running\n"
    report = await fake.run(["report", "01RUN", "--text"])
    assert report.code == 0 and "classify: bug" in report.out
    assert "claude haiku" in report.out and "1.5k" in report.out and "api-key" not in report.out
    assert json.loads((await fake.run(["report", "01RUN"])).out) == REPORT


async def test_domain_rpc_and_auth_errors(fake):
    fake.fail["cancel"] = domain_error("RUN_NOT_FOUND", "no such run", {"run_id": "nope"})
    result = await fake.run(["cancel", "nope"])
    assert result.code == 2
    assert json.loads(result.out) == {
        "error": {"code": "RUN_NOT_FOUND", "message": "no such run", "run_id": "nope"}
    }
    result = await fake.run(["cancel", "nope", "--text"])
    assert result.code == 2 and result.out == "" and "ERROR RUN_NOT_FOUND" in result.err
    fake.fail["preflight"] = domain_error(
        "AUTH_REQUIRED", "claude needs login", {"harness": "claude", "login_cmd": "claude login"}
    )
    result = await fake.run(["run", "wf", "--text"])
    assert (
        result.code == 1 and result.err == "ERROR AUTH_REQUIRED: claude needs login\nclaude login\n"
    )
    assert json.loads((await fake.run(["run", "wf"])).out)["error"]["login_cmd"] == "claude login"
    fake.fail["status"] = RpcError(-32603, "bad")
    assert (await fake.run(["status"])).code == 70


async def test_dead_socket_no_start():
    with tempfile.TemporaryDirectory(prefix="wcc-", dir="/tmp") as root:
        for text in (False, True):
            result = await invoke(
                ["status", "--data-root", root, "--no-start", *(["--text"] if text else [])]
            )
            assert result.code == 69
            assert "/wise-init" in result.out + result.err
        assert not list(Path(root).iterdir())


async def test_custom_socket_and_default_autostart():
    with tempfile.TemporaryDirectory(prefix="wca-", dir="/tmp") as root:
        try:
            result = await invoke(
                ["status", "--data-root", root, "--socket", root + "/custom.sock"]
            )
            assert result.code == 0, result.out + result.err
            assert json.loads(result.out) == []
            assert Path(root, "custom.sock").exists()
            again = await invoke(
                ["status", "--data-root", root, "--socket", root + "/custom.sock", "--no-start"]
            )
            assert again.code == 0
        finally:
            assert (
                await stop_daemon(
                    data_root=root, socket_path=root + "/custom.sock", env={}, now=True
                )
            )["stopped"]


async def test_question_parsing_optional_multiselect_defaults_and_invalids():
    output = []
    terminal = SimpleNamespace(err=output.append)
    options = [
        {"value": "a", "label": "Alpha", "description": "first"},
        {"value": "b", "label": "Beta"},
    ]
    assert await cli.read_question(
        {"id": "x", "kind": "multi", "label": "Pick", "options": options},
        cli.LineSource(io.StringIO("bad\n1,Beta,1\n")),
        terminal,
    ) == ["a", "b"]
    assert "comma-separated" in "".join(output)
    assert (
        await cli.read_question(
            {"id": "x", "kind": "multi", "label": "Pick", "options": options},
            cli.LineSource(io.StringIO("none\n")),
            terminal,
        )
        == []
    )
    assert (
        await cli.read_question(
            {"id": "x", "kind": "text", "label": "Text"},
            cli.LineSource(io.StringIO("\n yes \n")),
            terminal,
        )
        == "yes"
    )
    assert "A value is required." in "".join(output)
    assert (
        await cli.read_question(
            {"id": "x", "kind": "choice", "label": "Pick", "options": options},
            cli.LineSource(io.StringIO("bad\nALPHA\n")),
            terminal,
        )
        == "a"
    )


async def test_line_source_pipe_is_lazy_and_cancellable():
    import os

    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "r")
    source = cli.LineSource(stream)
    try:
        assert source._reader is None
        pending = asyncio.create_task(source.next())
        await asyncio.sleep(0.01)
        os.write(write_fd, "héllo\n".encode())
        assert await pending == "héllo"
        pending = asyncio.create_task(source.next())
        await asyncio.sleep(0.01)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        source.close()
    finally:
        source.close()
        stream.close()
        os.close(write_fd)


def test_formats_and_fill_answers():
    assert (
        cli.format_event(event(2, "step.done", step="classify", verdict="bug"))
        == "10:00:02  step.done     classify  bug"
    )
    assert (
        cli.format_event(event(3, "usage", usage={"input": 1500, "output": 2}))
        == "10:00:03  usage           in 1.5k out 2"
    )
    assert (
        cli.format_event(event(4, "phase.done", unit="x", phase="build"))
        == "10:00:04  phase.done    x  phase build"
    )
    assert cli.fill_answers(QUESTIONS, {})["missing"] == ["input.ticket"]
    assert "tuning.plan" not in cli.fill_answers(QUESTIONS, {})["answers"]
    assert cli.money({"cost_usd": 0.125, "cost_source": "priced"}) == "~$0.13"
    assert cli.parse_args(["run", "--follow", "wf", "--input", "a=1", "--input=b=2"])[
        "positional"
    ] == ["wf"]
