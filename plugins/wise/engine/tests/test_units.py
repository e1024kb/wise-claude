import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from test_model_phases import ModelFixture
from test_phases import PhaseFixture, command_result
from wise_engine.ledger import empty_usage, read_unit, write_unit
from wise_engine.phases.claim import claim_phase
from wise_engine.phases.common import make_unit, pass_
from wise_engine.units import config_for, run_units_step


def minimal_input(fixture, **over):
    return {
        "run_dir": str(fixture.run_dir),
        "cwd": str(fixture.repo),
        "step_run_id": "step-run",
        "step": {
            "id": "process",
            "type": "units",
            "pipeline": "ticket",
            "items": "PROJ-1",
            "groups": {},
        },
        "items": ["PROJ-1"],
        "state": {"context": {}, "caps": {}, "profile": "medium", "resolved": {}},
        "parent_env": {},
        "exec": fixture.execute,
        "emit": lambda event: None,
        **over,
    }


def test_config_caps_reviewer_default_and_context():
    step = {
        "pipeline": "ticket",
        "groups": {},
        "caps": ["max_review_cycles"],
        "reviewers": [],
        "resume": "unit",
        "timeout": 5,
        "max_turns": 3,
        "mcp": "engine-only",
    }
    state = {
        "context": {"guidance": "small", "decisions": {"x": "y"}},
        "caps": {"max_review_cycles": 4, "other": 7},
        "profile": "low",
        "permissions": "full",
        "provider_permissions": {"claude": "auto"},
    }
    result = config_for(step, state)
    assert result["caps"] == {"max_review_cycles": 4} and result["reviewers"] == []
    assert result["guidance"] == "small" and result["resume"] == "unit"
    assert result["timeout"] == 5 and result["mcp"] == "engine-only"
    del step["reviewers"]
    assert config_for(step, state)["reviewers"] == ["copilot-pull-request-reviewer"]
    assert config_for(step, state)["base"] == ""
    state["inputs"] = {"base_branch": " release-26-9-0 "}
    assert config_for(step, state)["base"] == "release-26-9-0"


def test_deduplicated_ticket_spellings_and_done_resume(tmp_path):
    async def scenario():
        fixture = PhaseFixture(tmp_path)
        events = []
        args = minimal_input(
            fixture, items=["PROJ-1", "https://tracker.invalid/PROJ-1/details"], emit=events.append
        )
        first = await run_units_step(args)
        assert first["verdict"] == "units=1 merged=0 open=0 failed=0 skipped=1"
        assert "duplicate of an earlier item" in Path(first["log"]).read_text()
        ledger = read_unit(fixture.run_dir, "PROJ-1")
        assert ledger["last_phase"] == "cleanup" and ledger["verdict"] == "skipped"
        fixture.calls.clear()
        events.clear()
        second = await run_units_step(args)
        assert second["outputs"] == first["outputs"] and not fixture.calls
        assert [event["type"] for event in events] == ["unit.done"]

    asyncio.run(scenario())


def test_throwing_phase_persisted_and_cleanup_runs(tmp_path):
    async def scenario():
        fixture = PhaseFixture(tmp_path)

        async def broken(ctx):
            saved = read_unit(ctx["run_dir"], ctx["unit"]["branch"])
            assert saved["cursors"]["claim"] == "owned"
            assert saved["cursors"]["worktree"] == "includes-done"
            raise RuntimeError("boom")

        result = await run_units_step(minimal_input(fixture, runners={"plan": broken}))
        ledger = read_unit(fixture.run_dir, "PROJ-1")
        assert ledger["verdict"] == "failed" and ledger["last_phase"] == "cleanup"
        assert ledger["reason"] == "plan: boom" and "failed=1" in result["verdict"]
        assert Path(ledger["unit"]["worktree"]).exists()

    asyncio.run(scenario())


def test_resume_keeps_original_completed_phase_boundary(tmp_path):
    async def scenario():
        fixture = PhaseFixture(tmp_path)
        unit = make_unit("ticket", "PROJ-1", str(fixture.repo), str(fixture.run_dir), "main")
        Path(unit["worktree"]).mkdir(parents=True)
        fixture.trees[unit["worktree"]] = "PROJ-1"
        fixture.branches.add("PROJ-1")
        saved = {
            "unit": unit,
            "last_phase": "implement",
            "cleaned": False,
            "cursors": {"claim": "owned", "worktree": "includes-done"},
            "usage": empty_usage(),
            "caps": {},
        }
        write_unit(fixture.run_dir, "PROJ-1", saved)
        called = []

        async def unexpected(ctx):
            raise AssertionError("completed side effect must not repeat")

        async def review(ctx):
            called.append("review")
            return pass_(extra={"output": {"findings": 0, "blocking": 0, "verdict": "approve"}})

        async def watch(ctx):
            return pass_(
                extra={
                    "output": {
                        "ci": "green",
                        "bot_reviews": "resolved",
                        "human_comment": False,
                        "merged": True,
                        "verdict": "ready",
                    }
                }
            )

        result = await run_units_step(
            minimal_input(
                fixture,
                runners={
                    "plan": unexpected,
                    "implement": unexpected,
                    "review": review,
                    "watch": watch,
                },
            )
        )
        assert called == ["review"] and "merged=1" in result["verdict"]
        assert any(args[:2] == ["worktree", "list"] for _, args, _ in fixture.calls)

    asyncio.run(scenario())


def test_parallel_order_and_serial_git(tmp_path):
    async def scenario():
        fixture = PhaseFixture(tmp_path)
        active = maximum = 0
        simultaneous = set()
        both_started = asyncio.Event()

        async def execute(cmd, args, opts):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.001)
            try:
                return await fixture.execute(cmd, args, opts)
            finally:
                active -= 1

        async def plan(ctx):
            simultaneous.add(ctx["unit"]["ref"])
            if len(simultaneous) == 2:
                both_started.set()
            await both_started.wait()
            if ctx["unit"]["ref"] == "PROJ-1":
                await asyncio.sleep(0.01)
            return {"ok": False, "verdict": "skipped", "reason": "test stop"}

        args = minimal_input(
            fixture, items=["PROJ-1", "PROJ-2"], exec=execute, runners={"plan": plan}
        )
        args["step"]["parallel"] = 2
        result = await asyncio.wait_for(run_units_step(args), 2)
        assert [row["unit"]["ref"] for row in result["outputs"]["units"]] == ["PROJ-1", "PROJ-2"]
        assert simultaneous == {"PROJ-1", "PROJ-2"}
        assert maximum >= 1

    asyncio.run(scenario())


def test_abort_before_next_unit(tmp_path):
    async def scenario():
        fixture = PhaseFixture(tmp_path)
        signal = asyncio.Event()

        async def plan(ctx):
            signal.set()
            return {"ok": False, "verdict": "skipped", "reason": "cancelled"}

        result = await run_units_step(
            minimal_input(
                fixture, items=["PROJ-1", "PROJ-2"], signal=signal, runners={"plan": plan}
            )
        )
        assert len(result["outputs"]["units"]) == 1
        assert read_unit(fixture.run_dir, "PROJ-2") is None

    asyncio.run(scenario())


def test_plan_missing_file_only_fails_its_unit(tmp_path):
    async def scenario():
        fixture = PhaseFixture(tmp_path)
        plan = fixture.repo / "PLAN-real.md"
        plan.write_text("# Plan")
        args = minimal_input(fixture, items=[str(plan), str(fixture.repo / "PLAN-missing.md")])
        args["step"]["pipeline"] = "plan"
        result = await run_units_step(args)
        assert [row["verdict"] for row in result["outputs"]["units"]] == ["skipped", "failed"]
        assert "missing: plan file" in result["outputs"]["units"][1]["reason"]

    asyncio.run(scenario())


def test_api_key_usage_pricing_and_unknown_notice_once(tmp_path):
    async def scenario():
        fixture = PhaseFixture(tmp_path)

        async def plan(ctx):
            return pass_(
                extra={
                    "usage": {**empty_usage("api-key"), "input": 1_000_000},
                    "resolved": {"harness": "codex", "model": "gpt-5", "effort": "high"},
                }
            )

        async def implement(ctx):
            return {
                "ok": False,
                "reason": "stop",
                "usage": {**empty_usage("api-key"), "input": 100},
                "resolved": {"harness": "codex", "model": "unknown", "effort": "high"},
            }

        usage = []
        result = await run_units_step(
            minimal_input(
                fixture,
                runners={"plan": plan, "implement": implement},
                on_usage=lambda *args: usage.append(args),
            )
        )
        ledger = read_unit(fixture.run_dir, "PROJ-1")
        assert ledger["usage"]["cost_usd"] == 1.25
        assert usage[0][2]["cost_source"] == "priced" and usage[1][2]["cost_source"] == "none"
        assert Path(result["log"]).read_text().count("no price for") == 1

    asyncio.run(scenario())


def test_watch_minutes_cap_and_stable_reset(tmp_path):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        from test_model_phases import answer, watch_output

        fixture.step["caps"] = ["watch_minutes", "watch_poll_seconds"]
        fixture.state["caps"] = {"watch_minutes": 2, "watch_poll_seconds": 60}
        fixture.scripts["watch"] = lambda req, nth: answer(watch_output(bot_reviews="pending"))
        result = await run_units_step(fixture.input())
        row = result["outputs"]["units"][0]
        assert row["verdict"] == "all-green" and "watch_minutes (2) cap reached" in row["reason"]
        assert fixture.counts["watch"] == 2

    asyncio.run(scenario())


def test_cancel_during_resume_recheck_preserves_completed_boundary(tmp_path):
    async def scenario():
        fixture = PhaseFixture(tmp_path)
        unit = make_unit("ticket", "PROJ-1", str(fixture.repo), str(fixture.run_dir), "main")
        write_unit(
            fixture.run_dir,
            "PROJ-1",
            {
                "unit": unit,
                "last_phase": "implement",
                "cleaned": False,
                "cursors": {"claim": "owned"},
                "usage": empty_usage(),
                "caps": {},
            },
        )
        signal = asyncio.Event()

        async def claim(ctx):
            signal.set()
            return pass_()

        await run_units_step(minimal_input(fixture, signal=signal, runners={"claim": claim}))
        assert read_unit(fixture.run_dir, "PROJ-1")["last_phase"] == "implement"

    asyncio.run(scenario())


def test_current_tree_units_run_serially_and_use_selected_checkout(tmp_path):
    async def scenario():
        fixture = PhaseFixture(tmp_path)
        active = 0
        observed = []

        async def plan(ctx):
            nonlocal active
            active += 1
            assert active == 1
            assert ctx["unit"]["worktree"] == str(fixture.repo)
            observed.append(ctx["unit"]["ref"])
            await asyncio.sleep(0.01)
            active -= 1
            from wise_engine.phases.common import fail

            return fail("stop after checking checkout")

        args = minimal_input(fixture, items=["PROJ-1", "PROJ-2"], runners={"plan": plan})
        args["state"]["inputs"] = {"worktree_mode": "current"}
        args["step"]["parallel"] = 2
        await run_units_step(args)
        assert observed == ["PROJ-1", "PROJ-2"]
        assert not any(
            cmd == "git" and call[:2] == ["worktree", "add"] for cmd, call, _ in fixture.calls
        )
        assert fixture.repo.exists()

    asyncio.run(scenario())


def test_current_tree_lock_rejects_other_runs_and_releases_on_cancel(tmp_path):
    async def scenario():
        fixture = PhaseFixture(tmp_path)
        started = asyncio.Event()

        async def plan(ctx):
            started.set()
            await asyncio.Event().wait()

        args = minimal_input(fixture, runners={"plan": plan})
        args["state"]["inputs"] = {"worktree_mode": "current"}
        first = asyncio.create_task(run_units_step(args))
        try:
            await asyncio.wait_for(started.wait(), 2)
            with pytest.raises(RuntimeError, match="another workflow is using this checkout"):
                await run_units_step({**args, "step_run_id": "other-run"})
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import fcntl, sys; f = open(sys.argv[1], 'a'); "
                    "fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)",
                    str(fixture.repo / "wise-current-tree.lock"),
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert probe.returncode != 0 and "BlockingIOError" in probe.stderr
        finally:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        retry = minimal_input(fixture, items=["PROJ-2"])
        retry["state"]["inputs"] = {"worktree_mode": "current"}
        result = await run_units_step(retry)
        assert len(result["outputs"]["units"]) == 1

    asyncio.run(scenario())


def test_current_tree_units_borrow_live_run_lock_without_releasing_it(tmp_path):
    from wise_engine.units import acquire_checkout_lock

    async def scenario():
        fixture = PhaseFixture(tmp_path)
        args = minimal_input(fixture)
        args["state"]["inputs"] = {"worktree_mode": "current"}
        path = fixture.repo / "wise-current-tree.lock"
        with acquire_checkout_lock(path) as lease:
            result = await run_units_step({**args, "checkout_lock": lease})
            assert len(result["outputs"]["units"]) == 1
            assert not lease.closed
            with pytest.raises(RuntimeError, match="another workflow"):
                await run_units_step(args)
        await run_units_step(args)

    asyncio.run(scenario())


def _on_branch(fixture, branch):
    fixture.failures[("git", "symbolic-ref", "--quiet", "--short", "HEAD")] = (
        command_result(branch + "\n") if branch else command_result(code=128)
    )


def test_pr_pipeline_watches_checked_out_branch_in_place(tmp_path):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        _on_branch(fixture, "feat/x")
        fixture.remote.add("develop")
        fixture.pr = {
            "number": 7,
            "url": "https://github.invalid/a/r/pull/7",
            "state": "OPEN",
            "baseRefName": "develop",
        }
        fixture.step.update(
            {"pipeline": "pr", "items": "feat/x", "groups": {"watch": "watch"}, "caps": []}
        )
        result = await run_units_step(
            fixture.input(items=["feat/x"], state={**fixture.state, "worktree_mode": "new"})
        )
        row = result["outputs"]["units"][0]
        assert row["verdict"] == "merged" and row["unit"]["pr"]["number"] == 7
        assert row["unit"]["base"] == "develop"
        assert Path(row["unit"]["worktree"]).resolve() == fixture.repo.resolve()
        assert not [c for c in fixture.calls if c[0] == "git" and c[1][:2] == ["worktree", "add"]]
        assert fixture.counts["plan"] == 0 and fixture.counts["implement"] == 0
        assert fixture.counts["watch"] >= 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "branch,pr,verdict,needle",
    [
        ("", None, "failed", "detached"),
        ("main", {"number": 1, "url": "u", "state": "OPEN"}, "failed", "protected branch main"),
        ("feat/x", None, "failed", "no pull request for feat/x"),
        ("feat/x", {"number": 1, "url": "u", "state": "MERGED"}, "merged", "pr-merged: #1"),
        ("feat/x", {"number": 1, "url": "u", "state": "CLOSED"}, "skipped", "is closed"),
        ("feat/y", {"number": 1, "url": "u", "state": "OPEN"}, "failed", "checkout is on feat/y"),
    ],
)
def test_pr_pipeline_claim_refusals(tmp_path, branch, pr, verdict, needle):
    async def scenario():
        fixture = PhaseFixture(tmp_path)
        _on_branch(fixture, branch)
        fixture.pr = pr
        fixture.ctx["config"]["pipeline"] = "pr"
        fixture.ctx["unit"] = make_unit("pr", "feat/x", str(fixture.repo), str(fixture.run_dir))
        result = await claim_phase(fixture.ctx)
        assert result["ok"] is False
        assert result.get("verdict", "failed") == verdict and needle in result["reason"]

    asyncio.run(scenario())


def test_implement_pipeline_runs_on_checked_out_branch(tmp_path):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        _on_branch(fixture, "feat/PROJ-3")
        plan = fixture.repo / "docs" / "plans" / "PLAN-PROJ-3.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n1. do it\n")
        fixture.step.update(
            {"pipeline": "implement", "items": str(plan), "groups": {"implement": "implement"}}
        )
        result = await run_units_step(fixture.input(items=[str(plan)]))
        row = result["outputs"]["units"][0]
        assert row["verdict"] == "all-green" and row["unit"]["branch"] == "feat/PROJ-3"
        assert row["unit"]["plan_path"] == str(plan)
        assert read_unit(str(fixture.run_dir), "feat/PROJ-3")["verdict"] == "all-green"
        assert fixture.counts == {"implement": 1}
        assert not [c for c in fixture.calls if c[0] == "gh" and c[1][:2] == ["pr", "create"]]
        assert not [c for c in fixture.calls if c[0] == "git" and c[1][0] == "push"]

    asyncio.run(scenario())


def test_implement_pipeline_requires_plan_file(tmp_path):
    async def scenario():
        fixture = PhaseFixture(tmp_path)
        _on_branch(fixture, "feat/x")
        fixture.ctx["config"]["pipeline"] = "implement"
        fixture.ctx["unit"] = make_unit(
            "implement", str(fixture.repo / "PLAN-nope.md"), str(fixture.repo), str(fixture.run_dir)
        )
        result = await claim_phase(fixture.ctx)
        assert result["ok"] is False and "missing: plan file" in result["reason"]

    asyncio.run(scenario())


def test_declined_substitute_review_stands_down_on_a_stuck_bot(tmp_path):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        from test_model_phases import answer, watch_output

        _on_branch(fixture, "feat/x")
        fixture.pr = {"number": 7, "url": "https://github.invalid/a/r/pull/7", "state": "OPEN"}
        fixture.step.update({"pipeline": "pr", "items": "feat/x", "groups": {}, "caps": []})
        fixture.scripts["watch"] = lambda req, nth: answer(watch_output(bot_reviews="stuck"))
        state = {**fixture.state, "inputs": {"substitute_review": "no"}}
        result = await run_units_step(fixture.input(items=["feat/x"], state=state))
        row = result["outputs"]["units"][0]
        assert row["verdict"] == "all-green" and "review-consent-declined" in row["reason"]
        assert fixture.counts["review"] == 0 and fixture.counts["watch"] == 1

    asyncio.run(scenario())


def _no_gh_pr(fixture):
    return not any(c[0] == "gh" and c[1][:1] == ["pr"] for c in fixture.calls)


def test_ticket_pipeline_no_origin_ends_no_pr(tmp_path):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        fixture.origin_url = None
        result = await run_units_step(fixture.input())
        row = result["outputs"]["units"][0]
        assert row["verdict"] == "no-pr"
        assert row["reason"].startswith("no-github-remote: no origin remote")
        assert row["cleaned"] is False
        assert not any(c[0] == "git" and c[1][0] == "push" for c in fixture.calls)
        assert not any(c[0] == "gh" for c in fixture.calls)
        assert result["verdict"].endswith(" no-pr=1")
        assert Path(row["unit"]["worktree"]).exists()

    asyncio.run(scenario())


def test_ticket_pipeline_non_github_origin_pushes_then_no_pr(tmp_path):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        fixture.origin_url = "git@gitlab.com:a/r.git"
        result = await run_units_step(fixture.input())
        row = result["outputs"]["units"][0]
        assert row["verdict"] == "no-pr"
        assert "gitlab.com" in row["reason"] and "git@gitlab.com" not in row["reason"]
        assert any(c[0] == "git" and c[1][0] == "push" for c in fixture.calls)
        assert _no_gh_pr(fixture)

    asyncio.run(scenario())


def test_plan_pipeline_no_origin_ends_no_pr(tmp_path):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        fixture.origin_url = None
        plan = fixture.repo / "PLAN-real.md"
        plan.write_text("# Plan\n1. do it\n")
        fixture.step.update({"pipeline": "plan", "items": str(plan)})
        result = await run_units_step(fixture.input(items=[str(plan)]))
        assert result["outputs"]["units"][0]["verdict"] == "no-pr"

    asyncio.run(scenario())


def test_current_tree_no_origin_accepts_local_base_and_ends_no_pr(tmp_path):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        fixture.origin_url = None
        fixture.branches.add("main")
        # No origin/main: the base resolves to the local branch, accepted
        # because no GitHub PR is opened against it.
        fixture.failures[("git", "show-ref", "--verify", "--quiet", "refs/remotes/origin/main")] = (
            command_result("", code=1)
        )
        state = {**fixture.state, "inputs": {"worktree_mode": "current", "base_branch": "main"}}
        result = await run_units_step(fixture.input(state=state))
        row = result["outputs"]["units"][0]
        assert row["verdict"] == "no-pr"
        assert Path(row["unit"]["worktree"]).resolve() == fixture.repo.resolve()

    asyncio.run(scenario())


def test_resume_across_remote_change(tmp_path):
    async def saved_at_review(fixture):
        unit = make_unit("ticket", "PROJ-1", str(fixture.repo), str(fixture.run_dir), "main")
        Path(unit["worktree"]).mkdir(parents=True, exist_ok=True)
        fixture.trees[unit["worktree"]] = "PROJ-1"
        fixture.branches.add("PROJ-1")
        write_unit(
            fixture.run_dir,
            "PROJ-1",
            {
                "unit": {**unit, "base_ref": "origin/main"},
                "last_phase": "review",
                "cleaned": False,
                "cursors": {"claim": "owned", "worktree": "includes-done"},
                "usage": empty_usage(),
                "caps": {},
            },
        )

    async def scenario():
        (tmp_path / "none").mkdir()
        (tmp_path / "gh").mkdir()
        none_fixture = ModelFixture(tmp_path / "none")
        none_fixture.origin_url = None
        await saved_at_review(none_fixture)
        result = await run_units_step(none_fixture.input())
        assert result["outputs"]["units"][0]["verdict"] == "no-pr"
        assert not any(c[0] == "git" and c[1][0] == "push" for c in none_fixture.calls)

        gh_fixture = ModelFixture(tmp_path / "gh")
        await saved_at_review(gh_fixture)
        await run_units_step(gh_fixture.input())
        assert any(c[0] == "git" and c[1][0] == "push" for c in gh_fixture.calls)

    asyncio.run(scenario())


def test_resume_after_pr_when_remote_drops_github(tmp_path):
    # A unit that opened its PR on an earlier GitHub run, resumed after the
    # remote is no longer GitHub, ends `skipped` (not `failed: no verdict`).
    async def scenario():
        fixture = ModelFixture(tmp_path)
        fixture.origin_url = None
        unit = make_unit("ticket", "PROJ-1", str(fixture.repo), str(fixture.run_dir), "main")
        Path(unit["worktree"]).mkdir(parents=True, exist_ok=True)
        fixture.trees[unit["worktree"]] = "PROJ-1"
        fixture.branches.add("PROJ-1")
        write_unit(
            fixture.run_dir,
            "PROJ-1",
            {
                "unit": {**unit, "base_ref": "origin/main"},
                "last_phase": "pr",
                "cleaned": False,
                "cursors": {"claim": "owned", "worktree": "includes-done"},
                "usage": empty_usage(),
                "caps": {},
            },
        )
        result = await run_units_step(fixture.input())
        row = result["outputs"]["units"][0]
        assert row["verdict"] == "skipped"
        assert "already opened" in row["reason"]

    asyncio.run(scenario())


@pytest.mark.parametrize("origin", [None, "git@gitlab.com:a/r.git"])
def test_pr_pipeline_without_github_remote_is_skipped(tmp_path, origin):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        fixture.origin_url = origin
        _on_branch(fixture, "feat/x")
        fixture.step.update({"pipeline": "pr", "items": "feat/x", "groups": {}, "caps": []})
        result = await run_units_step(fixture.input(items=["feat/x"]))
        row = result["outputs"]["units"][0]
        assert row["verdict"] == "skipped"
        assert row["reason"].startswith("no-github-remote:")
        assert not any(c[0] == "gh" and c[1][:2] == ["pr", "view"] for c in fixture.calls)

    asyncio.run(scenario())


def test_ticket_pipeline_ghes_origin_takes_github_path(tmp_path):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        fixture.origin_url = "https://ghes.corp.example/a/r.git"
        fixture.gh_hosts.add("ghes.corp.example")
        result = await run_units_step(fixture.input())
        row = result["outputs"]["units"][0]
        assert row["verdict"] != "no-pr"
        assert any(c[0] == "git" and c[1][0] == "push" for c in fixture.calls)

    asyncio.run(scenario())


def test_attached_pipelines_force_current_tree_and_input_cap_overrides():
    step = {"pipeline": "pr", "groups": {}, "caps": ["max_fix_attempts", "watch_minutes"]}
    state = {
        "context": {},
        "caps": {"max_fix_attempts": 10, "watch_minutes": 120},
        "profile": "medium",
        "worktree_mode": "new",
        "inputs": {"max_fix_attempts": " 3 ", "watch_minutes": "", "base_branch": ""},
    }
    result = config_for(step, state)
    assert result["worktree_mode"] == "current"
    assert result["caps"] == {"max_fix_attempts": 3.0, "watch_minutes": 120}
    for raw in ("soon", "inf", "-inf", "nan", "-1", "0", "1.5", "1441", "9" * 400):
        state["inputs"]["watch_minutes"] = raw
        assert config_for(step, state)["caps"]["watch_minutes"] == 120
    state["inputs"]["watch_minutes"] = "1440"
    assert config_for(step, state)["caps"]["watch_minutes"] == 1440.0
    state["inputs"]["max_fix_attempts"] = "9" * 400
    assert config_for(step, state)["caps"]["max_fix_attempts"] == 10
