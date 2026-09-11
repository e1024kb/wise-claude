import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

from wise_engine.defs import load_def, validate_def
from wise_engine.ledger import (
    find_runs_by_session,
    init_state,
    read_events,
    read_state,
    start_run,
    update_run,
)
from wise_engine.models import catalog_for
from wise_engine.paths import PLUGIN_ROOT
from wise_engine.preflight import apply_answers, fill_answers
from wise_engine.profile import synthetic_session_id
from wise_engine.resolve import LOW_PROFILE_OPUS_MODEL, resolve_model_dict
from wise_engine.units import resolve_unit_phases

BUNDLED = ["ticket-plan", "example-workflow", "ticket-auto", "impl-plan-auto", "code-review"]


def bundled(name):
    path = PLUGIN_ROOT / "workflows" / name / "workflow.yaml"
    result = validate_def(load_def(path), str(path))
    assert "def" in result, result["issues"]
    return result["def"]


def test_explicit_low_profile_rule_and_default_opus_resolution():
    assert resolve_model_dict("opus", "high", "low")["model"] == LOW_PROFILE_OPUS_MODEL
    result = resolve_model_dict(LOW_PROFILE_OPUS_MODEL, "high", "low")
    assert result["model"] == LOW_PROFILE_OPUS_MODEL
    assert "reason" not in result
    assert resolve_model_dict("claude-opus-5", "high", "medium")["model"] == "claude-opus-5"


@pytest.mark.parametrize("name", BUNDLED)
def test_bundled_groups_use_catalog_models(name):
    definition = bundled(name)
    applied = apply_answers(definition, {})
    for group in definition.get("tuning", {}).get("groups", []):
        tuning = applied["tuning"][group["id"]]
        ids = {model["id"] for model in catalog_for(tuning.get("harness", "claude"))}
        assert tuning.get("model") in ids
        assert (
            not re.match(r"^(opus|claude-opus-5)", tuning["model"])
            or tuning["model"] == "claude-opus-5"
        )
    for tuning in applied["tuning"].values():
        result = resolve_model_dict(
            tuning.get("model", ""), tuning.get("effort", ""), applied["profile"]
        )
        assert result["model"].startswith("claude-")


def test_review_workflow_gates_missing_reports_before_curation():
    definition = bundled("code-review")
    assert definition["preflight"]["control-mode"] == "interactive"
    steps = {step["id"]: step for step in definition["steps"]}
    health = steps["review-health"]
    assert health["type"] == "bash"
    assert health["depends_on"] == ["review-correctness", "review-security", "review-tests"]
    assert health["trigger-rule"] == "all-done"
    assert steps["review-errors"]["type"] == "ask"
    assert steps["review-errors"]["when"] == "missing_reviews != 'none'"
    assert steps["curate"]["depends_on"] == ["review-health", "review-errors"]
    assert "review_failure_action" in steps["curate"]["when"]
    failure = steps["fail-incomplete-review"]
    assert failure["type"] == "bash"
    assert failure["when"] == "missing_reviews != 'none'"
    assert failure["depends_on"] == ["finalize"]
    assert failure["trigger-rule"] == "all-done"


@pytest.mark.parametrize("name", ["ticket-auto", "impl-plan-auto"])
def test_bundled_unit_phase_models_and_caps(name):
    definition = bundled(name)
    applied = apply_answers(definition, {})
    step = next(step for step in definition["steps"] if step["id"] == "process")
    phases = resolve_unit_phases(step, applied["tuning"], applied["profile"], {})
    for phase in ("plan", "implement", "review", "fix"):
        assert phases[phase]["model"] == "claude-opus-5"
    assert phases["watch"]["model"] == "claude-sonnet-5"
    assert all(cap in applied["caps"] for cap in step["caps"])


def test_synthetic_session_finds_failed_run(tmp_path):
    cwd = str(tmp_path / "workspace")
    sid = synthetic_session_id(dict(cwd=cwd, env={}))
    assert sid.startswith("local-")
    root = tmp_path / "runs"
    run_dir = root / "01RUN"
    init_state(
        run_dir=run_dir,
        run_id="01RUN",
        workflow=dict(name="ticket-plan", version=2, dir="/wf"),
        step_ids=["a"],
        cwd=cwd,
        harness_session=sid,
    )
    start_run(run_dir, {})
    update_run(run_dir, {"status": "failed"})
    rows = find_runs_by_session(root, sid, {})
    assert len(rows) == 1 and rows[0]["run_id"] == "01RUN" and rows[0]["fresh"]
    assert find_runs_by_session(root, "other-session", {}) == []


def test_rpc_executor_bash_provider_output_and_ledger_integration():
    async def run(root):
        from wise_engine.adapters.claude import start_claude
        from wise_engine.client import connect
        from wise_engine.daemon import start_daemon
        from wise_engine.executor import executor_handlers

        workspace = root / "workspace"
        workspace.mkdir()
        definitions = root / "definitions"
        definitions.mkdir()
        (definitions / "roundtrip.yaml").write_text("""version: 2
name: roundtrip
preflight: {control-mode: interactive, worktree: current}
steps:
  - id: prepare
    type: bash
    run: 'printf ready > marker'
  - id: answer
    type: agent
    depends_on: [prepare]
    prompt: Give the answer
    schema:
      type: object
      properties: {answer: {type: string}}
      required: [answer]
    outputs: [answer]
  - id: finish
    type: bash
    depends_on: [answer]
    run: 'printf "{{answer}}" > final'
""")
        binary = root / "fake-claude"
        binary.write_text(
            f"#!{sys.executable}\n"
            + """import json, sys
from pathlib import Path
assert Path("marker").read_text() == "ready"
assert json.loads(sys.stdin.readline())["request"]["subtype"] == "initialize"
assert json.loads(sys.stdin.readline())["message"]["role"] == "user"
print(json.dumps(dict(type="system", subtype="init", session_id="fake-session", tools=[])), flush=True)
print(json.dumps(dict(type="result", subtype="success", is_error=False, result="done", structured_output=dict(answer="pong🙂"), usage=dict(input_tokens=10, output_tokens=3))), flush=True)
assert sys.stdin.read() == ""
"""
        )
        binary.chmod(0o755)

        class AuthOnlyAdapter:
            id = "claude"
            bin = None

            async def probe_auth(self, auth):
                return {"ok": True}

        async def starter(harness, req, on_event):
            assert harness == "claude"
            return await start_claude(
                req, on_event, bin=str(binary), parent_env={"PATH": os.environ.get("PATH", "")}
            )

        executors = []
        instance = await start_daemon(
            data_root=str(root / "data"),
            env={},
            version="integration",
            handlers=executor_handlers(
                dict(
                    env={"PATH": os.environ.get("PATH", "")},
                    roots=dict(user_root=str(definitions), bundled_root=str(definitions)),
                    adapters={"claude": AuthOnlyAdapter()},
                    start_agent=starter,
                    channel={"inject": False},
                ),
                executors.append,
            ),
        )
        client = await connect(data_root=str(root / "data"), env={}, version="integration")
        try:
            assert client.hello["version"] == "integration"
            answers = {}
            for _ in range(8):
                pre = await client.call(
                    "preflight", dict(workflow="roundtrip", cwd=str(workspace), answers=answers)
                )
                filled = fill_answers(pre["questions"], answers)["answers"]
                if filled == answers:
                    break
                answers = filled
            created = await client.call(
                "run", dict(workflow="roundtrip", cwd=str(workspace), answers=answers)
            )
            run_id = created["run_id"]
            async with asyncio.timeout(5):
                while True:
                    state = read_state(instance.runtime.require_run_dir(run_id))
                    if state["status"] in ("completed", "failed", "cancelled"):
                        break
                    await asyncio.sleep(0.01)
            assert state["status"] == "completed", state
            assert state["outputs"]["answer"] == "pong🙂"
            assert (workspace / "final").read_text() == "pong🙂"
            assert state["usage"]["subscription"]["input"] == 10
            events = read_events(instance.runtime.require_run_dir(run_id))
            assert any(event["type"] == "run.done" for event in events)
            report = await client.call("report", {"run_id": run_id})
            assert report
        finally:
            client.close()
            await client.rpc.task
            for executor in executors:
                for task in list(executor.tasks):
                    task.cancel()
                await asyncio.gather(*executor.tasks, return_exceptions=True)
            await instance.close()

    with tempfile.TemporaryDirectory(prefix="wi-", dir="/tmp") as directory:
        asyncio.run(run(Path(directory)))
