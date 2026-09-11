import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from wise_engine import ledger as l
from wise_engine.paths import cwd_slug, plugin_data_root, runs_root, wise_data_root

STAMP = "2026-01-01T00:00:00Z"


def demo(path, **kwargs):
    return l.init_state(
        run_dir=path,
        run_id=path.name,
        workflow={"name": "demo", "version": 2, "dir": "/demo"},
        step_ids=["a", "b"],
        now=STAMP,
        **kwargs,
    )


def saved(path, status, **kwargs):
    l.write_state(
        path,
        dict(
            run_id=path.name,
            workflow={"name": "demo"},
            status=status,
            last_activity_at=STAMP,
            **kwargs,
        ),
    )
    return path


def test_clock_and_ulid():
    assert l.utc_now(datetime.fromisoformat("2026-05-01T12:34:56.789Z")) == "2026-05-01T12:34:56Z"
    ids = [l.new_ulid(t) for t in [1000, 1000, 2000, 500]]
    assert all(re.fullmatch("[0-9A-HJKMNP-TV-Z]{26}", i) for i in ids)
    assert ids == sorted(set(ids))
    assert ids[0][:10] == ids[1][:10]


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 25), ("abc", 25), ("0", 25), ("-3", 25), ("7", 7), (" +7 ", 7), ("1.5", 25)],
)
def test_env_positive_int(raw, expected):
    assert l.env_positive_int("X", 25, {} if raw is None else {"X": raw}) == expected


@pytest.mark.parametrize(
    "stamp,seconds,expected",
    [
        (None, 1800, False),
        ("bad", 1800, False),
        (STAMP, 1800, True),
        (STAMP, 1799, False),
        ("2026-01-01T01:00:00Z", 1800, True),
    ],
)
def test_session_fresh(stamp, seconds, expected):
    now = datetime.fromisoformat("2026-01-01T00:30:00Z").timestamp() * 1000
    assert l.session_is_fresh(stamp, seconds, now) == expected


def test_roots(tmp_path):
    assert wise_data_root({"XDG_DATA_HOME": "/xdg"}) == Path("/xdg/wise")
    assert wise_data_root({"HOME": "/home/u"}) == Path("/home/u/.local/share/wise")
    assert plugin_data_root({"CLAUDE_PLUGIN_DATA": "/cpd", "WISE_DATA_DIR": "/wdd"}) == Path("/cpd")
    assert plugin_data_root({"WISE_DATA_DIR": "/wdd"}) == Path("/wdd")
    assert plugin_data_root({"HOME": "/home/u"}) == Path("/home/u/.local/share/wise")
    assert runs_root(
        tmp_path, {"XDG_DATA_HOME": str(tmp_path)}
    ) == tmp_path / "wise/runs" / cwd_slug(tmp_path)
    assert "/" not in cwd_slug(tmp_path)


def test_state_lifecycle(tmp_path):
    state = demo(tmp_path, harness_session="s")
    assert state["version"] == 2 and state["status"] == "initializing"
    assert state["run_id"] == tmp_path.name and state["workflow"]["name"] == "demo"
    assert state["harness_session"] == "s" and state["profile"] == "medium"
    assert state["steps"] == {
        "a": {"status": "pending", "attempts": 0},
        "b": {"status": "pending", "attempts": 0},
    }
    assert state["outputs"] == {} and (tmp_path / "logs").is_dir()
    assert l.read_state(tmp_path) == state
    state = l.start_run(
        tmp_path,
        {
            "answers": {"control-mode": "wave-sync"},
            "inputs": {"ticket": "TEST-1"},
            "project": {"kind": "backend"},
            "provider_permissions": {"claude": "auto", "cursor": "full-access"},
        },
        now="2099-01-01T00:00:00Z",
    )
    assert state["status"] == "running" and state["project"]["kind"] == "backend"
    assert state["answers"]["control-mode"] == "wave-sync"
    assert state["outputs"] == state["inputs"] == {"ticket": "TEST-1"}
    assert state["provider_permissions"] == {"claude": "auto", "cursor": "full-access"}
    assert state["last_activity_at"] == "2099-01-01T00:00:00Z"
    l.update_step(tmp_path, "a", {"status": "running"})
    assert l.read_state(tmp_path)["steps"]["a"]["status"] == "running"
    with pytest.raises(l.LedgerError) as error:
        l.update_step(tmp_path, "missing", {})
    assert error.value.code == "NO_SUCH_STEP"
    l.record_output(tmp_path, "pr_url", "https://example/1")
    assert l.read_state(tmp_path)["outputs"]["pr_url"] == "https://example/1"
    l.update_step(tmp_path, "b", {"status": "completed"})
    first = l.start_step(tmp_path, "a")
    l.update_step(tmp_path, "a", {"completed_at": STAMP, "error": "old", "verdict": "bad"})
    second = l.start_step(tmp_path, "a")
    assert second != first
    step = l.read_state(tmp_path)["steps"]["a"]
    assert step["step_run_id"] == second and step["attempts"] == 2
    assert not {"completed_at", "error", "verdict"} & step.keys()
    l.update_run(tmp_path, {"status": "paused"})
    state = l.reset_running(tmp_path)
    assert state["status"] == "running" and state["steps"]["b"]["status"] == "completed"
    assert state["steps"]["a"]["status"] == "pending"
    assert not {"started_at", "step_run_id"} & state["steps"]["a"].keys()
    state = l.update_run(tmp_path, {"status": "completed", "completed_at": STAMP}, now=STAMP)
    assert state["last_activity_at"] == state["completed_at"] == STAMP
    assert json.loads(l.dump_state(tmp_path)) == state
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "ids,code",
    [(["Bad"], "INVALID_STEP_ID"), (["a", "a"], "DUPLICATE_STEP_ID"), (["a\n"], "INVALID_STEP_ID")],
)
def test_invalid_step_ids(tmp_path, ids, code):
    with pytest.raises(l.LedgerError) as error:
        l.init_state(run_dir=tmp_path, run_id="r", workflow={}, step_ids=ids)
    assert error.value.code == code


def test_profile_output_roundtrip(tmp_path):
    demo(tmp_path)
    values = {
        "run_profile": "low",
        "tuning_step_gap_analysis": "sonnet / high",
        "team_mode": "solo",
        "cap_max_review_cycles": "2",
    }
    for key, value in values.items():
        l.record_output(tmp_path, key, value)
    assert l.read_state(tmp_path)["outputs"] == values


def test_listing_and_sessions(tmp_path):
    saved(tmp_path / "failed", "failed", harness_session="s")
    saved(tmp_path / "completed", "completed", harness_session="s")
    saved(tmp_path / "other", "running", harness_session="t")
    l.update_run(tmp_path / "other", {}, now="2026-02-01T00:00:00Z")
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken/state.json").write_text("{not json")
    (tmp_path / "orphan").mkdir()
    rows = l.list_runs(tmp_path)
    assert [r["run_id"] for r in rows] == ["broken", "completed", "failed", "other"]
    assert rows[0] == {
        "run_id": "broken",
        "status": "<unreadable>",
        "workflow": "",
        "last_activity_at": "",
    }
    assert "RUN ID" in l.format_runs_table(rows)
    assert l.format_runs_table([]) == "(no runs in this workspace yet)"
    assert [r["run_id"] for r in l.list_resumable_runs(tmp_path)] == ["other", "failed"]
    rows = l.find_runs_by_session(
        tmp_path, "s", {}, datetime.fromisoformat(STAMP).timestamp() * 1000
    )
    assert len(rows) == 1 and rows[0]["fresh"]
    assert l.format_session_run_row(rows[0]) == f"failed\tdemo\tfailed\t{STAMP}\tfresh"
    assert not l.find_runs_by_session(
        tmp_path, "s", {}, datetime.fromisoformat("2026-02-01T00:00:00Z").timestamp() * 1000
    )[0]["fresh"]


@pytest.mark.parametrize(
    "status,protected,terminal,cap,deleted",
    [
        ("running", 3, 0, 2, 0),
        ("running", 3, 3, 2, 3),
        ("paused", 2, 3, 4, 1),
        ("failed", 3, 0, 2, 0),
        ("failed", 3, 3, 2, 3),
        ("running", 0, 2, 25, 0),
    ],
)
def test_prune_budget(tmp_path, status, protected, terminal, cap, deleted):
    for i in range(protected):
        saved(tmp_path / f"protected-{i}", status)
    for i in range(terminal):
        saved(tmp_path / f"terminal-{i}", "completed")
        l.update_run(tmp_path / f"terminal-{i}", {}, now=f"2026-01-0{i + 1}T00:00:00Z")
    result = l.prune_runs(tmp_path, {"WISE_RUN_HISTORY_CAP": str(cap)})
    assert len(result["pruned"]) == deleted and result["failed"] == []
    assert all((tmp_path / f"protected-{i}").exists() for i in range(protected))
    assert all(not (tmp_path / f"terminal-{i}").exists() for i in range(deleted))


def test_prune_orphans_default_and_legacy(tmp_path):
    assert l.prune_runs(tmp_path / "missing", {}) == {"pruned": [], "failed": []}
    saved(tmp_path / "complete", "completed")
    (tmp_path / "orphan").mkdir()
    assert l.prune_runs(tmp_path, {"WISE_RUN_HISTORY_CAP": "1"})["pruned"] == ["orphan"]
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "state.yaml").write_text("status: failed\n")
    assert l.prune_runs(tmp_path, {"WISE_RUN_HISTORY_CAP": "1"})["pruned"] == ["complete"]
    assert (legacy / "state.yaml").read_text() == "status: failed\n"
    for i in range(26):
        saved(tmp_path / f"run-{i:02}", "completed")
        l.update_run(tmp_path / f"run-{i:02}", {}, now=f"2026-01-01T00:00:{i:02}Z")
    assert l.prune_runs(tmp_path, {})["pruned"] == ["run-01", "run-00"]


def test_prune_external_symlink(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    outside = saved(tmp_path / "outside", "completed")
    (root / "external").symlink_to(outside, target_is_directory=True)
    saved(root / "protected", "failed")
    assert l.prune_runs(root, {"WISE_RUN_HISTORY_CAP": "1"})["pruned"] == []
    assert outside.exists()


def test_atomic_failure_preserves_state(tmp_path, monkeypatch):
    demo(tmp_path)
    before = l.read_state(tmp_path)

    def fail(*args):
        raise OSError("rename failed")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail)
        with pytest.raises(OSError):
            l.update_run(tmp_path, {"status": "completed"})
    assert l.read_state(tmp_path) == before and not list(tmp_path.glob("*.tmp"))


def test_events_resume_torn_and_filters(tmp_path):
    assert l.read_events(tmp_path) == []
    for kind in ["run.started", "step.started", "step.done", "run.done"]:
        event = l.append_event(tmp_path, {"run_id": "r", "type": kind}, now=STAMP)
        assert event["ts"] == STAMP
    assert [e["seq"] for e in l.read_events(tmp_path)] == [1, 2, 3, 4]
    assert [(e["seq"], e["type"]) for e in l.read_events(tmp_path, 2)] == [
        (3, "step.done"),
        (4, "run.done"),
    ]
    assert l.read_events(tmp_path, 4) == []
    with l.events_path(tmp_path).open("a") as out:
        out.write('{"seq":5,"tr')
    assert l.append_event(tmp_path, {"type": "warn"})["seq"] == 5
    assert [e["seq"] for e in l.read_events(tmp_path)] == [1, 2, 3, 4, 5]


def test_large_event_tail(tmp_path):
    l.append_event(tmp_path, {"message": "Ω" * 70000})
    assert l.append_event(tmp_path, {"message": "next"})["seq"] == 2


def test_unit_checkpoint_and_logs(tmp_path):
    assert l.read_unit(tmp_path, "feat/test") is None and l.list_units(tmp_path) == []
    ledger = {
        "unit": {"ref": "test", "branch": "feat/test"},
        "last_phase": "review",
        "review": {"cycles": 2, "converged": False},
        "cleaned": False,
        "cursors": {"review": {"session": "s1"}},
        "usage": l.empty_usage(),
    }
    l.write_unit(tmp_path, "feat/test", ledger)
    l.write_unit(tmp_path, "feat/test2", {**ledger, "unit": {"ref": "test2"}})
    assert l.read_unit(tmp_path, "feat/test") == ledger
    assert l.unit_path(tmp_path, "feat/test").name == "feat%2Ftest.json"
    assert [u["unit"]["ref"] for u in l.list_units(tmp_path)] == ["test", "test2"]
    (tmp_path / "units/broken.json").write_text("{")
    (tmp_path / "units/valid.json.tmp").write_text("{}")
    assert len(l.list_units(tmp_path)) == 2
    ident = l.new_ulid()
    paths = l.log_paths(tmp_path, "a", ident)
    assert paths == {
        "log": str(tmp_path / f"logs/a.{ident}.log"),
        "raw": str(tmp_path / f"logs/a.{ident}.raw.jsonl"),
    }
    assert l.write_log(tmp_path, "a", ident, "hello\n") == paths["log"]
    assert Path(paths["log"]).read_text() == "hello\n"
    for line in [1, 2]:
        assert l.append_raw_log(tmp_path, "a", ident, {"line": line}) == paths["raw"]
    assert Path(paths["raw"]).read_text() == '{"line":1}\n{"line":2}\n'
    for step, run, code in [
        ("../x", ident, "INVALID_STEP_ID"),
        ("a", "../x", "INVALID_STEP_RUN_ID"),
    ]:
        with pytest.raises(l.LedgerError) as error:
            l.write_log(tmp_path, step, run, "")
        assert error.value.code == code


def test_usage_views(tmp_path):
    state = demo(tmp_path)

    def usage(n, pool, cost=None):
        value = dict(input=n, output=n / 10, cache_read=5, cache_write=1, pool=pool)
        if cost is not None:
            value.update(cost_usd=cost, cost_source="reported")
        return value

    for step, harness, u in [
        ("a", "claude", usage(100, "subscription", 0.5)),
        ("a", "claude", usage(200, "subscription")),
        ("b", "codex", usage(40, "api-key", 0.25)),
    ]:
        l.fold_usage_views(state, dict(step=step, harness=harness, usage=u))
    views = state["usage"]
    for values in [
        [views["subscription"], views["api-key"]],
        views["by_harness"].values(),
        views["by_step"].values(),
        [s["usage"] for s in state["steps"].values()],
    ]:
        assert sum(v["input"] for v in values) == 340
    assert state["steps"]["a"]["usage"] == views["by_step"]["a"]
    assert views["by_harness"]["claude"]["cache_read"] == 10
    assert views["subscription"]["cost_source"] == "reported"
    total = l.usage_total(views)
    assert (total["input"], total["output"], total["cost_usd"], total["pool"]) == (
        340,
        34,
        0.75,
        "subscription",
    )
    assert l.usage_tokens(total) == 377
    del views["by_step"]
    l.fold_usage_views(state, dict(step="b", harness="codex", usage=usage(1, "api-key")))
    assert views["by_step"]["b"]["input"] == 1 and state["steps"]["b"]["usage"]["input"] == 41
    into = l.empty_usage()
    for u, source in [
        (l.empty_usage(), "none"),
        ({**l.empty_usage(), "cost_usd": 1}, "reported"),
        ({**l.empty_usage(), "cost_usd": 2, "cost_source": "priced"}, "priced"),
        ({**l.empty_usage(), "cost_usd": 1}, "priced"),
    ]:
        l.add_usage(into, u)
        assert into["cost_source"] == source
    assert into["cost_usd"] == 4
    api_only = l.empty_usage_by_pool()
    api_only["api-key"]["output"] = 1
    assert l.usage_total(api_only)["pool"] == "api-key"


def test_worktree_include(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    assert "nothing to copy" in l.apply_worktree_include(repo, dest)["notices"][0]
    (repo / ".worktreeinclude").write_text("*.txt\ncache/\n")
    (repo / "tracked.txt").write_text("tracked")
    git("add", "tracked.txt")
    (repo / "plain.txt").write_text("new")
    (dest / "plain.txt").write_text("old")
    (repo / "cache").mkdir()
    (repo / "cache/a").write_text("cached")
    result = l.apply_worktree_include(repo, dest)
    assert result["copied"] == 2 and result["skipped"] == 0
    assert (dest / "plain.txt").read_text() == "new" and not (dest / "tracked.txt").exists()
    assert (dest / "cache/a").read_text() == "cached"

    def failing(*args):
        raise OSError("git failed")

    assert (
        "git ls-files failed" in l.apply_worktree_include(repo, dest, exec_fn=failing)["notices"][0]
    )
    assert l.apply_worktree_include(repo, dest, exec_fn=lambda *_: "ghost.txt\0")["skipped"] == 1
    result = l.apply_worktree_include(repo, dest, exec_fn=lambda *_: "../escape.txt\0")
    assert result["skipped"] == 1 and "out-of-tree" in result["notices"][0]
    (repo / "link.txt").symlink_to(tmp_path / "outside")
    assert l.apply_worktree_include(repo, dest, exec_fn=lambda *_: "link.txt\0")["skipped"] == 1
    (dest / "plain.txt").unlink()
    (dest / "plain.txt").symlink_to(tmp_path / "outside")
    assert l.apply_worktree_include(repo, dest, exec_fn=lambda *_: "plain.txt\0")["skipped"] == 1


def test_captured_state_contract(tmp_path):
    fixture = json.loads(
        (Path(__file__).parents[1] / "test/fixtures/contracts/ledger.json").read_text()
    )
    initial = fixture["initial"]
    state = l.init_state(
        run_dir=tmp_path,
        run_id=initial["run_id"],
        workflow=initial["workflow"],
        step_ids=list(initial["steps"]),
        cwd=initial["cwd"],
        now=initial["started_at"],
    )
    assert state == initial
    running = fixture["running"]
    l.start_run(
        tmp_path,
        {"inputs": running["inputs"], "answers": running["answers"]},
        now=running["last_activity_at"],
    )
    l.update_step(tmp_path, "a", running["steps"]["a"], now=running["last_activity_at"])
    assert l.read_state(tmp_path) == running


def test_event_unicode_and_torn_utf8(tmp_path):
    message = "a\u2028b\u2029c"
    first = l.append_event(tmp_path, {"message": message})
    assert l.read_events(tmp_path) == [first]
    with l.events_path(tmp_path).open("ab") as dest:
        dest.write(b'{"message":"\xce')
    assert l.read_events(tmp_path) == [first]
    assert l.append_event(tmp_path, {"message": "recovered"})["seq"] == 2
    assert [event["seq"] for event in l.read_events(tmp_path)] == [1, 2]
