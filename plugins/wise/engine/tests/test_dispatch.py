import asyncio
import json
from dataclasses import dataclass, field

import pytest

from wise_engine.dispatch import DispatchIo, cmd_dispatch, cmd_models


@dataclass
class FakeAdapter:
    result: dict = field(default_factory=dict)
    seen: list = field(default_factory=list)

    async def run(self, req, on_event):
        self.seen.append(req)
        on_event(dict(harness="fake", line="activity"))
        return (
            dict(
                text="all green, merged",
                usage={"input": 1, "output": 2, "cache_read": 0, "cache_write": 0},
                exit="ok",
            )
            | self.result
        )


def output():
    stdout, stderr = [], []
    return DispatchIo(stdout.append, stderr.append), stdout, stderr


def test_models_json_text_and_invalid_harness():
    io, out, err = output()
    assert cmd_models([], {}, io) == 0
    rows = json.loads("".join(out))
    assert any(row["harness"] == "claude" and row["id"] == "claude-fable-5-1" for row in rows)
    assert all(not row["efforts"] for row in rows if row["harness"] == "grok")
    out.clear()
    assert cmd_models(["codex"], {"text": True}, io) == 0
    assert "codex\tgpt-6-astra\t" in "".join(out)
    out.clear()
    assert cmd_models(["unknown"], {}, io) == 2
    assert "unknown harness unknown" in "".join(err)
    assert out == []


def test_dispatch_request_and_result(tmp_path):
    async def run():
        prompt = tmp_path / "prompt.md"
        prompt.write_text("watch the PR")
        io, out, err = output()
        adapter = FakeAdapter()
        code = await cmd_dispatch(
            {
                "harness": "codex",
                "model": "gpt-6-astra",
                "effort": "high",
                "mode": "full-access",
                "prompt-file": str(prompt),
                "cwd": str(tmp_path),
                "timeout-s": "60",
                "allowed-tools": "Bash(git:*),Bash(gh:*),",
                "add-dir": "/extra",
            },
            io,
            lambda h: adapter,
        )
        assert code == 0 and not err
        assert adapter.seen == [
            dict(
                prompt="watch the PR",
                model="gpt-6-astra",
                effort="high",
                mode="full-access",
                cwd=str(tmp_path),
                timeout_ms=60000,
                auth="subscription",
                allowed_tools=["Bash(git:*)", "Bash(gh:*)"],
                add_dirs=["/extra"],
            )
        ]
        result = json.loads("".join(out))
        assert result["ok"] and result["harness"] == "codex"
        assert result["verdict"] == "all green, merged"

    asyncio.run(run())


@pytest.mark.parametrize(
    "flags,message,code",
    [
        ({}, "--harness", 64),
        ({"harness": "vim"}, "--harness", 64),
        ({"harness": "claude"}, "--prompt-file", 64),
        ({"harness": "claude", "prompt": " "}, "prompt is empty", 64),
        ({"harness": "claude", "prompt-file": "/missing-prompt"}, "cannot read prompt", 66),
        ({"harness": "grok", "prompt": "p", "effort": "high"}, "takes no effort flag", 64),
        ({"harness": "codex", "prompt": "p", "effort": "typo"}, "--effort", 64),
        ({"harness": "claude", "prompt": "p", "mode": "typo"}, "--mode", 64),
        ({"harness": "claude", "prompt": "p", "timeout-s": "0"}, "positive number", 64),
        ({"harness": "claude", "prompt": "p", "timeout-s": "-1"}, "positive number", 64),
        ({"harness": "claude", "prompt": "p", "timeout-s": "NaN"}, "positive number", 64),
        ({"harness": "claude", "prompt": "p", "timeout-s": "Infinity"}, "positive number", 64),
    ],
)
def test_dispatch_usage_errors_do_not_start_provider(flags, message, code):
    async def run():
        io, out, err = output()
        assert (
            await cmd_dispatch(flags, io, lambda h: pytest.fail("provider must not start")) == code
        )
        assert message in "".join(err)
        assert out == []

    asyncio.run(run())


@pytest.mark.parametrize("text", [False, True])
def test_dispatch_off_catalog_failed_child_and_output_mode(text):
    async def run():
        io, out, err = output()
        adapter = FakeAdapter(
            result=dict(
                exit="timeout", error="deadline", warnings=["child warning"], text="partial text"
            )
        )
        code = await cmd_dispatch(
            {"harness": "claude", "model": "claude-nova-9", "prompt": "p", "text": text},
            io,
            lambda h: adapter,
        )
        assert code == 1
        assert adapter.seen[0]["model"] == "claude-nova-9"
        if text:
            assert out == ["partial text\n"]
            assert err == ["dispatch: child exit timeout: deadline\n"]
        else:
            result = json.loads("".join(out))
            assert result["exit"] == "timeout" and not result["ok"]
            assert "not in the catalog" in result["warnings"][0]
            assert result["warnings"][1] == "child warning"

    asyncio.run(run())


def test_dispatch_catalog_alias_and_provider_reported_model():
    async def run():
        io, out, _ = output()
        adapter = FakeAdapter(result=dict(model="provider-picked", text=""))
        assert (
            await cmd_dispatch(
                {
                    "harness": "claude",
                    "model": "haiku",
                    "prompt": "inline",
                    "prompt-file": "/ignored",
                },
                io,
                lambda h: adapter,
            )
            == 0
        )
        assert adapter.seen[0]["model"] == "claude-haiku-4-5"
        assert adapter.seen[0]["prompt"] == "inline"
        assert json.loads("".join(out))["model"] == "provider-picked"

    asyncio.run(run())
