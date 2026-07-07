"""Regression tests for PLAN-005 (workflows.py robustness batch):

- `save_yaml` / `insights.compact_ledger` write via a unique per-call tmp
  file + `os.replace`, not a fixed sibling name (no torn/interleaved writes).
- `cmd_init_state` / `cmd_write_log` reject malformed or path-traversing
  step ids instead of raising a bare `KeyError` or writing outside
  `<run-dir>/logs/`.
- `installed_plugins()`'s filesystem fallback reads the plugin's own
  `name` from `plugin.json` rather than the containing directory (which,
  in the cache layout, is the version string).

`insights.py` is not the `workflows_module` conftest fixture — it is
loaded here via its own `importlib` spec, same pattern conftest.py uses
for `workflows.py`.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
INSIGHTS_PATH = REPO / "plugins" / "wise" / "scripts" / "insights.py"

_spec = importlib.util.spec_from_file_location("insights", INSIGHTS_PATH)
assert _spec is not None, f"cannot load insights module from {INSIGHTS_PATH}"
assert _spec.loader is not None, f"no loader for insights module at {INSIGHTS_PATH}"
insights = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(insights)


# ---------- save_yaml: atomic per-writer tmp -------------------------------


def test_save_yaml_leaves_no_tmp_sibling_and_parses(workflows_module, tmp_path):
    path = tmp_path / "state.yaml"
    workflows_module.save_yaml(path, {"a": 1})
    workflows_module.save_yaml(path, {"a": 2})

    assert workflows_module.load_yaml(path) == {"a": 2}
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_yaml_uses_unique_tmp_name_per_call(workflows_module, tmp_path, monkeypatch):
    path = tmp_path / "state.yaml"
    seen_sources: list[str] = []
    real_replace = workflows_module.os.replace

    def spy_replace(src, dst):
        seen_sources.append(str(src))
        return real_replace(src, dst)

    monkeypatch.setattr(workflows_module.os, "replace", spy_replace)

    workflows_module.save_yaml(path, {"a": 1})
    workflows_module.save_yaml(path, {"a": 2})

    assert len(seen_sources) == 2
    assert seen_sources[0] != seen_sources[1]


# ---------- insights.compact_ledger: atomic per-writer tmp -----------------


def test_compact_ledger_leaves_no_tmp_sibling_and_writes_records(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    insights.compact_ledger({"s1": {"session_id": "s1", "n": 1}})
    insights.compact_ledger({"s1": {"session_id": "s1", "n": 2}})

    ledger = insights.ledger_path()
    lines = ledger.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["n"] == 2
    assert list(ledger.parent.glob("*.tmp")) == []


def test_compact_ledger_uses_unique_tmp_name_per_call(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    seen_sources: list[str] = []
    real_replace = insights.os.replace

    def spy_replace(src, dst):
        seen_sources.append(str(src))
        return real_replace(src, dst)

    monkeypatch.setattr(insights.os, "replace", spy_replace)

    insights.compact_ledger({"s1": {"session_id": "s1"}})
    insights.compact_ledger({"s1": {"session_id": "s1"}})

    assert len(seen_sources) == 2
    assert seen_sources[0] != seen_sources[1]


# ---------- init-state: step-id + required-key validation ------------------


def _def_yaml(workflows_module, tmp_path, steps):
    def_path = tmp_path / "workflow.yaml"
    workflows_module.save_yaml(def_path, {"name": "demo", "version": 1, "steps": steps})
    return def_path


def test_init_state_rejects_traversal_step_id(workflows_module, wise_env, tmp_path):
    def_path = _def_yaml(workflows_module, tmp_path, [{"id": "../evil", "type": "bash"}])
    run_dir = tmp_path / "run-1"
    ctx = json.dumps({"claude_session_id": None, "session_label": None})

    rc = workflows_module.cmd_init_state(str(def_path), str(run_dir), "run-1", ctx)

    assert rc != 0
    assert not (run_dir / "state.yaml").is_file()


def test_init_state_rejects_missing_id_or_type(workflows_module, wise_env, tmp_path):
    ctx = json.dumps({"claude_session_id": None, "session_label": None})
    run_dir = tmp_path / "run-1"

    def_path_no_id = _def_yaml(workflows_module, tmp_path, [{"type": "bash"}])
    assert workflows_module.cmd_init_state(str(def_path_no_id), str(run_dir), "run-1", ctx) != 0

    def_path_no_type = _def_yaml(workflows_module, tmp_path, [{"id": "a"}])
    assert workflows_module.cmd_init_state(str(def_path_no_type), str(run_dir), "run-1", ctx) != 0


# ---------- write-log: refuses to write outside <run-dir>/logs/ -----------


def test_write_log_refuses_traversing_step_id(workflows_module, tmp_path):
    run_dir = tmp_path / "run-1"
    rc = workflows_module.cmd_write_log(str(run_dir), "../evil", "01ABCDEFGH")

    assert rc != 0
    assert not (run_dir / "logs").exists()


def test_write_log_refuses_traversing_step_run_id(workflows_module, tmp_path):
    run_dir = tmp_path / "run-1"
    rc = workflows_module.cmd_write_log(str(run_dir), "valid-step", "../evil")

    assert rc != 0
    assert not (run_dir / "logs").exists()


# ---------- installed_plugins(): cache-layout fallback ---------------------


def test_installed_plugins_reads_plugin_json_name_not_version_dir(
    workflows_module, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    plugin_dir = home / ".claude" / "plugins" / "cache" / "m" / "p" / "1.2.3" / ".claude-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(json.dumps({"name": "p"}))
    monkeypatch.setattr(workflows_module, "HOME", home)

    assert workflows_module.installed_plugins() == {"p"}


def test_installed_plugins_falls_back_to_dir_name_when_json_unparseable(
    workflows_module, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    plugin_dir = home / ".claude" / "plugins" / "cache" / "m" / "p" / "1.2.3" / ".claude-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text("not json")
    monkeypatch.setattr(workflows_module, "HOME", home)

    assert workflows_module.installed_plugins() == {"1.2.3"}
