import asyncio
from pathlib import Path

from test_model_phases import ModelFixture
from test_phases import PhaseFixture
from wise_engine.ledger import empty_usage, read_unit, write_unit
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
