import asyncio
import io
import json
from pathlib import Path

from wise_engine.cli import Io, main, parse_args

ROOT = Path(__file__).parents[2]


def invoke(*args, env=None):
    out, err = io.StringIO(), io.StringIO()
    code = asyncio.run(main(args, Io(out.write, err.write, {} if env is None else env)))
    return code, out.getvalue(), err.getvalue()


def test_compile_bundled_and_missing():
    paths = sorted(str(p) for p in (ROOT / "workflows").glob("*/workflow.yaml"))
    code, out, err = invoke("compile-check", *paths)
    assert code == 0 and err == ""
    rows = json.loads(out)
    assert len(rows) == 5 and all(r["ok"] for r in rows)
    code, out, err = invoke("compile-check", "missing")
    assert code == 1 and json.loads(out)[0]["issues"] == [
        {"level": "error", "path": "", "message": "workflow not found"}
    ]
    assert invoke("compile-check")[0] == 64


def test_compile_v1_and_text(tmp_path):
    old = tmp_path / "old.yaml"
    old.write_text("name: old\nversion: 1\nsteps:\n  - id: a\n    type: prompt\n    prompt: x\n")
    code, out, _ = invoke("compile-check", str(old))
    assert code == 1 and not json.loads(out)[0]["ok"]
    code, out, _ = invoke("compile-check", str(old), "--text")
    assert code == 1 and "FAIL old" in out and "ERROR" in out


def test_listing_user_roots_and_flags(tmp_path):
    user = tmp_path / "user"
    user.mkdir()
    (user / "custom.yaml").write_text("name: custom\nversion: 2\nsteps: []\n")
    code, out, _ = invoke(
        "list-defs", "--user-root", str(user), "--bundled-root", str(ROOT / "workflows")
    )
    rows = json.loads(out)
    assert code == 0 and any(r["name"] == "custom" and r["source"] == "user" for r in rows)
    assert any(r["source"] == "bundled" for r in rows)
    code, out, _ = invoke("compile-check", "custom", "--user-root=" + str(user), "--text")
    assert code == 1 and "custom" in out
    assert parse_args(["list-defs", "--json", "--text"])["flags"] == {"json": True, "text": True}


def test_help_version_models_and_unknown():
    assert invoke()[0] == 0 and invoke("help")[0] == 0
    assert invoke("no-command")[0] == 64
    code, out, _ = invoke("version")
    assert code == 0 and out.startswith("wise-engine ") and "(python " in out
    code, out, _ = invoke("models", "codex")
    assert code == 0 and all(r["harness"] == "codex" for r in json.loads(out))
    assert invoke("models", "unknown")[0] == 2
    code, out, _ = invoke("models", "grok", "--text")
    assert code == 0 and "\t-\t" in out


def test_migration_cli_dry_run_write_backup_and_v2(tmp_path):
    source = tmp_path / "workflow.yaml"
    original = "# retain this guidance\nname: demo\nversion: 1\nsteps:\n  - id: a\n    type: prompt\n    prompt: hello\n"
    source.write_text(original)
    code, out, err = invoke("migrate", str(source))
    assert code == 0, err
    result = json.loads(out)
    assert result["dry_run"] and not result["already_v2"] and result["notes"]
    assert source.read_text() == original
    code, out, err = invoke("migrate", str(source), "--write")
    assert code == 0, err
    assert Path(str(source) + ".v1.bak").read_text() == original
    assert "# retain this guidance" in source.read_text()
    result = json.loads(out)
    assert not result["dry_run"] and result["written"] == [str(source)]
    code, out, err = invoke("migrate", str(source))
    assert code == 0, err
    assert json.loads(out)["already_v2"]
    assert invoke("migrate")[0] == 64 and invoke("migrate", "absent.yaml")[0] == 2


def test_preflight_shape_stages_and_context(monkeypatch):
    import wise_engine.cli as cli

    monkeypatch.setattr(cli, "adapter_lookup", lambda _: None)
    groups = [
        "analyze-design",
        "research-context",
        "codebase-audit",
        "gap-analysis",
        "build-plan",
        "refine-plan",
        "implement",
    ]
    answers = {
        "step-select": ["analyze-design", "analyze-related", "research-context", "gap-analysis"],
        **{"harness." + group: "claude" for group in groups},
        "permissions.claude": "auto",
        "model.analyze-design": "claude-sonnet-5",
    }
    workflow = str(ROOT / "workflows/ticket-plan/workflow.yaml")
    code, out, err = invoke("preflight", workflow, "--answers", json.dumps(answers))
    assert code == 0, err
    result = json.loads(out)
    assert result["workflow"] == "ticket-plan" and result["version"] == 2
    assert [q["id"] for q in result["questions"] if not q["id"].startswith("input.")][:2] == [
        "effort.analyze-design",
        "model.research-context",
    ]
    assert result["defaults"]["effort.analyze-design"] == "medium"
    code, out, err = invoke(
        "preflight", workflow, "--context", '{"ticket":[{"ref":"A"},{"ref":"B"}]}'
    )
    assert code == 0, err
    assert (
        next(q for q in json.loads(out)["questions"] if q["id"].startswith("input."))["default"]
        == "A, B"
    )
    assert invoke("preflight", workflow, "--answers", "{nope")[0] == 64
    assert invoke("preflight", workflow, "--context", "{nope")[0] == 64
    assert invoke("preflight", "absent")[0] == 2
    assert invoke("preflight")[0] == 64
    code, out, err = invoke("preflight", workflow, "--text", "--answers", json.dumps(answers))
    assert code == 0 and "ticket-plan v2" in out and "effort.analyze-design" in out


def test_auth_no_installed_harnesses_and_dispatch_usage():
    code, out, err = invoke("auth", "--json", env={"PATH": ""})
    rows = json.loads(out)
    assert code == 1 and err == ""
    assert all(not row["installed"] and row["login"] == "missing" for row in rows)
    assert any(
        row["harness"] == "cursor" and row["login_cmd"] == "cursor-agent login" for row in rows
    )
    assert invoke("auth", "unknown")[0] == 2
    assert invoke("auth", "codex", env={"PATH": ""})[0] == 1
    assert invoke("dispatch")[0] == 64


def test_captured_cli_help_unknown_and_missing_preflight():
    fixtures = ROOT / "engine/test/fixtures/contracts/cli.json"
    for case in json.loads(fixtures.read_text()):
        if case["args"][0] == "compile-check":
            continue
        code, out, err = invoke(*case["args"])
        additions = (
            "  refresh-host --host <host> --plugin-root <path>  refresh an existing registration\n",
            "  setup-host --host <host> --plugin-root <path> [--apply]  preview or repair registration\n",
            "  host-doctor --host <host>    inspect launch registration (does not prove host connectivity)\n",
            "  host-rollback <transaction>  restore setup files if they have not changed\n",
            "  definition-roots             canonical user and bundled definition directories\n",
            "  list-agents                  bundled role roster for workflow authors\n",
        )
        out = out.replace(
            "exit 1 when a checked provider is missing or logged out",
            "exit 1 when claude is missing or logged out",
        )
        err = err.replace(
            "exit 1 when a checked provider is missing or logged out",
            "exit 1 when claude is missing or logged out",
        )
        for before, after in [
            (
                "wait|status|answer|cancel|resume|report|nudge",
                "wait|status|answer|cancel|resume|report",
            ),
            ("used by managed host registration", "used by .mcp.json"),
        ]:
            out, err = out.replace(before, after), err.replace(before, after)
        for line in additions:
            out, err = out.replace(line, ""), err.replace(line, "")
        assert (code, out, err) == (case["code"], case["out"], case["err"])


def test_mcp_help_goes_to_stderr_without_start():
    for command in ("mcp", "unit-mcp"):
        code, out, err = invoke(command, "--help")
        assert code == 0 and out == "" and f"wise-engine {command}" in err
    assert invoke("daemon", "help")[0] == 0


def test_authoring_roots_and_roster(tmp_path):
    code, out, err = invoke("definition-roots", env={"WISE_DATA_DIR": str(tmp_path)})
    assert code == 0 and not err
    assert json.loads(out) == {
        "user_root": str(tmp_path / "workflows/definitions"),
        "bundled_root": str(ROOT / "workflows"),
    }
    code, out, err = invoke("list-agents")
    assert code == 0 and not err
    rows = json.loads(out)
    assert len(rows) == 13 and any(row["name"] == "software-engineer" for row in rows)


def test_auth_uses_cursor_adapter_binary_and_selected_provider_exit(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import wise_engine.cli as cli
    import wise_engine.auth as auth

    binary = tmp_path / "cursor-agent"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    monkeypatch.setattr(cli, "adapter_lookup", lambda name: SimpleNamespace(bin="cursor-agent"))
    calls = []

    async def probe(harness, pool, lookup):
        calls.append(harness)
        return {"ok": True, "login_cmd": "cursor-agent login"}

    monkeypatch.setattr(auth, "probe_one", probe)
    code, out, err = invoke("auth", "cursor", "--json", env={"PATH": str(tmp_path)})
    assert code == 0 and not err
    assert json.loads(out)[0]["installed"] is True
    assert calls == ["cursor"]
    for harness in ("claude", "codex", "cursor", "gemini", "grok"):
        code, out, err = invoke("auth", harness, "--json", env={"PATH": ""})
        assert code == 1 and not json.loads(out)[0]["installed"]
    assert calls == ["cursor"]


def test_new_host_control_help_is_explicit():
    code, out, err = invoke("help")
    assert code == 0 and not err
    for command in ("refresh-host", "setup-host", "host-doctor", "host-rollback", "nudge"):
        assert command in out
    assert "managed host registration" in out


def test_auth_selected_logged_out_provider_fails_without_claude(tmp_path, monkeypatch):
    import wise_engine.auth as auth
    import wise_engine.defs as defs

    monkeypatch.setattr(defs, "on_path", lambda *args: True)

    async def probe(harness, pool, lookup):
        assert harness == "codex"
        return {"ok": False, "login_cmd": "codex login"}

    monkeypatch.setattr(auth, "probe_one", probe)
    code, out, err = invoke("auth", "codex", "--json")
    assert code == 1 and not err
    assert json.loads(out) == [
        {"harness": "codex", "installed": True, "login": "missing", "login_cmd": "codex login"}
    ]


def test_refresh_host_route_uses_existing_registration(monkeypatch, tmp_path):
    import wise_engine.host_setup as setup

    seen = []

    def refresh(**kwargs):
        seen.append(kwargs)
        return {"refreshed": True, "transaction": None, "plugin_root": kwargs["plugin_root"]}

    monkeypatch.setattr(setup, "refresh_existing", refresh)
    code, out, err = invoke(
        "refresh-host",
        "--host",
        "cursor",
        "--plugin-root",
        "/loaded/plugin",
        env={"HOME": str(tmp_path)},
    )
    assert code == 0 and not err and json.loads(out)["refreshed"]
    assert seen == [
        {"plugin_root": "/loaded/plugin", "host": "cursor", "home": str(tmp_path), "config": None}
    ]
