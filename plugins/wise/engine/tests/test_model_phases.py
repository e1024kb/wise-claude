import asyncio
import inspect
import re
from collections import Counter
from pathlib import Path

import pytest

from test_phases import PhaseFixture, command_result
from wise_engine.adapter_types import AgentHandle
from wise_engine.ledger import empty_usage, read_unit
from wise_engine.phases.common import make_unit
from wise_engine.phases.model import (
    BOT_LOGINS,
    PHASE_MODE,
    PHASE_TOOLS,
    _ticket_block,
    base_vars,
    engine_plan_path,
    findings_path,
    render_phase_prompt,
    resolve_unit_phases,
)
from wise_engine.prompts.units.schemas import (
    MODEL_PHASES,
    parse_fix,
    parse_implement,
    parse_plan,
    parse_review,
    parse_watch,
)
from wise_engine.units import run_units_step


def answer(value, **extra):
    return {
        "text": "done",
        "json": value,
        "usage": {**empty_usage(), "input": 10, "output": 2},
        "exit": "ok",
        **extra,
    }


class ModelFixture(PhaseFixture):
    def __init__(self, root):
        super().__init__(root)
        self.child_calls = []
        self.counts = Counter()
        self.scripts = {}
        self.events = []
        self.usage = []
        self.sleeps = []
        self.time = 1000
        self.ctx["now"] = lambda: self.time
        self.ctx["agent"] = {
            "starter": self.starter,
            "step_id": "process",
            "step_run_id": "step-run",
            "step_token": "token",
        }
        self.state = {
            "context": {"ticket": self.ctx["config"]["tickets"], "guidance": "keep it small"},
            "caps": {},
            "profile": "medium",
            "resolved": {},
        }
        self.step = {
            "id": "process",
            "type": "units",
            "pipeline": "ticket",
            "items": "PROJ-1",
            "groups": {},
            "caps": [],
        }

    async def starter(self, harness, req, on_event):
        phase = re.match(r"# wise unit phase: (\S+)", req["prompt"])[1]
        self.child_calls.append((phase, harness, req))
        self.counts[phase] += 1

        async def execute():
            script = self.scripts.get(phase)
            if script:
                result = script(req, self.counts[phase])
                return await result if inspect.isawaitable(result) else result
            if phase == "plan":
                path = Path(req["add_dirs"][0]) / "plans" / f"PLAN-{Path(req['cwd']).name}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# Plan\n1. implement\n")
                return answer({"plan_path": str(path), "status": "ready"}, cursor="plan-session")
            if phase == "implement":
                self.commits += 1
                self.head = "head-" + str(self.commits)
                return answer(
                    {"waves": 1, "tasks": 1, "done": 1, "failed": 0, "commits": 99},
                    cursor="implement-session",
                )
            if phase == "review":
                return answer(
                    {"findings": 0, "blocking": 0, "verdict": "approve"}, cursor="review-session"
                )
            if phase == "fix":
                self.commits += 1
                self.head = "head-" + str(self.commits)
                return answer({"fixed": 1, "skipped": 0, "commits": 1}, cursor="fix-session")
            if phase == "watch":
                return answer(watch_output())
            raise AssertionError(phase)

        return AgentHandle(done=asyncio.create_task(execute()))

    async def sleep(self, ms, signal=None):
        self.sleeps.append(ms)
        self.time += ms
        await asyncio.sleep(0)

    def input(self, **over):
        return {
            "run_dir": str(self.run_dir),
            "cwd": str(self.repo),
            "step_run_id": "step-run",
            "step": self.step,
            "items": ["PROJ-1"],
            "state": self.state,
            "parent_env": {},
            "exec": self.execute,
            "agent": {"starter": self.starter, "step_token": "token"},
            "emit": self.events.append,
            "on_usage": lambda phase, harness, usage, model: self.usage.append(
                (phase, harness, usage, model)
            ),
            "sleep": self.sleep,
            "now": lambda: self.time,
            **over,
        }


def watch_output(**over):
    return {
        "ci": "green",
        "bot_reviews": "resolved",
        "human_comment": False,
        "merged": False,
        "verdict": "ready",
        **over,
    }


def test_phase_schemas_reject_boolean_counts_and_unknown_values():
    assert parse_plan({"plan_path": "p", "status": "ready", "blueprint_path": ""}) == {
        "plan_path": "p",
        "status": "ready",
    }
    assert parse_plan({"plan_path": "p", "status": "bogus"}) is None
    assert (
        parse_implement({"waves": True, "tasks": 1, "done": 1, "failed": 0, "commits": 1}) is None
    )
    assert parse_fix({"fixed": 1.5, "skipped": 0, "commits": 1}) is None
    assert (
        parse_review({"findings": 0, "blocking": 0, "verdict": "approve"})["verdict"] == "approve"
    )
    assert parse_watch(watch_output(human_comment=1)) is None
    assert parse_watch(watch_output()) == watch_output()


def test_templates_resolution_and_context_pointer(tmp_path):
    fixture = ModelFixture(tmp_path)
    variables = {
        **base_vars(fixture.ctx),
        "ticket": "ticket body {{data}}",
        "shape": "panel",
        "cycle": 1,
        "lenses": "all",
        "effort": "high",
        "verification": "",
        "source": "review",
        "instructions": "fix",
        "pass": 1,
        "head_sha": "sha",
        "run_started": "now",
    }
    for pipeline in ("ticket", "plan"):
        for phase in MODEL_PHASES:
            text = render_phase_prompt(pipeline, phase, variables)
            assert text.startswith("# wise unit phase: " + phase)
    with pytest.raises(ValueError, match="unresolved placeholder"):
        render_phase_prompt("ticket", "plan", {})
    step = {"groups": {"plan": "p", "implement": "i"}}
    tuning = {
        "p": {"harness": "codex", "model": "gpt-5.5", "effort": "high"},
        "i": {"harness": "grok", "model": "grok-4.6"},
    }
    resolved = resolve_unit_phases(step, tuning, "low", {})
    assert resolved["plan"]["harness"] == "codex" and resolved["fix"] == resolved["implement"]
    assert (
        resolved["review"]["model"] == "claude-opus-4-8"
        and resolved["review"]["effort"] == "medium"
    )
    assert resolved["watch"]["model"] == "sonnet"
    assert all(
        value["model"] == "haiku"
        for value in resolve_unit_phases({**step, "model": "haiku"}, tuning, "medium", {}).values()
    )
    assert "github-actions[bot]" in BOT_LOGINS


def test_repository_qualified_ticket_uses_native_context_and_prompt_ref(tmp_path):
    fixture = ModelFixture(tmp_path)
    fixture.ctx["unit"] = make_unit(
        "ticket", "owner/repo#42", str(fixture.repo), str(fixture.run_dir), "main"
    )
    fixture.ctx["ledger"]["unit"] = fixture.ctx["unit"]
    fixture.ctx["config"]["tickets"] = [
        {"ref": "owner/repo#42", "body": "Repository-specific ticket body."}
    ]
    ticket = _ticket_block(fixture.ctx)
    variables = {**base_vars(fixture.ctx), "ticket": ticket}
    prompt = render_phase_prompt("ticket", "plan", variables)
    assert "ticket owner/repo#42" in prompt
    assert "Repository-specific ticket body." in prompt
    assert "# owner/repo#42: <title>" in prompt


def test_happy_pipeline_model_requests_and_usage(tmp_path):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        fixture.state["context"]["ticket"][0]["path"] = "/saved/ticket.md"
        fixture.state["context"]["ticket"][0]["body"] = "BODY MUST NOT RIDE IN PROMPT"
        fixture.step.update(mcp="engine-only", timeout=17, max_turns=8)
        result = await run_units_step(fixture.input())
        assert result["verdict"] == "units=1 merged=1 open=0 failed=0 skipped=0"
        ledger = read_unit(fixture.run_dir, "PROJ-1")
        assert ledger["cleaned"] and ledger["review"] == {"converged": True, "cycles": 1}
        assert ledger["watch"]["passes"] == 2
        assert fixture.counts == {"plan": 1, "implement": 1, "review": 1, "watch": 2}
        assert len(fixture.usage) == 5 and ledger["usage"]["input"] == 50
        assert ledger["usage_by_phase"]["watch"]["input"] == 20
        for phase, harness, request in fixture.child_calls:
            assert request["mode"] == PHASE_MODE[phase]
            assert request["allowed_tools"] == PHASE_TOOLS[phase]
            assert request["mcp_policy"] == "engine-only" and request["timeout_ms"] == 17000
            assert request["max_turns"] == 8
            assert request["cwd"] in request["add_dirs"]
        plan_prompt = fixture.child_calls[0][2]["prompt"]
        assert "/saved/ticket.md" in plan_prompt and "BODY MUST NOT" not in plan_prompt

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "resume,cross,expected",
    [("unit", False, "review-session"), ("fresh", False, None), ("unit", True, None)],
)
def test_review_fix_cycles_resume_and_cross_harness(tmp_path, resume, cross, expected):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        fixture.step["resume"] = resume
        fixture.scripts["review"] = lambda req, nth: answer(
            {
                "findings": 1 if nth == 1 else 0,
                "blocking": 0,
                "verdict": "changes-requested" if nth == 1 else "approve",
            },
            cursor="review-session",
        )
        if cross:
            fixture.state["resolved"] = {
                "process.review": {"harness": "codex", "model": "gpt-5.5", "effort": "high"},
                "process.fix": {"harness": "grok", "model": "grok-4.6", "effort": ""},
            }
        result = await run_units_step(fixture.input())
        assert "merged=1" in result["verdict"]
        assert fixture.counts["review"] == 2 and fixture.counts["fix"] == 1
        request = next(req for phase, _, req in fixture.child_calls if phase == "fix")
        assert request.get("resume") == expected
        assert read_unit(fixture.run_dir, "PROJ-1")["review"] == {"converged": True, "cycles": 2}

    asyncio.run(scenario())


@pytest.mark.parametrize("permission", ["full", "provider"])
def test_permission_floor_every_phase(tmp_path, permission):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        if permission == "full":
            fixture.state["permissions"] = "full"
        else:
            fixture.state["provider_permissions"] = {"claude": "full-access"}
        await run_units_step(fixture.input())
        assert all(req["mode"] == "full-access" for _, _, req in fixture.child_calls)

    asyncio.run(scenario())


def test_review_cap_pushes_nonconverged(tmp_path):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        fixture.scripts["review"] = lambda req, nth: answer(
            {"findings": 1, "blocking": 1, "verdict": "changes-requested"}
        )
        result = await run_units_step(fixture.input())
        assert "merged=1" in result["verdict"]
        assert read_unit(fixture.run_dir, "PROJ-1")["review"] == {"converged": False, "cycles": 2}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "watch,verdict",
    [
        (watch_output(human_comment=True), "human-intervention"),
        (watch_output(verdict="blocked"), "blocked"),
        (watch_output(merged=True), "merged"),
    ],
)
def test_watch_human_blocked_and_merged(tmp_path, watch, verdict):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        fixture.scripts["watch"] = lambda req, nth: answer(watch)
        result = await run_units_step(fixture.input())
        assert result["outputs"]["units"][0]["verdict"] == verdict
        assert read_unit(fixture.run_dir, "PROJ-1")["cleaned"] == (verdict == "merged")

    asyncio.run(scenario())


def test_watch_red_fix_push_green(tmp_path):
    async def scenario():
        fixture = ModelFixture(tmp_path)

        def watch(req, nth):
            path = Path(findings_path(fixture.ctx))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("CI failure")
            return answer(watch_output(ci="red", verdict="fix") if nth == 1 else watch_output())

        fixture.scripts["watch"] = watch
        result = await run_units_step(fixture.input())
        assert "merged=1" in result["verdict"] and fixture.counts["fix"] == 1
        assert read_unit(fixture.run_dir, "PROJ-1")["watch"]["fix_attempts"] == 1
        assert sum(args[0] == "push" for cmd, args, _ in fixture.calls if cmd == "git") == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("zero_commit,expected", [(False, "exhausted"), (True, "partial")])
def test_fix_cap_or_no_commit(tmp_path, zero_commit, expected):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        fixture.step["caps"] = ["max_fix_attempts"]
        fixture.state["caps"]["max_fix_attempts"] = 1

        def watch(req, nth):
            Path(findings_path(fixture.ctx)).write_text("CI failure")
            return answer(watch_output(ci="red", verdict="fix"))

        fixture.scripts["watch"] = watch
        if zero_commit:
            fixture.scripts["fix"] = lambda req, nth: answer(
                {"fixed": 0, "skipped": 1, "commits": 0}
            )
            fixture.failures[("git", "rev-list", "--count", "head-1..HEAD")] = command_result("0")
        result = await run_units_step(fixture.input())
        assert result["outputs"]["units"][0]["verdict"] == expected

    asyncio.run(scenario())


def test_stuck_bot_substitute_once_and_timeout(tmp_path):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        fixture.scripts["watch"] = lambda req, nth: answer(watch_output(bot_reviews="stuck"))
        result = await run_units_step(fixture.input())
        assert "merged=1" in result["verdict"] and fixture.counts["review"] == 2
        substitute = [req for phase, _, req in fixture.child_calls if phase == "review"][1]
        assert "universal (one reviewer, medium effort)" in substitute["prompt"]
        assert read_unit(fixture.run_dir, "PROJ-1")["watch"]["fallback_sha"] == fixture.head

    asyncio.run(scenario())


@pytest.mark.parametrize("both,expected", [(False, "merged"), (True, "all-green")])
def test_merge_method_fallback(tmp_path, both, expected):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        fixture.failures[("gh", "pr", "merge", "5", "--squash")] = command_result(
            code=1, stderr="squash disabled"
        )
        if both:
            fixture.failures[("gh", "pr", "merge", "5", "--merge")] = command_result(
                code=1, stderr="blocked"
            )
        result = await run_units_step(fixture.input())
        assert result["outputs"]["units"][0]["verdict"] == expected
        assert any(args[-1] == "--merge" for cmd, args, _ in fixture.calls if cmd == "gh")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,write_plan,blueprint",
    [("no-access", False, False), ("ready", False, False), ("insufficient-context", False, True)],
)
def test_plan_failure_statuses(tmp_path, status, write_plan, blueprint):
    async def scenario():
        fixture = ModelFixture(tmp_path)

        def plan(req, nth):
            path = Path(engine_plan_path(fixture.ctx))
            if blueprint:
                path.with_name("BLUEPRINT-PROJ-1.md").write_text("# Blueprint")
            return answer({"plan_path": str(path), "status": status})

        fixture.scripts["plan"] = plan
        result = await run_units_step(fixture.input())
        assert result["outputs"]["units"][0]["verdict"] == "failed"
        assert not fixture.counts["implement"]
        assert ("blueprint" in read_unit(fixture.run_dir, "PROJ-1")) == blueprint

    asyncio.run(scenario())


@pytest.mark.parametrize("done,commits", [(0, 1), (1, 0)])
def test_implement_requires_done_and_real_commit(tmp_path, done, commits):
    async def scenario():
        fixture = ModelFixture(tmp_path)

        def implement(req, nth):
            fixture.commits += commits
            return answer({"waves": 1, "tasks": 1, "done": done, "failed": 0, "commits": 100})

        fixture.scripts["implement"] = implement
        result = await run_units_step(fixture.input())
        assert "failed=1" in result["verdict"] and not fixture.counts["review"]

    asyncio.run(scenario())


def test_plan_pipeline_seed_and_acquire_release(tmp_path):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        plan = fixture.repo / "PLAN-feature.md"
        plan.write_text("# Existing plan")
        fixture.step["pipeline"] = "plan"
        acquired = []
        released = []

        async def acquire(harness, signal):
            acquired.append(harness)
            return lambda: released.append(harness)

        result = await run_units_step(
            fixture.input(
                items=[str(plan)],
                agent={"starter": fixture.starter, "step_token": "token", "acquire": acquire},
            )
        )
        assert "merged=1" in result["verdict"] and acquired == released and len(acquired) == 5
        assert str(plan) in fixture.child_calls[0][2]["prompt"]
        assert result["outputs"]["units"][0]["unit"]["ref"] == "feature"

    asyncio.run(scenario())


def test_cancel_mid_phase_kills_and_records_cursor(tmp_path):
    async def scenario():
        fixture = ModelFixture(tmp_path)
        signal = asyncio.Event()
        entered = asyncio.Event()
        killed = []

        async def starter(harness, req, on_event):
            done = asyncio.get_running_loop().create_future()

            def kill(sig):
                killed.append(sig)
                if not done.done():
                    done.set_result(
                        {
                            "text": "",
                            "usage": empty_usage(),
                            "exit": "error",
                            "error": "terminated",
                            "cursor": "partial-session",
                        }
                    )

            entered.set()
            return AgentHandle(done=done, kill=kill)

        task = asyncio.create_task(
            run_units_step(
                fixture.input(signal=signal, agent={"starter": starter, "step_token": "token"})
            )
        )
        await entered.wait()
        signal.set()
        result = await asyncio.wait_for(task, 2)
        assert killed == ["SIGTERM"]
        assert "cancelled" in result["outputs"]["units"][0]["reason"]
        assert read_unit(fixture.run_dir, "PROJ-1")["cursors"]["plan"] == "partial-session"

    asyncio.run(scenario())
