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


class ListingAdapter:
    def __init__(self, identifier, bin=None, rows=None, error=None):
        self.id = identifier
        self.bin = bin
        self.rows = rows or []
        self.error = error
        self.calls = 0

    async def list_models(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.rows


def test_models_json_text_and_invalid_harness():
    io, out, err = output()
    assert asyncio.run(cmd_models([], {"catalog-only": True}, io)) == 0
    rows = json.loads("".join(out))
    assert any(row["harness"] == "claude" and row["id"] == "claude-fable-5-1" for row in rows)
    assert any(row["harness"] == "claude" and row["id"] == "claude-fable-5" for row in rows)
    assert all(row["source"] == "catalog" for row in rows)
    assert all(not row["efforts"] for row in rows if row["harness"] == "grok")
    out.clear()
    assert asyncio.run(cmd_models(["codex"], {"text": True, "catalog-only": True}, io)) == 0
    assert "codex\tgpt-6-astra\t" in "".join(out) and "\tcatalog\t" in "".join(out)
    out.clear()
    assert asyncio.run(cmd_models(["unknown"], {}, io)) == 2
    assert "unknown harness unknown" in "".join(err)
    assert out == []


def test_models_appends_harness_reported_rows_deterministically(tmp_path):
    binary = tmp_path / "grok"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    reported = [
        dict(id="grok-5", label="grok-5", description="reported", efforts=[]),
        dict(id="grok-4.6", label="dup of catalog", description="reported", efforts=[]),
        dict(id="grok-4.4", label="grok-4.4", description="reported", efforts=[]),
        dict(id="grok-5", label="dup", description="reported", efforts=[]),
    ]
    adapters = {
        "grok": ListingAdapter("grok", "grok", reported),
        "cursor": ListingAdapter("cursor", "cursor-agent", [dict(id="x", label="x", efforts=[])]),
        "codex": ListingAdapter("codex", "codex", error=RuntimeError("boom")),
    }
    env = {"PATH": str(tmp_path)}
    io, out, _ = output()
    assert asyncio.run(cmd_models(["grok", "cursor", "codex"], {}, io, env, adapters.get)) == 0
    rows = json.loads("".join(out))
    # catalog first, then the extras in the order the harness listed them
    assert [(r["id"], r["source"]) for r in rows if r["harness"] == "grok"] == [
        ("grok-4.6", "catalog"),
        ("grok-4.5", "catalog"),
        ("grok-5", "harness"),
        ("grok-4.4", "harness"),
    ]
    assert next(r for r in rows if r["id"] == "grok-5")["label"] == "grok-5"
    assert adapters["grok"].calls == 1
    assert adapters["cursor"].calls == 0, "cursor-agent is not on PATH"
    assert adapters["codex"].calls == 0
    assert all(r["source"] == "catalog" for r in rows if r["harness"] != "grok")
    out.clear()
    assert asyncio.run(cmd_models(["grok"], {"catalog-only": True}, io, env, adapters.get)) == 0
    assert [r["id"] for r in json.loads("".join(out))] == ["grok-4.6", "grok-4.5"]
    assert adapters["grok"].calls == 1


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
        assert len(adapter.seen) == 1
        request = adapter.seen[0]
        assert request.pop("system").startswith("# Repository instruction contract")
        assert request == dict(
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
