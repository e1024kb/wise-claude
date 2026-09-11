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
