import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from test_executor import Rig
from wise_engine import bootstrap, host_setup, ledger
from wise_engine.migrate import MigrationError, migrate_file
from wise_engine.paths import ENGINE_ROOT, PLUGIN_ROOT


def file_bytes(root):
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("status", ["running", "paused", "gated"])
def test_copied_v2_run_continues_without_repeating_completed_effects(tmp_path, status):
    async def scenario():
        definitions = tmp_path / "definitions"
        definitions.mkdir()
        (definitions / "rehearsal.yaml").write_text("""version: 2
name: rehearsal
preflight: {control-mode: interactive, worktree: current}
steps:
  - id: prepare
    type: bash
    run: 'printf once >> prepared'
  - id: approve
    type: approval
    depends_on: [prepare]
    message: Continue after upgrade?
  - id: finish
    type: bash
    depends_on: [approve]
    run: 'printf finished > result'
""")
        options = dict(roots={"user_root": str(definitions), "bundled_root": str(definitions)})
        old = Rig(tmp_path, **options)
        try:
            run = await old.conduct(
                "rehearsal", context={"ticket": [{"ref": "UPGRADE", "body": "Retained context 🙂"}]}
            )
            state = await old.status(run["run_id"], "gated", "failed")
            assert state["status"] == "gated", state
            directory = Path(old.rt.require_run_dir(run["run_id"]))
            ledger.write_unit(
                directory,
                "upgrade/unit",
                {"last_phase": "review", "cursors": {"review": "retained-session"}},
            )
            if status != "gated":
                ledger.update_run(directory, {"status": status, "gate": None})
                ledger.update_step(
                    directory,
                    "approve",
                    {"status": "running" if status == "running" else "pending"},
                )
            saved_state = ledger.read_state(directory)
            before_events = ledger.read_events(directory)
            before_context = file_bytes(directory / "context")
            assert before_context
            backup = tmp_path / "upgrade-snapshot"
            shutil.copytree(directory, backup)
        finally:
            await old.close()
        shutil.rmtree(directory)
        shutil.copytree(backup, directory)
        assert ledger.read_state(directory) == saved_state
        fresh = Rig(tmp_path, **options)
        try:
            if status == "running":
                ledger.reset_running(directory)
                assert fresh.executor.pick_up() == [run["run_id"]]
            elif status == "paused":
                assert fresh.executor.pick_up() == []
                await fresh.executor.resume({"run_id": run["run_id"]})
            else:
                assert fresh.executor.pick_up() == []
            gated = await fresh.status(run["run_id"], "gated", "failed")
            assert gated["status"] == "gated", gated
            fresh.executor.answer(
                {"run_id": run["run_id"], "gate_id": gated["gate"]["gate_id"], "value": "approve"}
            )
            final = await fresh.status(run["run_id"], "completed", "failed")
            assert final["status"] == "completed", final
            assert (Path(fresh.cwd) / "prepared").read_text() == "once"
            assert (Path(fresh.cwd) / "result").read_text() == "finished"
            assert final["context"] == saved_state["context"]
            assert file_bytes(directory / "context") == before_context
            assert ledger.read_unit(directory, "upgrade/unit")["cursors"] == {
                "review": "retained-session"
            }
            assert ledger.read_events(directory)[: len(before_events)] == before_events
            assert file_bytes(backup) != file_bytes(directory)
            assert json.loads((backup / "state.json").read_text()) == saved_state
            assert not fresh.adapter.calls
        finally:
            await fresh.close()

    asyncio.run(scenario())


def test_v1_import_rehearsal_preserves_original_history_and_backup(tmp_path):
    legacy = tmp_path / "legacy history"
    legacy.mkdir()
    (legacy / "state.yaml").write_text(
        "version: 1\nrun_id: retained\nsteps: {a: {status: completed}}\n"
    )
    (legacy / "events.jsonl").write_text('{"type":"step.completed","step":"a"}\n')
    (legacy / "checkpoint.json").write_text('{"cursor":"old-session"}')
    history = file_bytes(legacy)
    source = ENGINE_ROOT / "test/fixtures/migrate/example-workflow.v1.yaml"
    imported = tmp_path / "definition.yaml"
    shutil.copyfile(source, imported)
    original = imported.read_bytes()
    assert migrate_file(imported)["dry_run"]
    assert imported.read_bytes() == original
    converted = tmp_path / "converted.yaml"
    assert migrate_file(imported, out=converted)["ok"]
    assert imported.read_bytes() == original
    assert migrate_file(imported, write=True)["ok"]
    backup = imported.with_name(imported.name + ".v1.bak")
    assert backup.read_bytes() == original
    imported.write_bytes(original)
    assert migrate_file(imported, write=True)["ok"]
    assert backup.read_bytes() == original
    second = imported.read_bytes()
    assert migrate_file(imported, write=True)["already_v2"]
    assert imported.read_bytes() == second
    for unsupported in (legacy, legacy / "state.yaml"):
        with pytest.raises(MigrationError, match="UNSUPPORTED_V1_RUN"):
            migrate_file(unsupported, write=True)
    renamed = tmp_path / "renamed-run.yaml"
    shutil.copyfile(legacy / "state.yaml", renamed)
    with pytest.raises(MigrationError, match="UNSUPPORTED_V1_RUN"):
        migrate_file(renamed, out=tmp_path / "must-not-exist.yaml")
    assert not (tmp_path / "must-not-exist.yaml").exists()
    assert file_bytes(legacy) == history


def test_readonly_install_restarts_through_managed_launcher_without_history_changes():
    with tempfile.TemporaryDirectory(prefix="wu-", dir="/tmp") as root:
        readonly_launcher_rehearsal(Path(root))


def readonly_launcher_rehearsal(tmp_path):
    plugin = tmp_path / "cache spaces 🙂/wise"
    shutil.copytree(
        ENGINE_ROOT / "wise_engine",
        plugin / "engine/wise_engine",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copyfile(ENGINE_ROOT / "engine.sh", plugin / "engine/engine.sh")
    shutil.copyfile(ENGINE_ROOT / "requirements.txt", plugin / "engine/requirements.txt")
    (plugin / ".claude-plugin").mkdir()
    shutil.copyfile(
        PLUGIN_ROOT / ".claude-plugin/plugin.json", plugin / ".claude-plugin/plugin.json"
    )
    data = tmp_path / "data"
    target = bootstrap.environment_path(plugin / "engine/requirements.txt", data)
    (target / "bin").mkdir(parents=True)
    (target / "bin/python").write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    (target / "bin/python").chmod(0o755)
    (target / ".ready.json").write_text(
        json.dumps({"key": bootstrap.environment_key(plugin / "engine/requirements.txt")})
    )
    host_setup.apply_plan(
        host_setup.plan_setup(
            plugin_root=plugin, home=tmp_path, host="codex", python=sys.executable
        )
    )
    run = tmp_path / "wise/runs/history/01FIXTURE"
    run.mkdir(parents=True)
    captured = json.loads((ENGINE_ROOT / "test/fixtures/contracts/ledger.json").read_text())[
        "running"
    ]
    captured["status"] = "paused"
    (run / "state.json").write_text(json.dumps(captured))
    (run / "events.jsonl").write_text('{"seq":1,"type":"upgrade-history"}\n')
    history = file_bytes(run)
    source = file_bytes(plugin)
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path),
        "WISE_PYTHON": sys.executable,
        "CLAUDE_PLUGIN_DATA": str(data),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.pop("WISE_PLUGIN_ROOT", None)
    launcher = tmp_path / ".local/share/wise/bin/wise-engine"

    def invoke(*args):
        result = subprocess.run(
            [str(launcher), "--wise-host", "codex", *args],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    paths = list(plugin.rglob("*")) + [plugin]
    try:
        for path in paths:
            path.chmod(0o555 if path.is_dir() else 0o444)
        assert "python" in invoke("version")
        for _ in range(2):
            invoke("daemon", "start")
            assert "running" in invoke("daemon", "status")
            assert "paused" in invoke("status", "01FIXTURE", "--json")
            invoke("daemon", "stop")
        assert file_bytes(run) == history
        assert file_bytes(plugin) == source
        assert not list(plugin.rglob("__pycache__"))
    finally:
        subprocess.run(
            [str(launcher), "--wise-host", "codex", "daemon", "stop"],
            env=env,
            capture_output=True,
            timeout=15,
        )
        for path in paths:
            path.chmod(0o755 if path.is_dir() else 0o644)
