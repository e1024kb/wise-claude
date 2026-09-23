from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from wise_engine.adapter_types import AgentHandle
from wise_engine.daemon import DaemonRuntime, daemon_paths
from wise_engine.defs import load_and_validate
from wise_engine.executor import (
    create_executor,
    default_backoff_ms,
    detect_project,
    load_caps,
    workflow_branch_component,
    workflow_manages_worktrees,
)
from wise_engine.ledger import read_state, read_events, utc_now, usage_total
from wise_engine.preflight import build_questionary, fill_answers
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


def test_workflow_branch_component_is_git_ref_safe_and_bounded():
    assert workflow_branch_component("team plan: review") == "team-plan-review"
    assert workflow_branch_component(":::") == "workflow"
    assert len(workflow_branch_component("a" * 200)) == 80


def test_only_enabled_units_steps_manage_worktrees():
    definition = {
        "inputs": [],
        "steps": [
            {"id": "prepare", "type": "agent"},
            {"id": "batch", "type": "units"},
        ],
    }
    assert not workflow_manages_worktrees(definition, {"prepare"})
    assert not workflow_manages_worktrees(definition, {"prepare", "batch"})
    assert workflow_manages_worktrees(definition, {"batch"})
    definition["steps"].append({"id": "confirm", "type": "approval"})
    assert workflow_manages_worktrees(definition, {"batch", "confirm"})
    definition["inputs"] = [{"name": "worktree_mode"}]
    assert workflow_manages_worktrees(definition, {"prepare", "batch"})


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


def test_preflight_uses_request_context_for_input_defaults(tmp_path):
    async def scenario():
        rig = Rig(tmp_path)
        definitions = tmp_path / "context-definitions"
        definitions.mkdir()
        (definitions / "context.yaml").write_text(
            "version: 2\n"
            "name: context\n"
            "inputs:\n"
            "  - name: guidance\n"
            "    prompt: Guidance?\n"
            "    from-context: guidance\n"
            "steps:\n"
            "  - id: answer\n"
            "    type: agent\n"
            "    prompt: Answer.\n"
        )
        rig.executor.roots["user_root"] = str(definitions)
        try:
            result = await rig.executor.preflight(
                {
                    "workflow": "context",
                    "cwd": rig.cwd,
                    "answers": {},
                    "context": {"guidance": "keep it small"},
                }
            )
            assert result["defaults"]["input.guidance"] == "keep it small"
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


def test_separate_worktree_runs_ordinary_workflow_on_new_branch(tmp_path):
    async def scenario():
        rig = Rig(tmp_path)
        subprocess.run(["git", "init", "-q", "-b", "main", rig.cwd], check=True)
        source = Path(rig.cwd)
        (source / "tracked.txt").write_text("tracked\n")
        (source / ".worktreeinclude").write_text(".env\n")
        (source / ".env").write_text("LOCAL=1\n")
        subprocess.run(["git", "add", "tracked.txt", ".worktreeinclude"], cwd=source, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-q",
                "-m",
                "initial",
            ],
            cwd=source,
            check=True,
        )
        try:
            run = await rig.conduct(answers={"worktree": "new"})
            state = await rig.status(run["run_id"], "completed")
            selected = Path(state["cwd"])
            assert state["source_cwd"] == str(source)
            assert state["worktree"]["path"] == str(selected)
            assert state["worktree"]["branch"].startswith("wise/single-agent-")
            assert selected != source and (selected / "tracked.txt").is_file()
            assert (selected / ".env").read_text() == "LOCAL=1\n"
            assert rig.adapter.calls[0]["cwd"] == str(selected)
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_separate_worktree_requires_a_git_checkout(tmp_path):
    async def scenario():
        rig = Rig(tmp_path)
        try:
            with pytest.raises(RpcError) as error:
                await rig.conduct(answers={"worktree": "new"})
            assert domain_code(error.value) == "WORKTREE_CREATE_FAILED"
            assert rig.rt.list_run_dirs() == []
            assert not rig.adapter.calls
        finally:
            await rig.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("answers", [{"worktree": "elsewhere"}, {"input.worktree_mode": "x"}])
def test_invalid_worktree_answer_never_falls_back_to_default(tmp_path, answers):
    async def scenario():
        rig = Rig(tmp_path)
        try:
            with pytest.raises(RpcError) as error:
                await rig.executor.run(
                    {
                        "workflow": "single-agent",
                        "cwd": rig.cwd,
                        "answers": {"permissions.claude": "auto", **answers},
                    },
                    rig.ctx,
                )
            assert domain_code(error.value) == "MISSING_ANSWERS"
            assert error.value.data["missing"] == ["worktree"]
            assert [question["id"] for question in error.value.data["questions"]] == ["worktree"]
            assert rig.rt.list_run_dirs() == []
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_invalid_tuning_scope_answer_is_asked_again(tmp_path):
    async def scenario():
        rig = Rig(tmp_path)
        definitions = tmp_path / "scope-definitions"
        definitions.mkdir()
        (definitions / "scope.yaml").write_text(
            "version: 2\n"
            "name: scope\n"
            "tuning:\n"
            "  groups:\n"
            "    - id: plan\n"
            "      default: {harness: claude, model: opus, effort: high}\n"
            "    - id: implement\n"
            "      default: {harness: claude, model: opus, effort: high}\n"
            "steps:\n"
            "  - id: verify\n"
            "    type: bash\n"
            "    run: 'true'\n"
        )
        rig.executor.roots["user_root"] = str(definitions)
        try:
            with pytest.raises(RpcError) as error:
                await rig.executor.run(
                    {
                        "workflow": "scope",
                        "cwd": rig.cwd,
                        "answers": {"worktree": "current", "tuning-scope": "bogus"},
                    },
                    rig.ctx,
                )
            assert domain_code(error.value) == "MISSING_ANSWERS"
            assert error.value.data["missing"] == ["tuning-scope"]
            assert [q["id"] for q in error.value.data["questions"]] == ["tuning-scope"]
            assert rig.rt.list_run_dirs() == []
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_invalid_input_retry_preserves_context_default(tmp_path):
    async def scenario():
        rig = Rig(tmp_path)
        definitions = tmp_path / "retry-definitions"
        definitions.mkdir()
        (definitions / "retry.yaml").write_text(
            "version: 2\n"
            "name: retry\n"
            "inputs:\n"
            "  - name: mode\n"
            "    prompt: Mode?\n"
            "    from-context: guidance\n"
            "    validate: '^(small|large)$'\n"
            "steps:\n"
            "  - id: verify\n"
            "    type: bash\n"
            "    run: 'true'\n"
        )
        rig.executor.roots["user_root"] = str(definitions)
        try:
            with pytest.raises(RpcError) as error:
                await rig.executor.run(
                    {
                        "workflow": "retry",
                        "cwd": rig.cwd,
                        "answers": {"worktree": "current", "input.mode": "invalid"},
                        "context": {"guidance": "small"},
                    },
                    rig.ctx,
                )
            assert domain_code(error.value) == "MISSING_ANSWERS"
            assert error.value.data["questions"][0]["default"] == "small"
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_parallel_children_in_the_current_tree_are_rejected_at_preflight(tmp_path):
    async def scenario():
        rig = Rig(tmp_path)
        definitions = tmp_path / "fanout-definitions"
        definitions.mkdir()
        (definitions / "fanout.yaml").write_text(
            "version: 2\n"
            "name: fanout\n"
            "inputs:\n"
            "  - name: tickets\n"
            "    prompt: Tickets?\n"
            "  - name: concurrency\n"
            "    prompt: Concurrency?\n"
            "    default: '2'\n"
            "    validate: '^(1|2|3|4)$'\n"
            "    needs-fanout: tickets\n"
            "steps:\n"
            "  - id: verify\n"
            "    type: bash\n"
            "    run: 'true'\n"
        )
        rig.executor.roots["user_root"] = str(definitions)
        try:
            with pytest.raises(RpcError) as error:
                await rig.executor.run(
                    {
                        "workflow": "fanout",
                        "cwd": rig.cwd,
                        "answers": {
                            "worktree": "current",
                            "input.tickets": "A-1, B-2",
                            "input.concurrency": "2",
                        },
                    },
                    rig.ctx,
                )
            assert domain_code(error.value) == "MISSING_ANSWERS"
            assert error.value.data["missing"] == ["input.concurrency"]
            assert error.value.data["questions"][0]["default"] == "1"
            assert rig.rt.list_run_dirs() == []
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_shared_worktree_answer_reaches_workflow_managed_input(tmp_path):
    async def scenario():
        rig = Rig(tmp_path)
        definitions = tmp_path / "managed-definitions"
        definitions.mkdir()
        (definitions / "managed.yaml").write_text(
            "version: 2\n"
            "name: managed\n"
            "inputs:\n"
            "  - name: worktree_mode\n"
            "    prompt: Tree?\n"
            "    default: current\n"
            "    validate: '^(current|new)$'\n"
            "steps:\n"
            "  - id: verify\n"
            "    type: bash\n"
            "    run: test '{{worktree_mode}}' = new\n"
        )
        rig.executor.roots["user_root"] = str(definitions)
        try:
            run = await rig.conduct(
                "managed", answers={"worktree": "new"}, inputs={"worktree_mode": "current"}
            )
            state = await rig.status(run["run_id"], "completed")
            assert state["inputs"]["worktree_mode"] == "new"
            assert state["cwd"] == rig.cwd
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


@pytest.mark.parametrize("harness", ["claude", "codex", "cursor", "gemini", "grok"])
def test_dispatch_relay_questions_and_result(tmp_path, harness):
    async def scenario():
        held = Held()
        rig = Rig(tmp_path, start_agent=held.start)
        try:
            run = rig.executor.dispatch_start(
                dict(
                    harness=harness,
                    prompt="literal {{inputs.keep}}",
                    cwd=rig.cwd,
                    mode="auto",
                    **{"add-dir": str(tmp_path / "extra")},
                )
            )
            run_id = run["run_id"]
            await rig.until(lambda: bool(held.calls))
            req = held.calls[0][0]
            assert req["prompt"] == "literal {{inputs.keep}}"
            assert str(tmp_path / "extra") in req["add_dirs"]
            assert (
                req["mcp_config"]["mcpServers"]["wise-engine"]["env"]["WISE_STEP_TOKEN"]
                == req["step_token"]
            )
            for options, value in [(["Allow", "Decline"], "Decline"), (None, "custom decision")]:
                question = dict(
                    token=req["step_token"], question="Main harness decision?", timeout_ms=0
                )
                if options is not None:
                    question["options"] = options
                pending = await rig.executor.child_ask(question)
                assert pending["status"] != "answered"
                assert not held.calls[0][1].done()
                gate = rig.state(run_id)["gate"]
                assert rig.executor.answer(
                    dict(run_id=run_id, gate_id=gate["gate_id"], value=value)
                )["accepted"]
                answered = await rig.executor.child_ask({**question, "ask_id": pending["ask_id"]})
                assert answered["value"] == value
            held.finish()
            await rig.status(run_id, "completed")
            status = await rig.executor.status(dict(run_id=run_id))
            assert status["dispatch_result"]["exit"] == "ok"
            assert len(held.calls) == 1
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_dispatch_relay_restores_pending_question(tmp_path):
    async def scenario():
        held = Held()
        rig = Rig(tmp_path, start_agent=held.start)
        try:
            run_id = rig.executor.dispatch_start(dict(harness="claude", prompt="ask", cwd=rig.cwd))[
                "run_id"
            ]
            await rig.until(lambda: bool(held.calls))
            token = held.calls[0][0]["step_token"]
            question = dict(token=token, question="Continue?", timeout_ms=0)
            pending = await rig.executor.child_ask(question)
            state = rig.state(run_id)
            gate_id = state["gate"]["gate_id"]
            assert state["dispatch_step_token"] == token
            assert state["dispatch_pending_asks"][pending["ask_id"]]["gate_id"] == gate_id

            rig.executor.lives.pop(run_id)
            assert rig.executor.answer(dict(run_id=run_id, gate_id=gate_id, value="Continue"))[
                "accepted"
            ]
            answered = await rig.executor.child_ask({**question, "ask_id": pending["ask_id"]})
            assert answered["value"] == "Continue"
            assert "dispatch_pending_asks" not in rig.state(run_id)

            held.finish()
            await rig.status(run_id, "completed")
        finally:
            await rig.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("exit_code", ["rate_limited", "auth", "timeout", "error"])
def test_dispatch_relay_preserves_failed_result_without_retry(tmp_path, exit_code):
    async def scenario():
        rig = Rig(tmp_path)
        rig.adapter.script = lambda req, count: {
            **schema_answer(req),
            "exit": exit_code,
            "error": "provider failure",
            "text": "partial output",
        }
        try:
            run_id = rig.executor.dispatch_start(dict(harness="claude", prompt="run", cwd=rig.cwd))[
                "run_id"
            ]
            await rig.status(run_id, "failed")
            status = await rig.executor.status(dict(run_id=run_id))
            assert status["dispatch_result"]["text"] == "partial output"
            assert status["dispatch_result"]["exit"] == exit_code
            assert len(rig.adapter.calls) == 1
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_dispatch_relay_refuses_missing_channel_and_invalid_flags(tmp_path):
    async def scenario():
        rig = Rig(tmp_path, channel={"inject": False})
        try:
            with pytest.raises(RpcError):
                rig.executor.dispatch_start(dict(harness="claude", cwd=rig.cwd))
            with pytest.raises(RpcError):
                rig.executor.dispatch_start(
                    dict(harness="not-a-harness", prompt="run", cwd=rig.cwd)
                )
            with pytest.raises(RpcError) as failure:
                rig.executor.dispatch_start(dict(harness="claude", prompt="run", cwd=rig.cwd))
            assert domain_code(failure.value) == "INTERACTION_RELAY_UNAVAILABLE"
            assert not rig.executor.lives and not rig.adapter.calls
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_dispatch_relay_cancel_stops_waiting_child(tmp_path):
    async def scenario():
        held = Held()
        rig = Rig(tmp_path, start_agent=held.start)
        try:
            run_id = rig.executor.dispatch_start(dict(harness="claude", prompt="ask", cwd=rig.cwd))[
                "run_id"
            ]
            await rig.until(lambda: bool(held.calls))
            await rig.executor.child_ask(
                dict(token=held.calls[0][0]["step_token"], question="Continue?", timeout_ms=0)
            )
            rig.executor.cancel(dict(run_id=run_id))
            assert rig.state(run_id)["status"] == "cancelled"
            assert held.kills
            assert not rig.state(run_id).get("gate")
        finally:
            await rig.close()

    asyncio.run(scenario())


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


@pytest.mark.parametrize(
    "context,answers,expected_mode,active",
    [
        ({"decisions": {"mode": "on"}}, {}, "on", True),
        ({"decisions": {"mode": "invalid"}}, {}, "off", False),
        ({"decisions": {"mode": "on"}}, {"input.mode": "off"}, "off", False),
        ({"decisions": {"mode": "on"}}, {"input.mode": ""}, "", False),
    ],
)
def test_context_choice_gates_match_runtime_staging(
    tmp_path, context, answers, expected_mode, active
):
    async def scenario():
        rig = Rig(tmp_path)
        try:
            defn = load_and_validate({"path": str(FIXTURES / "gated-tuning.yaml")})["def"]
            staged = build_questionary(
                defn,
                {"context": context},
                {"worktree": "current", "permissions.claude": "auto", **answers},
            )
            assert ("model.gated" in [question["id"] for question in staged["questions"]]) is active

            params = {
                "workflow": "gated-tuning",
                "cwd": rig.cwd,
                "answers": {"worktree": "current", "permissions.claude": "auto", **answers},
                "context": context,
            }
            if active:
                with pytest.raises(RpcError) as error:
                    await rig.executor.run(params, rig.ctx)
                assert domain_code(error.value) == "MISSING_ANSWERS"
                assert error.value.data["missing"] == ["model.gated"]
            else:
                run = await rig.executor.run(params, rig.ctx)
                state = await rig.status(run["run_id"], "completed")
                assert state["inputs"]["mode"] == expected_mode
                assert rig.adapter.calls == []
        finally:
            await rig.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("answers,expected", [({}, "engines"), ({"input.topic": ""}, "")])
def test_explicit_optional_unset_overrides_context(tmp_path, answers, expected):
    async def scenario():
        rig = Rig(tmp_path)
        try:
            run = await rig.executor.run(
                {
                    "workflow": "channel",
                    "cwd": rig.cwd,
                    "answers": {
                        "worktree": "current",
                        "permissions.claude": "auto",
                        **answers,
                    },
                    "context": {"guidance": "engines"},
                },
                rig.ctx,
            )
            state = await rig.status(run["run_id"], "completed")
            assert state["inputs"]["topic"] == expected
        finally:
            await rig.close()

    asyncio.run(scenario())


def enum_input_rig(tmp_path, default=None, optional=False):
    definitions = tmp_path / "definitions"
    definitions.mkdir()
    optional_line = "    optional: true\n" if optional else ""
    default_line = f'    default: "{default}"\n' if default is not None else ""
    (definitions / "enum-input.yaml").write_text(
        "version: 2\n"
        "name: enum-input\n"
        "inputs:\n"
        "  - name: mode\n"
        "    prompt: Mode?\n"
        f"{optional_line}"
        f"{default_line}"
        "    from-context: decisions.mode\n"
        '    validate: "^(auto|ask)$"\n'
        "steps:\n"
        "  - id: only\n"
        "    type: bash\n"
        '    run: echo "{{mode}}"\n'
    )
    return Rig(
        tmp_path,
        roots={"user_root": str(definitions), "bundled_root": str(BUNDLED)},
    )


@pytest.mark.parametrize(
    "default,optional,answers,context,inputs,expected",
    [
        ("auto", False, {}, {"decisions": {"mode": "ask"}}, {}, "ask"),
        ("invalid", False, {}, {"decisions": {"mode": "ask"}}, {}, "ask"),
        ("auto", False, {}, {"decisions": {"mode": "invalid"}}, {}, "auto"),
        ("ask", False, {"input.mode": "auto"}, {"decisions": {"mode": "ask"}}, {}, "auto"),
        (
            "ask",
            False,
            {"input.mode": "ask"},
            {"decisions": {"mode": "ask"}},
            {"mode": "auto"},
            "auto",
        ),
        ("invalid", True, {}, {}, {}, ""),
        ("auto", True, {"input.mode": ""}, {"decisions": {"mode": "ask"}}, {}, ""),
    ],
)
def test_inferred_choice_runtime_precedence(
    tmp_path, default, optional, answers, context, inputs, expected
):
    async def scenario():
        rig = enum_input_rig(tmp_path, default, optional)
        try:
            run = await rig.executor.run(
                {
                    "workflow": "enum-input",
                    "cwd": rig.cwd,
                    "answers": {"worktree": "current", **answers},
                    "context": context,
                    "inputs": inputs,
                },
                rig.ctx,
            )
            state = await rig.status(run["run_id"], "completed")
            assert state["inputs"]["mode"] == expected
        finally:
            await rig.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "selected,answers,inputs,expected",
    [
        (["gap"], {"input.gap_mode": "ask"}, {}, "ask"),
        ([], {"input.gap_mode": "ask"}, {}, "defaults"),
        ([], {}, {"gap_mode": "ask"}, "defaults"),
    ],
)
def test_run_restores_default_for_deselected_needs_steps_input(
    tmp_path, selected, answers, inputs, expected
):
    definitions = tmp_path / "definitions"
    definitions.mkdir()
    (definitions / "needs-steps.yaml").write_text(
        "version: 2\n"
        "name: needs-steps\n"
        "inputs:\n"
        "  - name: gap_mode\n"
        "    prompt: Gap mode?\n"
        "    default: defaults\n"
        '    validate: "^(defaults|ask)$"\n'
        "    needs-steps: [gap]\n"
        "step-select:\n"
        "  prompt: Which stages?\n"
        "  optional: [gap]\n"
        "steps:\n"
        "  - id: gap\n"
        "    type: bash\n"
        '    run: echo "{{gap_mode}}"\n'
        "  - id: only\n"
        "    type: bash\n"
        '    run: echo "{{gap_mode}}"\n'
    )

    async def scenario():
        rig = Rig(tmp_path, roots={"user_root": str(definitions), "bundled_root": str(BUNDLED)})
        try:
            run = await rig.executor.run(
                {
                    "workflow": "needs-steps",
                    "cwd": rig.cwd,
                    "answers": {"worktree": "current", "step-select": selected, **answers},
                    "inputs": inputs,
                },
                rig.ctx,
            )
            state = await rig.status(run["run_id"], "completed")
            assert state["inputs"]["gap_mode"] == expected
        finally:
            await rig.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "default,answers,context,inputs",
    [
        (None, {"input.mode": "invalid"}, {}, {}),
        (None, {}, {"decisions": {"mode": "invalid"}}, {}),
        ("invalid", {}, {}, {}),
        (None, {}, {}, {"mode": {"invalid": True}}),
    ],
)
def test_run_rejects_invalid_inferred_choice_inputs(tmp_path, default, answers, context, inputs):
    async def scenario():
        rig = enum_input_rig(tmp_path, default)
        try:
            with pytest.raises(RpcError) as error:
                await rig.executor.run(
                    {
                        "workflow": "enum-input",
                        "cwd": rig.cwd,
                        "answers": {"worktree": "current", **answers},
                        "context": context,
                        "inputs": inputs,
                    },
                    rig.ctx,
                )
            assert domain_code(error.value) == "MISSING_ANSWERS"
            assert error.value.data["missing"] == ["input.mode"]
            assert [question["id"] for question in error.value.data["questions"]] == ["input.mode"]
            assert rig.rt.list_run_dirs() == []
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


def test_current_checkout_lock_spans_gates_and_rejects_another_run(tmp_path):
    from wise_engine.units import acquire_checkout_lock

    async def scenario():
        rig = Rig(tmp_path)
        subprocess.run(["git", "init", "-q", rig.cwd], check=True)
        path = Path(rig.cwd) / ".git/wise-current-tree.lock"
        try:
            first = await rig.conduct("approval", inputs={"worktree_mode": "current"})
            state = await rig.status(first["run_id"], "gated")
            with pytest.raises(RuntimeError, match="another workflow"):
                acquire_checkout_lock(path)
            second = await rig.conduct(inputs={"worktree_mode": "current"})
            failed = await rig.status(second["run_id"], "failed")
            assert "another workflow" in failed["error"]
            assert not rig.adapter.calls
            rig.executor.answer(
                dict(run_id=first["run_id"], gate_id=state["gate"]["gate_id"], value="approve")
            )
            await rig.status(first["run_id"], "completed")
            third = await rig.conduct(inputs={"worktree_mode": "current"})
            await rig.status(third["run_id"], "completed")
            with acquire_checkout_lock(path):
                pass
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_current_checkout_lock_propagates_operational_git_errors(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    live = type("Live", (), {"checkout_lock": None})()

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 10)

    monkeypatch.setattr("wise_engine.executor.subprocess.check_output", fail)
    with pytest.raises(subprocess.TimeoutExpired):
        rig.executor.lock_checkout(live, {"cwd": rig.cwd, "inputs": {"worktree_mode": "current"}})


@pytest.mark.parametrize("ending", ["cancel", "fail", "stop", "task-cancel"])
def test_current_checkout_lock_waits_for_child_exit(tmp_path, ending):
    from wise_engine.units import acquire_checkout_lock

    async def scenario():
        held = Held()

        async def slow_exit(*args):
            handle = await held.start(*args)
            handle.kill = held.kills.append
            return handle

        rig = Rig(tmp_path, start_agent=slow_exit)
        subprocess.run(["git", "init", "-q", rig.cwd], check=True)
        path = Path(rig.cwd) / ".git/wise-current-tree.lock"
        try:
            run = await rig.conduct(inputs={"worktree_mode": "current"})
            live = rig.executor.lives[run["run_id"]]
            await rig.until(lambda: bool(live.children))
            if ending == "fail":
                rig.executor.fail_run(live, "test failure")
            elif ending == "stop":
                rig.executor.stop()
            else:
                rig.executor.cancel(dict(run_id=run["run_id"]))
                if ending == "task-cancel":
                    for task in list(rig.executor.tasks):
                        task.cancel()
            await asyncio.sleep(0)
            assert held.kills
            with pytest.raises(RuntimeError, match="another workflow"):
                acquire_checkout_lock(path)
            held.finish()
            await rig.until(lambda: not rig.executor.tasks)
            assert live.dispatches == 0
            with acquire_checkout_lock(path):
                pass
        finally:
            held.finish()
            await rig.close()

    asyncio.run(scenario())


def test_current_checkout_lock_released_after_cancel_before_dispatch_starts(tmp_path):
    from wise_engine.units import acquire_checkout_lock

    async def scenario():
        rig = Rig(tmp_path)
        subprocess.run(["git", "init", "-q", rig.cwd], check=True)
        start_task = rig.executor.task

        def cancel_before_start(coroutine):
            task = start_task(coroutine)
            task.cancel()
            return task

        rig.executor.task = cancel_before_start
        try:
            run = await rig.conduct(inputs={"worktree_mode": "current"})
            live = rig.executor.lives[run["run_id"]]
            await rig.until(lambda: live.checkout_lock is not None)
            await rig.until(lambda: not rig.executor.tasks)
            assert live.dispatches == 0 and not rig.adapter.calls
            rig.executor.cancel(dict(run_id=run["run_id"]))
            with acquire_checkout_lock(Path(rig.cwd) / ".git/wise-current-tree.lock"):
                pass
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_direct_units_lock_blocks_current_tree_agent_workflow(tmp_path):
    from test_phases import PhaseFixture, command_result
    from test_units import minimal_input
    from wise_engine.phases.common import fail
    from wise_engine.units import run_units_step

    async def scenario():
        rig = Rig(tmp_path)
        subprocess.run(["git", "init", "-q", rig.cwd], check=True)
        fixture_root = tmp_path / "units"
        fixture_root.mkdir()
        fixture = PhaseFixture(fixture_root)
        fixture.repo = Path(rig.cwd)
        started, finish = asyncio.Event(), asyncio.Event()

        async def execute(cmd, args, opts):
            if args[-1] == "wise-current-tree.lock":
                return command_result(str(fixture.repo / ".git/wise-current-tree.lock"))
            return await fixture.execute(cmd, args, opts)

        async def plan(ctx):
            started.set()
            await finish.wait()
            return fail("test finished")

        params = minimal_input(fixture, exec=execute, runners={"plan": plan})
        params["state"]["inputs"] = {"worktree_mode": "current"}
        units = asyncio.create_task(run_units_step(params))
        try:
            await asyncio.wait_for(started.wait(), 2)
            run = await rig.conduct(inputs={"worktree_mode": "current"})
            state = await rig.status(run["run_id"], "failed")
            assert "another workflow" in state["error"] and not rig.adapter.calls
            finish.set()
            await units
            resumed = await rig.executor.resume(dict(run_id=run["run_id"]))
            await rig.status(resumed["run_id"], "completed")
            assert len(rig.adapter.calls) == 1
        finally:
            finish.set()
            await units
            await rig.close()

    asyncio.run(scenario())


def test_current_tree_resume_refuses_branch_changed_since_setup(tmp_path):
    from wise_engine.ledger import update_run, update_step
    from wise_engine.units import acquire_checkout_lock

    async def scenario():
        rig = Rig(tmp_path)
        subprocess.run(["git", "init", "-q", "-b", "main", rig.cwd], check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "initial",
            ],
            cwd=rig.cwd,
            check=True,
        )
        try:
            run = await rig.conduct(inputs={"worktree_mode": "current"})
            await rig.status(run["run_id"], "completed")
            directory = rig.rt.require_run_dir(run["run_id"])
            update_run(
                directory,
                {
                    "status": "paused",
                    "outputs": {"work_path": rig.cwd, "work_branch": "main"},
                },
            )
            update_step(directory, "answer", {"status": "running"})
            subprocess.run(["git", "checkout", "-q", "-b", "other"], cwd=rig.cwd, check=True)
            await rig.executor.resume(dict(run_id=run["run_id"]))
            state = await rig.status(run["run_id"], "failed")
            assert "branch changed since setup" in state["error"]
            assert len(rig.adapter.calls) == 1
            with acquire_checkout_lock(Path(rig.cwd) / ".git/wise-current-tree.lock"):
                pass
        finally:
            await rig.close()

    asyncio.run(scenario())


async def legacy_ticket_run(tmp_path, workflow="ticket-plan", inputs=None, outputs=None):
    from wise_engine.ledger import write_state

    rig = Rig(tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "main", rig.cwd], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "initial",
        ],
        cwd=rig.cwd,
        check=True,
    )
    run = await rig.conduct()
    state = await rig.status(run["run_id"], "completed")
    definitions = tmp_path / "legacy-definitions"
    definitions.mkdir()
    (definitions / f"{workflow}.yaml").write_text(
        f"version: 2\nname: {workflow}\n"
        "inputs:\n  - name: worktree_mode\n    prompt: Tree?\n"
        "    validate: '^(current|new)$'\n"
        "steps:\n  - id: setup\n    type: bash\n    run: echo setup\n"
        "  - id: implement\n    type: agent\n    model: haiku\n"
        "    prompt: '{{worktree_mode}} {{work_path}}'\n"
        "    depends_on: [setup]\n"
    )
    rig.executor.roots["user_root"] = str(definitions)
    state.update(
        status="paused",
        workflow={"name": workflow, "version": 2, "dir": str(definitions)},
        inputs=dict(inputs or {}),
        outputs=dict(outputs or {"work_branch": "main", "implement_choice": "yes"}),
    )
    saved = state["steps"]["answer"]
    state["steps"] = {
        "setup": {**saved, "outputs": dict(state["outputs"])},
        "implement": {"status": "pending"},
    }
    write_state(rig.rt.require_run_dir(run["run_id"]), state)
    rig.adapter.calls.clear()
    return rig, run["run_id"]


@pytest.mark.parametrize("workflow,mode", [("ticket-plan", "current"), ("ticket-auto", "new")])
def test_legacy_ticket_resume_restores_tree_mode_and_setup_path(tmp_path, workflow, mode):
    async def scenario():
        rig, run_id = await legacy_ticket_run(tmp_path, workflow)
        try:
            await rig.executor.resume(dict(run_id=run_id))
            state = await rig.status(run_id, "completed")
            assert state["inputs"]["worktree_mode"] == mode
            if workflow == "ticket-plan":
                assert state["outputs"]["work_path"] == rig.cwd
                assert state["steps"]["setup"]["outputs"]["work_path"] == rig.cwd
                assert rig.adapter.calls[0]["prompt"].startswith(f"current {rig.cwd}")
            else:
                assert rig.adapter.calls[0]["prompt"].startswith("new ")
        finally:
            await rig.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["current", "new"])
def test_resumed_ticket_plan_refuses_detached_implementation(tmp_path, mode):
    from wise_engine.ledger import update_run

    async def scenario():
        rig, run_id = await legacy_ticket_run(tmp_path, inputs={"worktree_mode": mode})
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=rig.cwd, text=True).strip()
        path = Path(rig.cwd)
        if mode == "new":
            path = Path(rig.rt.require_run_dir(run_id)) / "worktrees/PROJ-1"
            path.parent.mkdir()
            subprocess.run(
                ["git", "worktree", "add", "-q", "--detach", str(path)], cwd=rig.cwd, check=True
            )
        else:
            subprocess.run(["git", "checkout", "-q", "--detach"], cwd=rig.cwd, check=True)
        update_run(
            rig.rt.require_run_dir(run_id),
            {
                "outputs": {
                    "work_branch": "HEAD",
                    "work_path": str(path),
                    "work_head": head,
                    "ticket_ref": "PROJ-1",
                },
            },
        )
        try:
            await rig.executor.resume(dict(run_id=run_id))
            state = await rig.status(run_id, "failed")
            assert "implementation requires the named branch" in state["error"]
            assert not rig.adapter.calls
            assert (
                subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=rig.cwd, text=True
                ).strip()
                == head
            )
        finally:
            await rig.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("saved_head", ["changed", "missing"])
def test_detached_current_resume_checks_setup_commit(tmp_path, saved_head):
    from wise_engine.ledger import update_run

    async def scenario():
        rig, run_id = await legacy_ticket_run(tmp_path, inputs={"worktree_mode": "current"})
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=rig.cwd, text=True).strip()
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "next",
            ],
            cwd=rig.cwd,
            check=True,
        )
        subprocess.run(["git", "checkout", "-q", "--detach"], cwd=rig.cwd, check=True)
        outputs = {"work_branch": "HEAD", "work_path": rig.cwd}
        if saved_head == "changed":
            outputs["work_head"] = head
        update_run(rig.rt.require_run_dir(run_id), {"outputs": outputs})
        try:
            await rig.executor.resume(dict(run_id=run_id))
            state = await rig.status(run_id, "failed")
            assert "detached setup commit changed or is missing" in state["error"]
            assert not rig.adapter.calls
        finally:
            await rig.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode,ref,selected_branch",
    [
        ("current", "PROJ-1", "main"),
        ("new", "PROJ-1", "PROJ-1"),
        ("new", "#678", "abstract-task-678"),
    ],
)
@pytest.mark.parametrize("contaminated_env", [False, True])
def test_ticket_plan_dispatches_in_validated_selected_checkout(
    tmp_path, mode, ref, selected_branch, contaminated_env, monkeypatch
):
    from wise_engine.ledger import update_run

    async def scenario():
        rig, run_id = await legacy_ticket_run(tmp_path, inputs={"worktree_mode": mode})
        path, branch = Path(rig.cwd), selected_branch
        if mode == "new":
            path = Path(rig.rt.require_run_dir(run_id)) / "worktrees" / branch
            path.parent.mkdir()
            subprocess.run(
                ["git", "worktree", "add", "-q", "-b", branch, str(path)], cwd=rig.cwd, check=True
            )
        update_run(
            rig.rt.require_run_dir(run_id),
            {
                "outputs": {"work_branch": branch, "work_path": str(path), "ticket_ref": ref},
            },
        )
        if contaminated_env:
            overrides = {
                "GIT_DIR": str(tmp_path / "unrelated.git"),
                "GIT_WORK_TREE": str(tmp_path / "unrelated"),
                "GIT_INDEX_FILE": str(tmp_path / "unrelated-index"),
            }
            for name, value in overrides.items():
                monkeypatch.setenv(name, value)
            rig.executor.env.update(overrides)
        try:
            await rig.executor.resume(dict(run_id=run_id))
            await rig.status(run_id, "completed")
            assert rig.adapter.calls[0]["cwd"] == str(path)
        finally:
            await rig.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "command",
    [
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        ["git", "rev-parse", "--show-toplevel"],
        ["git", "worktree", "list", "--porcelain"],
    ],
)
@pytest.mark.parametrize("ending", ["complete", "cancel", "task-cancel"])
def test_ticket_checkout_validation_yields_and_drains(tmp_path, monkeypatch, command, ending):
    from threading import Event

    from wise_engine.units import acquire_checkout_lock

    async def scenario():
        rig, run_id = await legacy_ticket_run(tmp_path, inputs={"worktree_mode": "current"})
        entered, released, timed_out = Event(), Event(), Event()
        original_run = subprocess.run

        def held_git(args, **kwargs):
            if args == command:
                entered.set()
                if not released.wait(2):
                    timed_out.set()
            return original_run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", held_git)
        lock_path = Path(rig.cwd) / ".git/wise-current-tree.lock"
        try:
            await rig.executor.resume(dict(run_id=run_id))
            live = rig.executor.lives[run_id]
            await rig.until(entered.is_set)
            assert not timed_out.is_set()
            assert not rig.adapter.calls
            if ending != "complete":
                rig.executor.cancel(dict(run_id=run_id))
                if ending == "task-cancel":
                    for task in list(rig.executor.tasks):
                        task.cancel()
                await asyncio.sleep(0)
                with pytest.raises(RuntimeError, match="another workflow"):
                    acquire_checkout_lock(lock_path)
            released.set()
            await rig.until(lambda: not rig.executor.tasks)
            if ending == "complete":
                await rig.status(run_id, "completed")
                assert rig.adapter.calls[0]["cwd"] == rig.cwd
            else:
                assert not rig.adapter.calls
            assert live.dispatches == 0
            with acquire_checkout_lock(lock_path):
                pass
        finally:
            released.set()
            await rig.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "destination", ["other-repo", "other-worktree", "unregistered", "wrong-branch"]
)
def test_ticket_plan_rejects_checkout_outside_selection_contract(tmp_path, destination):
    from wise_engine.ledger import update_run

    async def scenario():
        mode = "current" if destination == "other-repo" else "new"
        rig, run_id = await legacy_ticket_run(tmp_path, inputs={"worktree_mode": mode})
        expected = Path(rig.rt.require_run_dir(run_id)) / "worktrees/PROJ-1"
        path = tmp_path / "other" if destination.startswith("other-") else expected
        path.parent.mkdir(parents=True, exist_ok=True)
        branch = "main" if mode == "current" else "PROJ-1"
        if destination in ("other-repo", "unregistered"):
            subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    "unrelated",
                ],
                cwd=path,
                check=True,
            )
        else:
            if destination == "wrong-branch":
                branch = "other-branch"
            subprocess.run(
                ["git", "worktree", "add", "-q", "-b", branch, str(path)], cwd=rig.cwd, check=True
            )
        update_run(
            rig.rt.require_run_dir(run_id),
            {
                "outputs": {"work_branch": branch, "work_path": str(path), "ticket_ref": "PROJ-1"},
            },
        )
        try:
            await rig.executor.resume(dict(run_id=run_id))
            state = await rig.status(run_id, "failed")
            assert "selected tree" in state["error"] or "not registered" in state["error"]
            assert not rig.adapter.calls
        finally:
            await rig.close()

    asyncio.run(scenario())
