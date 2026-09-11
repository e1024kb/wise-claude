import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from wise_engine.auth import (
    LOGIN_CMDS,
    auth_required,
    bin_on_path,
    collect_needs,
    installed_harnesses,
    logged_out_harnesses,
    probe_harnesses,
    probe_one,
)
from wise_engine.preflight import build_questionary_with_auth
from wise_engine.rpc import RpcError, domain_code


@dataclass
class FakeAdapter:
    id: str = "claude"
    bin: str | None = None
    ok: bool = True
    error: Exception | None = None
    calls: list[Any] = field(default_factory=list)
    login: str | None = None

    async def probe_auth(self, auth):
        self.calls.append((self.id, auth))
        if self.error:
            raise self.error
        return {"ok": self.ok, **({"login_cmd": self.login} if self.login else {})}

    async def run(self, req, on_event):
        raise AssertionError("auth never starts a provider")

    def effort_map(self, effort):
        return None


def test_collect_needs_step_order_unique_pairs_and_unit_phases():
    definition = {
        "steps": [
            {"id": "skip", "type": "agent", "harness": "grok"},
            {"id": "a", "type": "agent"},
            {"id": "b", "type": "agent", "harness": "claude", "auth": "api-key"},
            {"id": "c", "type": "agent", "harness": "claude"},
            {"id": "d", "type": "bash"},
            {"id": "u", "type": "units", "harness": "grok"},
            {"id": "v", "type": "units", "harness": "gemini"},
            {"id": "w", "type": "units"},
        ]
    }
    resolved = {
        "a": {"harness": "codex"},
        "u.plan": {"harness": "codex"},
        "u.review": {"harness": "cursor"},
        "other.phase": {"harness": "grok"},
    }
    assert collect_needs(
        definition, {s["id"] for s in definition["steps"]} - {"skip"}, resolved
    ) == [
        {"harness": "codex", "auth": "subscription"},
        {"harness": "claude", "auth": "api-key"},
        {"harness": "claude", "auth": "subscription"},
        {"harness": "cursor", "auth": "subscription"},
        {"harness": "gemini", "auth": "subscription"},
    ]


def test_bin_on_path_checks_executable_regular_files(tmp_path, monkeypatch):
    binary = tmp_path / "cli"
    binary.write_text("#!/bin/sh\n")
    assert not bin_on_path(str(binary), {})
    binary.chmod(0o755)
    assert bin_on_path(str(binary), {})
    assert bin_on_path("cli", {"PATH": str(tmp_path)})
    assert not bin_on_path("absent", {"PATH": str(tmp_path)})
    assert not bin_on_path(str(tmp_path), {})
    monkeypatch.chdir(tmp_path)
    assert bin_on_path("cli", {"PATH": ""})


def test_installed_harnesses_respects_unlocked_defaults_and_binary_presence(tmp_path):
    definition = {
        "tuning": {
            "groups": [
                {"id": "x", "default": {"harness": "claude"}},
                {"id": "locked", "locked": True, "default": {"harness": "codex"}},
            ]
        }
    }
    installed = {h: FakeAdapter(id=h) for h in LOGIN_CMDS}
    installed["cursor"].bin = "missing-cli"
    assert installed_harnesses(definition, installed.get, {"PATH": str(tmp_path)}) == [
        "codex",
        "gemini",
        "grok",
    ]
    assert not any(a.calls for a in installed.values())
    assert installed_harnesses({}, installed.get) == []
    definition["tuning"]["groups"].append({"id": "different", "default": {"harness": "codex"}})
    assert installed_harnesses(definition, installed.get, {"PATH": str(tmp_path)}) == [
        "claude",
        "codex",
        "gemini",
        "grok",
    ]


def test_auth_probe_failure_shapes_and_first_failure_order():
    async def run():
        calls = []
        adapters = {
            "claude": FakeAdapter(calls=calls),
            "codex": FakeAdapter(id="codex", ok=False, calls=calls, login="custom login"),
            "grok": FakeAdapter(id="grok", calls=calls),
        }
        needs = [{"harness": h, "auth": "subscription"} for h in adapters]
        with pytest.raises(RpcError) as raised:
            await probe_harnesses(needs, adapters.get)
        assert domain_code(raised.value) == "AUTH_REQUIRED"
        assert raised.value.data == dict(
            code="AUTH_REQUIRED", harness="codex", login_cmd="custom login"
        )
        assert "subscription" in str(raised.value)
        assert calls == [("claude", "subscription"), ("codex", "subscription")]
        assert await probe_one("cursor", "subscription", adapters.get) == dict(
            ok=False, login_cmd="cursor-agent login", detail="no adapter in this build"
        )
        adapters["codex"].error = RuntimeError("broken probe")
        assert await probe_one("codex", "subscription", adapters.get) == dict(
            ok=False, login_cmd="codex login", detail="broken probe"
        )
        with pytest.raises(RpcError, match="broken probe"):
            await probe_harnesses(needs, adapters.get)
        with pytest.raises(RpcError, match="no adapter"):
            await probe_harnesses([dict(harness="cursor", auth="subscription")], adapters.get)
        assert await logged_out_harnesses(["claude", "codex", "cursor"], adapters.get) == [
            "codex",
            "cursor",
        ]

    asyncio.run(run())
    assert str(auth_required("grok", "grok login")) == "grok: not logged in; run `grok login`"


def test_questionary_auth_probes_only_harness_stage_and_includes_defaults():
    async def run():
        calls = []
        adapters = {
            "claude": FakeAdapter(calls=calls, ok=False),
            "codex": FakeAdapter(id="codex", calls=calls),
        }
        definition = {
            "name": "x",
            "version": 2,
            "steps": [{"id": "a", "type": "agent", "prompt": "x", "tuning": "author"}],
            "tuning": {
                "groups": [{"id": "author", "default": {"harness": "claude", "model": "haiku"}}]
            },
        }
        asked = await build_questionary_with_auth(
            definition, {"harnesses": ["codex"]}, {}, adapters.get
        )
        assert calls == [("codex", "subscription"), ("claude", "subscription")]
        question = next(q for q in asked["questions"] if q["id"] == "harness.author")
        assert "not logged in" in question["options"][0]["description"]
        calls.clear()
        await build_questionary_with_auth(
            definition, {"harnesses": ["codex"]}, {"harness.author": "codex"}, adapters.get
        )
        assert calls == []
        await build_questionary_with_auth(definition, {}, {}, adapters.get)
        assert calls == []

    asyncio.run(run())
