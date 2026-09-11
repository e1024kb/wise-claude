from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
from typing import Any

import pytest

from wise_engine.adapter_types import AgentHandle
from wise_engine.daemon import DaemonRuntime, daemon_paths
from wise_engine.executor import create_executor, load_caps, default_backoff_ms, detect_project
from wise_engine.ledger import read_state, read_events, utc_now, usage_total
from wise_engine.preflight import fill_answers
from wise_engine.rpc import RpcError, domain_code, CallContext

ENGINE = Path(__file__).resolve().parents[1]
FIXTURES = ENGINE / "test/fixtures/executor"
BUNDLED = ENGINE.parent / "workflows"
Json = dict[str, Any]


def usage(input=100, output=10, pool="subscription"):
    return dict(input=input, output=output, cache_read=0, cache_write=0, pool=pool)


def schema_answer(req, **extra):
    result = {}
    for name, spec in req.get("schema", {}).get("properties", {}).items():
        result[name] = (
            spec["enum"][0]
            if spec.get("enum")
            else 1
            if spec.get("type") in ("integer", "number")
            else True
            if spec.get("type") == "boolean"
            else f"{name}-value"
        )
    return dict(
        text=f"answered {','.join(result)}",
        json=result,
        usage=usage(pool=req.get("auth", "subscription")),
        cursor="sess-" + ("-".join(result) or "none"),
        exit="ok",
        **extra,
    )


class FakeAdapter:
    def __init__(self, identifier="claude", script=None, logged_in=True, delay=0.005):
        self.id = identifier
        self.script = script
        self.logged_in = logged_in
        self.delay = delay
        self.calls = []
        self.probes = []
        self.in_flight = self.max_in_flight = 0

    async def probe_auth(self, auth):
        self.probes.append(auth)
        return dict(ok=self.logged_in, login_cmd=f"{self.id} login")

    async def run(self, req, on_event):
        self.calls.append(req)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            result = self.script(req, len(self.calls)) if self.script else schema_answer(req)
            return await result if inspect.isawaitable(result) else result
        finally:
            self.in_flight -= 1

    def effort_map(self, effort):
        return effort


class Rig:
    def __init__(self, root, **options):
        self.root = root
        self.cwd = str(root / "project")
        Path(self.cwd).mkdir(exist_ok=True)
        self.logs = []
        env = dict(HOME=str(root), XDG_DATA_HOME=str(root), PATH=os.environ.get("PATH", ""))
        self.rt = DaemonRuntime(
            daemon_paths(env=env), "test", os.getpid(), utc_now(), self.logs.append
        )
        self.adapter = FakeAdapter()
        self.executor = create_executor(
            self.rt,
            dict(
                env=env,
                roots=dict(user_root=str(FIXTURES), bundled_root=str(BUNDLED)),
                config_path=str(root / "engine.json"),
                backoff_ms=lambda attempt: 20,
                adapters={"claude": self.adapter},
            )
            | options,
        )
        self.ctx = CallContext(lambda *args: None, asyncio.Event(), 0)

    async def conduct(self, workflow="single-agent", answers=None, inputs=None, context=None):
        answers = dict(answers or {})
        answers.update({f"input.{key}": value for key, value in (inputs or {}).items()})
        for _ in range(32):
            pre = await self.executor.preflight(
                dict(workflow=workflow, cwd=self.cwd, answers=answers), self.ctx
            )
            filled = fill_answers(pre["questions"], answers)["answers"]
            if len(filled) == len(answers):
                break
            answers = filled
        return await self.executor.run(
            dict(
                workflow=workflow,
                cwd=self.cwd,
                answers=answers,
                inputs=inputs or {},
                context=context or {},
            ),
            self.ctx,
        )

    def state(self, run_id):
        return read_state(self.rt.require_run_dir(run_id))

    async def until(self, predicate, seconds=10):
        async with asyncio.timeout(seconds):
            while not predicate():
                await asyncio.sleep(0.005)

    async def status(self, run_id, *statuses):
        await self.until(lambda: self.state(run_id)["status"] in statuses)
        return self.state(run_id)

    async def close(self):
        self.executor.stop()
        if self.executor.tasks:
            await asyncio.wait_for(asyncio.gather(*self.executor.tasks, return_exceptions=True), 3)


def test_caps_backoff_project(tmp_path):
    config = tmp_path / "engine.json"
    assert load_caps(str(config))["global"] == 4
    config.write_text('{"concurrency":{"global":3,"claude":1,"codex":0,"grok":true}}')
    assert load_caps(str(config), {"harness": {"cursor": 2}}) == {
        "global": 3,
        "harness": {"claude": 1, "codex": 1, "cursor": 2, "gemini": 1, "grok": 1},
    }
    assert [default_backoff_ms(n) for n in range(1, 8)] == [
        60000,
        120000,
        240000,
        480000,
        960000,
        1800000,
        1800000,
    ]
    assert detect_project(str(tmp_path))["kind"] == "other"
    (tmp_path / "pyproject.toml").touch()
    assert detect_project(str(tmp_path))["kind"] == "python"


def test_preflight_strict_answers_and_errors(tmp_path):
    async def scenario():
        rig = Rig(tmp_path)
        try:
            with pytest.raises(RpcError) as error:
                await rig.executor.run(dict(workflow="single-agent", cwd=rig.cwd))
            assert domain_code(error.value) == "MISSING_ANSWERS"
            assert rig.rt.list_run_dirs() == []
            for workflow, expected in [
                ("missing", "WORKFLOW_NOT_FOUND"),
                ("broken", "WORKFLOW_INVALID"),
            ]:
                with pytest.raises(RpcError) as error:
                    await rig.executor.preflight(dict(workflow=workflow, cwd=rig.cwd))
                assert domain_code(error.value) == expected
            with pytest.raises(RpcError) as error:
                await rig.executor.preflight(
                    dict(
                        workflow="single-agent",
                        cwd=rig.cwd,
                        answers={"permissions.claude": "unsafe"},
                    )
                )
            assert domain_code(error.value) == "MISSING_ANSWERS"
        finally:
            await rig.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "workflow,status",
    [
        ("single-agent", "completed"),
        ("sync-approval", "completed"),
        ("three-parallel", "completed"),
    ],
)
def test_basic_execution(tmp_path, workflow, status):
    async def scenario():
        rig = Rig(tmp_path)
        try:
            run = await rig.conduct(workflow)
            state = await rig.status(run["run_id"], status, "failed")
            assert state["status"] == status, (state, rig.logs)
            assert rig.adapter.max_in_flight <= 2
            events = read_events(rig.rt.require_run_dir(run["run_id"]))
            assert events[0]["type"] == "run.started"
            assert events[-1]["type"] == "run.done"
            assert not rig.executor.live_runs()
        finally:
            await rig.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("answer,status", [("approve", "completed"), ("reject", "failed")])
def test_approval_gate(tmp_path, answer, status):
    async def scenario():
        rig = Rig(tmp_path)
        try:
            run = await rig.conduct("approval")
            state = await rig.status(run["run_id"], "gated", "failed")
            assert state["status"] == "gated", (state, rig.logs)
            assert state["gate"]["message"] == "Ship prepared?"
            with pytest.raises(RpcError) as error:
                rig.executor.answer(dict(run_id=run["run_id"], gate_id="wrong", value=answer))
            assert domain_code(error.value) == "GATE_STALE"
            rig.executor.answer(
                dict(run_id=run["run_id"], gate_id=state["gate"]["gate_id"], value=answer)
            )
            assert (await rig.status(run["run_id"], status))["status"] == status
        finally:
            await rig.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "exit,error", [("timeout", "timeout"), ("error", "broken"), ("auth", "AUTH_REQUIRED")]
)
def test_child_failure(tmp_path, exit, error):
    async def scenario():
        fake = FakeAdapter(
            script=lambda req, n: {**schema_answer(req), "exit": exit, "error": error}
        )
        rig = Rig(tmp_path, adapters={"claude": fake})
        try:
            run = await rig.conduct()
            state = await rig.status(run["run_id"], "failed")
            assert error in state["error"]
            assert not rig.executor.live_runs()
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_auth_requires_and_inputs_precede_run_creation(tmp_path):
    async def scenario():
        fake = FakeAdapter(logged_in=False)
        rig = Rig(tmp_path, adapters={"claude": fake})
        try:
            for workflow, expected in [
                ("single-agent", "AUTH_REQUIRED"),
                ("required-input", "MISSING_ANSWERS"),
                ("requires-missing", "REQUIRES_MISSING"),
            ]:
                with pytest.raises(RpcError) as error:
                    await rig.conduct(workflow)
                assert domain_code(error.value) == expected
                assert rig.rt.list_run_dirs() == []
        finally:
            await rig.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("fallback", ["ok", "missing", "logged-out"])
def test_rate_limit_retry_and_fallback(tmp_path, fallback):
    async def scenario():
        claude = FakeAdapter(
            script=lambda req, n: {
                **schema_answer(req),
                **({"exit": "rate_limited", "error": "quota"} if n == 1 else {}),
            }
        )
        codex = FakeAdapter("codex", logged_in=fallback != "logged-out")
        adapters = {"claude": claude}
        if fallback != "missing":
            adapters["codex"] = codex
        rig = Rig(tmp_path, adapters=adapters, backoff_ms=lambda n: 50)
        try:
            run = await rig.conduct("fallback")
            state = await rig.status(run["run_id"], "completed", "failed")
            assert state["status"] == "completed", (state, rig.logs)
            assert len(codex.calls) == (1 if fallback == "ok" else 0)
            assert len(claude.calls) == (1 if fallback == "ok" else 2)
            if codex.calls:
                assert codex.calls[0]["model"] == "inherit"
                assert codex.calls[0]["prompt"] == claude.calls[0]["prompt"]
                assert "resume" not in codex.calls[0]
            events = read_events(rig.rt.require_run_dir(run["run_id"]))
            assert any("rate limited" in event.get("message", "") for event in events)
        finally:
            await rig.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("decision", ["approve", "reject", "synchronous"])
def test_ceiling(tmp_path, decision):
    async def scenario():
        rig = Rig(tmp_path)
        try:
            run = await rig.conduct("sync-ceiling" if decision == "synchronous" else "ceiling")
            if decision == "synchronous":
                state = await rig.status(run["run_id"], "failed")
                assert "ceiling" in state["error"]
                return
            state = await rig.status(run["run_id"], "gated", "failed")
            assert state["status"] == "gated", (state, rig.logs)
            assert state["gate"]["ceiling"] == dict(used=220, limit=150)
            rig.executor.answer(
                dict(run_id=run["run_id"], gate_id=state["gate"]["gate_id"], value=decision)
            )
            if decision == "reject":
                assert (await rig.status(run["run_id"], "failed"))["error"].endswith("rejected")
            else:
                state = await rig.status(run["run_id"], "gated", "completed")
                if state["status"] == "gated":
                    assert state["gate"]["ceiling"]["limit"] == 300
                    rig.executor.answer(
                        dict(
                            run_id=run["run_id"], gate_id=state["gate"]["gate_id"], value="approve"
                        )
                    )
                state = await rig.status(run["run_id"], "completed")
                assert usage_total(state["usage"])["input"] == 300
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_global_caps_across_runs(tmp_path):
    async def scenario():
        rig = Rig(tmp_path, concurrency={"global": 1, "harness": {"claude": 3}})
        try:
            runs = [await rig.conduct("three-parallel") for _ in range(2)]
            await asyncio.gather(*(rig.status(run["run_id"], "completed") for run in runs))
            assert len(rig.adapter.calls) == 6
            assert rig.adapter.max_in_flight == 1
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_missing_schema_output_and_start_exception_fail_without_hang(tmp_path):
    async def scenario():
        fake = FakeAdapter(script=lambda req, n: {**schema_answer(req), "json": {}})
        rig = Rig(tmp_path, adapters={"claude": fake})
        try:
            run = await rig.conduct()
            state = await rig.status(run["run_id"], "failed")
            assert "answer" in state["error"]

            async def fail(*args):
                raise OSError("could not spawn")

            rig.executor.opts["start_agent"] = fail
            run = await rig.conduct()
            assert "could not spawn" in (await rig.status(run["run_id"], "failed"))["error"]
        finally:
            await rig.close()

    asyncio.run(scenario())


class Held:
    def __init__(self):
        self.calls = []
        self.nudges = []
        self.kills = []

    async def start(self, harness, req, on_event):
        done = asyncio.get_running_loop().create_future()
        self.calls.append((req, done, on_event))

        def kill(signal):
            self.kills.append(signal)
            if not done.done():
                done.set_result({**schema_answer(req), "exit": "error", "error": "killed"})

        return AgentHandle(done=done, kill=kill, nudge=self.nudges.append)

    def finish(self, index=0):
        req, done, _ = self.calls[index]
        if not done.done():
            done.set_result(schema_answer(req))


def test_child_channel_ask_report_context_checkpoint_and_token_end(tmp_path):
    async def scenario():
        held = Held()
        rig = Rig(tmp_path, start_agent=held.start)
        try:
            run = await rig.conduct("channel", context={"decisions": {"test": "yes"}})
            identifier = run["run_id"]
            await rig.until(lambda: bool(held.calls))
            token = rig.executor.step_token(identifier, "work")
            assert rig.executor.step_by_token(token) == dict(run_id=identifier, step="work")
            config = held.calls[0][0]["mcp_config"]["mcpServers"]["wise-engine"]
            assert config["env"]["WISE_STEP_TOKEN"] == token
            assert config["args"] == ["-m", "wise_engine", "unit-mcp"]
            report = rig.executor.child_report(
                dict(token=token, kind="progress", text="x" * 1000, data={"ok": True})
            )
            assert report["accepted"]
            assert rig.executor.child_context(dict(token=token, key="prep_out")) == {
                "value": "prepared"
            }
            path = rig.executor.child_checkpoint(dict(token=token, data={"at": 3}))["path"]
            assert json.loads(Path(path).read_text()) == {"at": 3}
            asking = asyncio.create_task(
                rig.executor.child_ask(
                    dict(token=token, question="Proceed?", options=["yes", "no"], timeout_ms=1000),
                    rig.ctx,
                )
            )
            state = await rig.status(identifier, "gated")
            rig.executor.answer(
                dict(run_id=identifier, gate_id=state["gate"]["gate_id"], value="yes")
            )
            answer = await asking
            assert answer["status"] == "answered" and answer["value"] == "yes"
            assert held.nudges == ["Answer to your question: yes"]
            summary = await rig.executor.status(dict(run_id=identifier))
            assert summary["children"][0]["step"] == "work"
            held.finish()
            await rig.status(identifier, "completed")
            assert rig.executor.step_token(identifier, "work") is None
            with pytest.raises(RpcError) as error:
                rig.executor.child_context(dict(token=token, key="inputs"))
            assert domain_code(error.value) == "TOKEN_INVALID"
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_child_ask_disconnect_repoll_orphan_and_cancel(tmp_path):
    async def scenario():
        held = Held()
        rig = Rig(tmp_path, start_agent=held.start)
        try:
            run = await rig.conduct("channel")
            await rig.until(lambda: bool(held.calls))
            token = rig.executor.step_token(run["run_id"], "work")
            ctx = CallContext(lambda *args: None, asyncio.Event(), 1)
            waiting = asyncio.create_task(
                rig.executor.child_ask(
                    dict(token=token, question="Continue?", timeout_ms=10000), ctx
                )
            )
            await rig.status(run["run_id"], "gated")
            ctx.signal.set()
            pending = await asyncio.wait_for(waiting, 1)
            assert pending["status"] == "pending"
            polled = await rig.executor.child_ask(
                dict(token=token, question="Continue?", ask_id=pending["ask_id"], timeout_ms=0),
                rig.ctx,
            )
            assert polled == pending
            held.finish()
            state = await rig.status(run["run_id"], "completed")
            assert "gate" not in state
            run = await rig.conduct("channel")
            await rig.until(lambda: len(held.calls) == 2)
            rig.executor.cancel(dict(run_id=run["run_id"]))
            assert (await rig.status(run["run_id"], "cancelled"))["status"] == "cancelled"
            assert held.kills
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_example_workflow_end_to_end(tmp_path):
    async def scenario():
        rig = Rig(tmp_path)
        try:
            run = await rig.conduct(
                str(BUNDLED / "example-workflow/workflow.yaml"),
                answers={"profile": "medium", "input.focus": "speed"},
            )
            identifier = run["run_id"]
            state = await rig.status(identifier, "gated", "failed")
            assert state["status"] == "gated", (state, rig.logs)
            assert state["gate"]["kind"] == "ask"
            rig.executor.answer(
                dict(
                    run_id=identifier,
                    gate_id=state["gate"]["gate_id"],
                    value=state["gate"]["options"][0]["value"],
                )
            )
            state = await rig.status(identifier, "gated", "failed")
            assert state["gate"]["kind"] == "approval"
            rig.executor.answer(
                dict(run_id=identifier, gate_id=state["gate"]["gate_id"], value="approve")
            )
            state = await rig.status(identifier, "completed", "failed")
            assert state["status"] == "completed", (state, rig.logs)
            report = rig.executor.report(dict(run_id=identifier))
            assert report["usage_total"]["input"] > 0
            assert report["verdicts"]
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_resume_and_pickup_preserve_completed_work(tmp_path):
    from wise_engine.ledger import update_run, update_step, reset_running

    async def scenario():
        rig = Rig(tmp_path)
        try:
            run = await rig.conduct()
            await rig.status(run["run_id"], "completed")
            directory = rig.rt.require_run_dir(run["run_id"])
            update_run(directory, {"status": "paused"})
            update_step(directory, "answer", {"status": "running"})
            resumed = await rig.executor.resume(dict(run_id=run["run_id"]))
            assert resumed["status"] == "running"
            await rig.status(run["run_id"], "completed")
            assert len(rig.adapter.calls) == 2
            update_run(directory, {"status": "running"})
            update_step(directory, "answer", {"status": "running"})
            reset_running(directory)
            assert rig.executor.pick_up() == [run["run_id"]]
            await rig.status(run["run_id"], "completed")
            assert len(rig.adapter.calls) == 3
            update_run(directory, {"status": "paused"})
            assert rig.executor.pick_up() == []
        finally:
            await rig.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode,calls", [("on", 1), ("off", 0)])
def test_explicit_input_staging(tmp_path, mode, calls):
    async def scenario():
        rig = Rig(tmp_path)
        try:
            run = await rig.conduct("gated-tuning", inputs={"mode": mode})
            assert (await rig.status(run["run_id"], "completed"))["inputs"]["mode"] == mode
            assert len(rig.adapter.calls) == calls
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_stale_child_nudge_then_kill_and_human_gate_pause(tmp_path):
    async def scenario():
        held = Held()
        rig = Rig(tmp_path, start_agent=held.start, channel={"stale_after_secs": 0.025})
        try:
            run = await rig.conduct("channel")
            await rig.until(lambda: bool(held.calls))
            token = rig.executor.step_token(run["run_id"], "work")
            pending = await rig.executor.child_ask(
                dict(token=token, question="Wait?", timeout_ms=0)
            )
            assert pending["status"] == "pending"
            await asyncio.sleep(0.08)
            assert not held.kills and not held.nudges
            state = rig.state(run["run_id"])
            rig.executor.answer(
                dict(run_id=run["run_id"], gate_id=state["gate"]["gate_id"], value="yes")
            )
            state = await rig.status(run["run_id"], "failed")
            assert state["steps"]["work"]["error"] == "stale"
            assert len(held.nudges) == 2
            assert held.kills == ["SIGTERM"]
            assert state["steps"]["work"]["cursor"].startswith("sess-")
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_synchronous_child_decisions_and_needs_human(tmp_path):
    async def scenario():
        held = Held()
        rig = Rig(tmp_path, start_agent=held.start)
        try:
            run = await rig.conduct(
                "channel",
                answers={"control-mode": "synchronous"},
                context={"decisions": {"Proceed?": "yes"}},
            )
            await rig.until(lambda: bool(held.calls))
            token = rig.executor.step_token(run["run_id"], "work")
            assert (await rig.executor.child_ask(dict(token=token, question="Proceed?")))[
                "value"
            ] == "yes"
            assert (await rig.executor.child_ask(dict(token=token, question="Unknown?")))[
                "status"
            ] == "needs-human"
            assert rig.state(run["run_id"])["status"] == "running"
            held.finish()
            await rig.status(run["run_id"], "completed")
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_cancel_real_bash_child(tmp_path):
    from wise_engine.daemon import pid_alive

    async def scenario():
        rig = Rig(tmp_path)
        try:
            run = await rig.conduct("slow-bash")
            live = rig.executor.lives[run["run_id"]]
            await rig.until(lambda: bool(live.children))
            pid = next(iter(live.children.values())).pid
            assert pid_alive(pid)
            rig.executor.cancel(dict(run_id=run["run_id"]))
            await rig.until(lambda: not pid_alive(pid))
            assert rig.state(run["run_id"])["status"] == "cancelled"
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_cancel_during_async_spawn_reaps_new_child(tmp_path):
    async def scenario():
        spawning, proceed = asyncio.Event(), asyncio.Event()
        held = Held()

        async def delayed(*args):
            spawning.set()
            await proceed.wait()
            return await held.start(*args)

        rig = Rig(tmp_path, start_agent=delayed)
        try:
            run = await rig.conduct()
            await spawning.wait()
            rig.executor.cancel(dict(run_id=run["run_id"]))
            proceed.set()
            await rig.until(lambda: not rig.executor.tasks)
            assert held.kills == ["SIGTERM"]
            assert rig.state(run["run_id"])["status"] == "cancelled"
        finally:
            proceed.set()
            await rig.close()

    asyncio.run(scenario())


def test_units_pipeline_shares_slots_usage_and_report(tmp_path):
    from test_model_phases import ModelFixture

    async def scenario():
        fixture = ModelFixture(tmp_path)
        in_flight = maximum = 0

        async def starter(harness, req, on_event):
            nonlocal in_flight, maximum
            handle = await fixture.starter(harness, req, on_event)
            in_flight += 1
            maximum = max(maximum, in_flight)

            async def result():
                nonlocal in_flight
                try:
                    await asyncio.sleep(0.005)
                    return await handle.done
                finally:
                    in_flight -= 1

            return AgentHandle(done=asyncio.create_task(result()))

        rig = Rig(
            tmp_path, start_agent=starter, units_exec=fixture.execute, concurrency={"global": 1}
        )
        try:
            run = await rig.conduct(
                "units-parallel",
                inputs={"tickets": "PROJ-1, PROJ-2"},
                context={"ticket": fixture.ctx["config"]["tickets"]},
            )
            state = await rig.status(run["run_id"], "completed", "failed")
            assert state["status"] == "completed", (state, rig.logs)
            report = rig.executor.report(dict(run_id=run["run_id"]))
            assert len(report["units"]) == 2
            assert maximum == 1
            assert report["usage_total"]["input"] > 0
            assert state["resolved"]["process.plan"]["harness"] == "claude"
            assert not rig.executor.is_busy()
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_cancelled_slot_waiter_returns_transferred_capacity(tmp_path):
    async def scenario():
        rig = Rig(tmp_path, concurrency={"global": 1})
        try:
            release = rig.executor.take_slot("claude")
            waiting = asyncio.create_task(rig.executor.acquire_slot("claude"))
            await asyncio.sleep(0)
            assert len(rig.executor.slot_waiters) == 1
            release()
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            assert rig.executor.in_flight_global == 0
            assert rig.executor.in_flight["claude"] == 0
            assert not rig.executor.slot_waiters
            next_release = await asyncio.wait_for(rig.executor.acquire_slot("claude"), 1)
            next_release()
            assert not rig.executor.is_busy()
        finally:
            await rig.close()

    asyncio.run(scenario())
