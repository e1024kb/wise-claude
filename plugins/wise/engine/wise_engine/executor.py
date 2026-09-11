from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .adapter_types import AgentHandle
from .channel import (
    STALE_AFTER_SECS_DEFAULT,
    answer_from_decisions,
    clip_report_data,
    clip_report_text,
    create_child_tracker,
    progress_line,
    real_timers,
    resolve_context_key,
    stale_nudge_text,
    start_stale_watch,
    write_checkpoint,
)
from .constants import HARNESSES, REPORT_KINDS
from .context_files import persist_context
from .daemon import (
    DaemonRuntime,
    as_record,
    require_string,
    ledger_handlers,
    record_child,
    clear_child,
)
from .defs import default_roots, locate_def, load_and_validate, probe_requires
from .ledger import (
    append_event,
    init_state,
    read_state,
    write_state,
    start_run,
    start_step,
    update_run,
    update_step,
    utc_now,
    new_ulid,
    list_units,
    fold_usage_views,
    usage_tokens,
    usage_total,
)
from .paths import cwd_slug, ENGINE_ROOT
from .permissions import effective_mode, provider_permission
from .preflight import (
    apply_answers,
    build_questionary,
    choice_input_preset,
    complete_answers,
    input_choice_values,
    invalid_choice_input_ids,
    invalid_provider_permission_answers,
    resolve_from_context,
)
from .pricing import price_usage
from .protocol import RPC_INVALID_PARAMS, WAIT_DEFAULT_MS, WAIT_MAX_MS, WAIT_PROGRESS_MS
from .render import render_step, _json
from .resolve import resolve_model_dict
from .rpc import RpcError, domain_error
from .scheduler import next_wave, JS_WHITESPACE
from .steps.agent import headline, start_agent_step
from .steps.bash import start_bash_step
from .steps.gate import APPROVAL_OPTIONS, build_gate, decide_gate, is_gate_step

Json = dict[str, Any]
DEFAULT_CAPS: Json = {
    "global": 4,
    "harness": {"claude": 2, "codex": 1, "cursor": 1, "gemini": 1, "grok": 1},
}


def default_config_path(env: Any = None) -> str:
    env = os.environ if env is None else env
    return str(
        Path(env.get("XDG_CONFIG_HOME") or Path(env.get("HOME", str(Path.home()))) / ".config")
        / "wise/engine.json"
    )


def load_caps(config_path: str, overrides: Json | None = None) -> Json:
    caps: Json = {"global": DEFAULT_CAPS["global"], "harness": dict(DEFAULT_CAPS["harness"])}
    try:
        conc = json.loads(Path(config_path).read_text()).get("concurrency", {})
        for key in ("global", *HARNESSES):
            value = conc.get(key)
            if (
                type(value) in (int, float)
                and math.isfinite(value)
                and value >= 1
                and value == int(value)
            ):
                if key == "global":
                    caps[key] = int(value)
                else:
                    caps["harness"][key] = int(value)
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    overrides = overrides or {}
    if "global" in overrides:
        caps["global"] = overrides["global"]
    caps["harness"].update(overrides.get("harness", {}))
    return caps


def default_backoff_ms(attempt: int) -> int:
    return min(2 ** min(max(0, attempt - 1), 5), 30) * 60_000


def detect_project(cwd: str) -> Json:
    kind = "other"
    for candidate, manifests in (
        ("node", ("package.json",)),
        ("python", ("pyproject.toml", "requirements.txt")),
        ("go", ("go.mod",)),
        ("rust", ("Cargo.toml",)),
    ):
        if any((Path(cwd) / name).exists() for name in manifests):
            kind = candidate
            break
    return dict(path=cwd, name=Path(cwd).name, kind=kind)


def optional_record(rec: Json, key: str, method: str) -> Json:
    value = rec.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RpcError(RPC_INVALID_PARAMS, f"{method}: {key} must be an object")
    return value


def control_mode_of(definition: Json, answers: Json) -> str:
    value = answers.get("control-mode")
    return (
        value
        if value in ("synchronous", "interactive")
        else definition.get("preflight", {}).get("control-mode", "interactive")
    )


def validated(located: Json) -> Json:
    try:
        result = load_and_validate(located)
    except Exception as exc:
        raise domain_error(
            "WORKFLOW_INVALID",
            f"{located['path']}: {exc}",
            {
                "workflow": located["name"],
                "issues": [dict(level="error", path="", message=str(exc))],
            },
        ) from exc
    if "def" not in result:
        raise domain_error(
            "WORKFLOW_INVALID",
            f"{located['path']}: definition has errors",
            {"workflow": located["name"], "issues": result["issues"]},
        )
    return result["def"]


@dataclass
class PendingAsk:
    ask_id: str
    step: str
    question: str
    options: list[str] | None = None
    allow_text: bool | None = None
    gate_id: str | None = None
    value: str | None = None
    dropped: bool = False
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def drop(self) -> None:
        self.dropped = True
        self.changed.set()


@dataclass
class LiveRun:
    run_id: str
    run_dir: str
    definition: Json
    workflow_dir: str
    control_mode: str
    stopped: bool = False
    children: dict[str, Any] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)
    fallback_auth: dict[str, str] = field(default_factory=dict)
    trackers: dict[str, Any] = field(default_factory=dict)
    stale_watches: dict[str, Any] = field(default_factory=dict)
    stale_killed: set[str] = field(default_factory=set)
    asks: dict[str, PendingAsk] = field(default_factory=dict)
    pricing_warned: set[str] = field(default_factory=set)

    def emit(self, event: Json) -> None:
        append_event(self.run_dir, {"run_id": self.run_id, **event})


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class Executor:
    def __init__(self, rt: DaemonRuntime, opts: Json | None = None):
        self.rt, self.opts = rt, opts or {}
        self.env = self.opts.get("env", os.environ)
        self.roots = self.opts.get("roots", default_roots({"env": self.env}))
        self.caps = load_caps(
            self.opts.get("config_path", default_config_path(self.env)),
            self.opts.get("concurrency"),
        )
        self.backoff_ms = self.opts.get("backoff_ms", default_backoff_ms)
        self.project_of = self.opts.get("detect_project", detect_project)
        self.requires_of = self.opts.get("probe_requires", probe_requires)
        self.ledger = ledger_handlers(rt)
        self.channel_opts = self.opts.get("channel", {})
        self.timers = self.channel_opts.get("timers") or real_timers()
        self.channel = (
            None
            if self.channel_opts.get("inject") is False
            else {
                "engine_root": self.channel_opts.get("engine_root", str(ENGINE_ROOT)),
                "socket_path": self.channel_opts.get("socket_path", rt.paths.socket_path),
                "data_root": self.channel_opts.get("data_root", rt.paths.data_root),
            }
        )
        self.lives: dict[str, LiveRun] = {}
        self.in_flight = dict.fromkeys(HARNESSES, 0)
        self.in_flight_global = 0
        self.parked: dict[str, Json] = {}
        self.slot_waiters: list[tuple[str, asyncio.Future[Any]]] = []
        self.tasks: set[asyncio.Task[Any]] = set()
        self.handlers = {
            name: getattr(self, name)
            for name in (
                "preflight",
                "run",
                "answer",
                "status",
                "cancel",
                "resume",
                "report",
                "child_report",
                "child_ask",
                "child_context",
                "child_checkpoint",
            )
        }
        self.handlers.update(wait=self.ledger["wait"], nudge=self.nudge_handler)

    def task(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)

        def done(task: asyncio.Task[Any]) -> None:
            self.tasks.discard(task)
            if not task.cancelled() and task.exception():
                self.rt.log(f"executor task failed: {task.exception()}")

        task.add_done_callback(done)
        return task

    def get_adapter(self, harness: str) -> Any:
        from .adapters import adapter_for, has_adapter

        if "adapters" in self.opts:
            return self.opts["adapters"].get(harness)
        return adapter_for(harness) if has_adapter(harness) else None

    async def starter(self, harness: str, req: Json, on_event: Any) -> AgentHandle:
        if "start_agent" in self.opts:
            return await _await(self.opts["start_agent"](harness, req, on_event))
        adapter = self.get_adapter(harness)
        if adapter is None:
            raise domain_error(
                "HARNESS_UNAVAILABLE", f'no adapter for harness "{harness}"', {"harness": harness}
            )
        if "adapters" not in self.opts:
            from .adapters import default_starter

            return await default_starter(harness, req, on_event)
        return AgentHandle(done=self.task(adapter.run(req, on_event)))

    def locate(self, ref: str) -> Json:
        path = Path(ref)
        if path.is_absolute() and path.suffix in (".yaml", ".yml"):
            if not path.is_file():
                raise domain_error(
                    "WORKFLOW_NOT_FOUND", f"no workflow file at {ref}", {"workflow": ref}
                )
            return dict(
                name=path.parent.name if path.name == "workflow.yaml" else path.stem,
                path=ref,
                dir=str(path.parent),
                source="user",
            )
        located = locate_def(ref, self.roots)
        if not located:
            raise domain_error(
                "WORKFLOW_NOT_FOUND", f"no workflow named {_json(ref)}", {"workflow": ref}
            )
        return located

    def ensure_live(self, directory: str, state: Json) -> LiveRun:
        if state["run_id"] in self.lives:
            return self.lives[state["run_id"]]
        wf = state["workflow"]
        located = locate_def(wf["name"], self.roots)
        if not located and wf.get("dir"):
            for name in ("workflow.yaml", f"{wf['name']}.yaml"):
                candidate = Path(wf["dir"]) / name
                if candidate.exists():
                    located = self.locate(str(candidate))
                    break
        if not located:
            raise domain_error(
                "WORKFLOW_NOT_FOUND",
                f"workflow {_json(wf['name'])} is gone",
                {"workflow": wf["name"]},
            )
        definition = validated(located)
        live = LiveRun(
            state["run_id"],
            directory,
            definition,
            located["dir"],
            control_mode_of(definition, state["answers"]),
        )
        self.lives[live.run_id] = live
        return live

    def is_parked(self, harness: str) -> bool:
        park = self.parked.get(harness)
        return park is not None and self.timers.now() < park["until"]

    def harness_free(self, harness: str) -> bool:
        return (
            not self.is_parked(harness)
            and self.in_flight[harness] < self.caps["harness"].get(harness, 1)
            and self.in_flight_global < self.caps["global"]
        )

    def take_slot(self, harness: str) -> Any:
        self.in_flight[harness] += 1
        self.in_flight_global += 1
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            self.in_flight[harness] -= 1
            self.in_flight_global -= 1
            self.wake_slots()
            self.defer_all()

        return release

    def wake_slots(self) -> None:
        for harness, future in list(self.slot_waiters):
            if future.done():
                self.slot_waiters.remove((harness, future))
            elif self.harness_free(harness):
                self.slot_waiters.remove((harness, future))
                future.set_result(self.take_slot(harness))

    async def acquire_slot(self, harness: str, signal: asyncio.Event | None = None) -> Any:
        if signal and signal.is_set():
            raise RuntimeError("cancelled while waiting for a slot")
        if self.harness_free(harness):
            return self.take_slot(harness)
        future = asyncio.get_running_loop().create_future()
        waiter = (harness, future)
        self.slot_waiters.append(waiter)
        aborted = asyncio.create_task(signal.wait()) if signal else None
        try:
            if aborted:
                await asyncio.wait((future, aborted), return_when=asyncio.FIRST_COMPLETED)
                if not future.done():
                    raise RuntimeError("cancelled while waiting for a slot")
            return await future
        except BaseException:
            # A wakeup owns a slot even if its waiter is cancelled before resuming.
            if future.done() and not future.cancelled():
                future.result()()
            raise
        finally:
            if aborted:
                aborted.cancel()
            if waiter in self.slot_waiters:
                self.slot_waiters.remove(waiter)
            if not future.done():
                future.cancel()

    def defer(self, live: LiveRun) -> None:
        asyncio.get_running_loop().call_soon(self.schedule, live)

    def defer_all(self) -> None:
        for live in list(self.lives.values()):
            self.defer(live)

    def schedule(self, live: LiveRun) -> None:
        try:
            self._schedule(live)
        except Exception as exc:
            self.rt.log(f"run {live.run_id}: scheduler error {exc}")
            if not live.stopped:
                self.fail_run(live, f"scheduler error: {exc}")

    def _schedule(self, live: LiveRun) -> None:
        while not live.stopped:
            state = read_state(live.run_dir)
            if state["status"] != "running":
                return
            wave = next_wave(live.definition, state)
            for warning in wave["warnings"]:
                live.emit(dict(type="warn", message=warning))
            if wave["skipped"]:
                for step in wave["skipped"]:
                    verdict = headline(f"skipped: {step['reason']}")
                    update_step(
                        live.run_dir,
                        step["id"],
                        dict(status="skipped", verdict=verdict, completed_at=utc_now()),
                    )
                    live.emit(dict(type="step.done", step=step["id"], verdict=verdict))
                continue
            if wave["done"]:
                self.finish(live, wave["failed"])
                return
            gate = None
            for step in wave["ready"]:
                if is_gate_step(step):
                    gate = gate or step
                    continue
                resolved = None
                if step["type"] == "agent":
                    resolved = self.pick_harness(live, state, step)
                    if resolved is None:
                        continue
                # Persist running before yielding to async process creation, so re-passes cannot duplicate a child.
                cursor_harness = state["steps"][step["id"]].get("resolved", {}).get("harness")
                step_run_id = start_step(live.run_dir, step["id"])
                release = self.take_slot(resolved["harness"]) if resolved else None
                self.task(
                    self.dispatch(live, state, step, step_run_id, resolved, cursor_harness, release)
                )
            if gate and not self.open_gate(live, gate):
                continue
            return

    def drop_live(self, live: LiveRun) -> None:
        live.stopped = True
        for watch in live.stale_watches.values():
            watch.stop()
        for ask in live.asks.values():
            ask.drop()
        for collection in (
            live.stale_watches,
            live.asks,
            live.trackers,
            live.children,
            live.tokens,
        ):
            collection.clear()
        self.lives.pop(live.run_id, None)

    def finish(self, live: LiveRun, failed: bool) -> None:
        state = read_state(live.run_dir)
        if failed and not state.get("error"):
            bad = next(
                ((key, step) for key, step in state["steps"].items() if step["status"] == "failed"),
                None,
            )
            state["error"] = (
                f"{bad[0]}: {bad[1].get('error', bad[1].get('verdict', 'failed'))}"
                if bad
                else "unreachable steps"
            )
        state.update(
            status="failed" if failed else "completed",
            completed_at=utc_now(),
            last_activity_at=utc_now(),
        )
        state.pop("gate", None)
        write_state(live.run_dir, state)
        live.emit(
            dict(
                type="run.failed" if failed else "run.done",
                verdict=headline(state.get("error", "failed")) if failed else "completed",
            )
        )
        self.rt.log(
            f"run {live.run_id}: {state['status']}" + (f" ({state.get('error')})" if failed else "")
        )
        self.drop_live(live)

    def complete_step(
        self, live: LiveRun, step_id: str, verdict: str, outputs: Json, extra: Json | None = None
    ) -> None:
        extra = extra or {}
        state = read_state(live.run_dir)
        step = state["steps"].get(step_id)
        if step is None:
            return
        step.update(status="completed", completed_at=utc_now(), verdict=verdict, outputs=outputs)
        if "cursor" in extra:
            step["cursor"] = extra["cursor"]
        step.pop("error", None)
        state["outputs"].update(outputs)
        state["last_activity_at"] = utc_now()
        write_state(live.run_dir, state)
        event: Json = dict(type="step.done", step=step_id, verdict=verdict)
        compact = {
            key: headline(value) if isinstance(value, str) else value
            for key, value in outputs.items()
            if isinstance(value, (str, int, float, bool))
        }
        if compact:
            event["outputs"] = compact
        event.update({key: extra[key] for key in ("usage", "harness") if key in extra})
        live.emit(event)

    def fail_step(
        self,
        live: LiveRun,
        step_id: str,
        error: str,
        verdict: str | None = None,
        extra: Json | None = None,
    ) -> None:
        extra = extra or {}
        verdict = verdict if verdict is not None else headline(f"failed: {error}")
        patch = dict(status="failed", error=error, verdict=verdict, completed_at=utc_now())
        if "cursor" in extra:
            patch["cursor"] = extra["cursor"]
        update_step(live.run_dir, step_id, patch)
        live.emit(
            dict(
                type="step.done",
                step=step_id,
                verdict=verdict,
                **({"harness": extra["harness"]} if "harness" in extra else {}),
            )
        )

    def kill_children(self, live: LiveRun) -> None:
        for child in list(live.children.values()):
            if child.kill:
                child.kill("SIGTERM")

    def fail_run(self, live: LiveRun, error: str) -> None:
        self.kill_children(live)
        state = read_state(live.run_dir)
        now = utc_now()
        for step in state["steps"].values():
            if step["status"] == "running":
                step.update(
                    status="failed",
                    error=error,
                    verdict=headline(f"failed: {error}"),
                    completed_at=now,
                )
        state.update(status="failed", error=error, completed_at=now, last_activity_at=now)
        state.pop("gate", None)
        write_state(live.run_dir, state)
        live.emit(dict(type="run.failed", verdict=headline(error)))
        self.rt.log(f"run {live.run_id}: failed ({error})")
        self.drop_live(live)

    def planned_resolution(self, state: Json, step: Json) -> Json:
        return state["resolved"].get(
            step["id"],
            dict(
                harness=step.get("harness", "claude"),
                model=step.get("model", "inherit"),
                effort=step.get("effort", ""),
            ),
        )

    def pick_harness(self, live: LiveRun, state: Json, step: Json) -> Json | None:
        primary = self.planned_resolution(state, step)
        if not self.is_parked(primary["harness"]):
            return primary if self.harness_free(primary["harness"]) else None
        group: Json = next(
            (
                group
                for group in live.definition.get("tuning", {}).get("groups", [])
                if group["id"] == step.get("group")
            ),
            {},
        )
        for harness in step.get("fallback", group.get("fallback", [])) or []:
            if harness == primary["harness"]:
                continue
            key = f"{harness}/{step.get('auth', 'subscription')}"
            if key not in live.fallback_auth:
                if not self.get_adapter(harness):
                    live.fallback_auth[key] = "failed"
                    live.emit(
                        dict(
                            type="warn",
                            step=step["id"],
                            message=f"{step['id']}: no adapter for fallback {harness}; waiting for {primary['harness']}",
                        )
                    )
                else:
                    live.fallback_auth[key] = "pending"
                    self.task(self.probe_fallback(live, step, primary["harness"], harness, key))
            if live.fallback_auth[key] == "ok" and self.harness_free(harness):
                return dict(
                    harness=harness,
                    model="inherit",
                    effort=primary["effort"],
                    reason=f"fallback from {primary['harness']} (rate limited): model inherit",
                )
        return None

    async def probe_fallback(
        self, live: LiveRun, step: Json, primary: str, harness: str, key: str
    ) -> None:
        from .auth import probe_one

        probe = await probe_one(harness, step.get("auth", "subscription"), self.get_adapter)
        if live.stopped:
            return
        live.fallback_auth[key] = "ok" if probe["ok"] else "failed"
        if not probe["ok"]:
            live.emit(
                dict(
                    type="warn",
                    step=step["id"],
                    harness=harness,
                    message=f"{step['id']}: fallback {harness} not logged in; run `{probe['login_cmd']}`; waiting for {primary}",
                )
            )
        self.defer(live)

    async def dispatch(
        self,
        live: LiveRun,
        state: Json,
        definition: Json,
        step_run_id: str,
        resolved: Json | None,
        cursor_harness: str | None,
        release: Any,
    ) -> None:
        step_id = definition["id"]
        handle: Any = None
        try:
            if live.stopped:
                return
            if resolved:
                update_step(live.run_dir, step_id, {"resolved": resolved})
            fresh = read_state(live.run_dir)
            step = render_step(definition, fresh, live.workflow_dir, live.run_dir)
            event = dict(type="step.started", step=step_id)
            if definition.get("description"):
                event["message"] = headline(definition["description"])
            if resolved:
                harness = resolved["harness"]
                planned = self.planned_resolution(state, definition)["harness"]
                if harness != planned:
                    live.emit(
                        dict(
                            type="warn",
                            step=step_id,
                            harness=harness,
                            message=f"{step_id} falls back to {harness}",
                        )
                    )
                step["mode"] = effective_mode(step.get("mode"), provider_permission(fresh, harness))
                event.update(harness=harness, model=resolved["model"])
                if resolved.get("effort"):
                    event["effort"] = resolved["effort"]
            live.emit(event)
            if step["type"] == "bash":
                handle = await start_bash_step(
                    step, dict(cwd=state["cwd"], parent_env=self.env, **self.timeout_opts())
                )
                live.children[step_id] = handle
                if live.stopped and handle.kill:
                    handle.kill("SIGTERM")
                result = await handle.result
                if self.current(live, step_id, step_run_id):
                    if result["ok"]:
                        self.complete_step(live, step_id, result["verdict"], result["outputs"])
                    else:
                        self.fail_step(
                            live, step_id, result.get("error", "failed"), result["verdict"]
                        )
            elif step["type"] == "units":
                await self.dispatch_units(live, fresh, step, step_run_id)
            else:
                assert resolved is not None
                tracker = create_child_tracker(
                    dict(
                        step=step_id,
                        now=self.timers.now,
                        **(
                            {"throttle_ms": self.channel_opts["progress_throttle_ms"]}
                            if "progress_throttle_ms" in self.channel_opts
                            else {}
                        ),
                    )
                )
                live.trackers[step_id] = tracker
                token = secrets.token_hex(16)
                live.tokens[step_id] = token

                def on_event(event: Json) -> None:
                    progress = tracker.ingest(event)
                    if progress and not live.stopped:
                        live.emit(
                            dict(
                                type="step.progress", step=step_id, message=progress_line(progress)
                            )
                        )

                params = dict(
                    run_dir=live.run_dir,
                    step_run_id=step_run_id,
                    step=step,
                    resolved=resolved,
                    cwd=state["cwd"],
                    step_token=token,
                    starter=self.starter,
                    on_event=on_event,
                    **self.timeout_opts(),
                )
                if self.channel is not None:
                    params["channel"] = self.channel
                if cursor_harness == resolved["harness"] and "cursor" in fresh["steps"][step_id]:
                    params["cursor"] = fresh["steps"][step_id]["cursor"]
                started = await start_agent_step(params)
                handle = started.handle
                live.children[step_id] = handle
                if handle.pid and handle.pid > 0:
                    record_child(live.run_dir, dict(pgid=handle.pid, pid=handle.pid))
                if live.stopped:
                    if handle.kill:
                        handle.kill("SIGTERM")
                else:
                    self.watch_child(live, step, tracker, handle)
                try:
                    outcome = await started.outcome
                except Exception as exc:
                    outcome = dict(
                        exit="error",
                        ok=False,
                        outputs={},
                        verdict=headline(f"error: {exc}"),
                        error=str(exc),
                        usage=dict(
                            input=0,
                            output=0,
                            cache_read=0,
                            cache_write=0,
                            pool=step.get("auth", "subscription"),
                        ),
                        warnings=[],
                    )
                if self.current(live, step_id, step_run_id):
                    self.close_child_asks(live, step_id)
                    self.settle_agent(live, step, resolved, outcome)
        except asyncio.CancelledError:
            if handle and handle.kill:
                handle.kill("SIGTERM")
            raise
        except Exception as exc:
            if self.current(live, step_id, step_run_id):
                self.fail_step(live, step_id, str(exc))
        finally:
            live.children.pop(step_id, None)
            live.tokens.pop(step_id, None)
            live.trackers.pop(step_id, None)
            watch = live.stale_watches.pop(step_id, None)
            if watch:
                watch.stop()
            if handle and handle.pid:
                clear_child(live.run_dir, handle.pid)
            if release:
                release()
            self.defer_all()

    def timeout_opts(self) -> Json:
        return (
            {"default_timeout_ms": self.opts["default_timeout_ms"]}
            if "default_timeout_ms" in self.opts
            else {}
        )

    def current(self, live: LiveRun, step: str, step_run_id: str) -> bool:
        return (
            not live.stopped
            and read_state(live.run_dir)["steps"].get(step, {}).get("step_run_id") == step_run_id
        )

    async def dispatch_units(
        self, live: LiveRun, state: Json, step: Json, step_run_id: str
    ) -> None:
        from .units import run_units_step, parse_items

        step_id = step["id"]
        if "{{" in step["items"]:
            raise ValueError(f"items template unresolved: {headline(step['items'], 80)}")
        signal = asyncio.Event()
        live.children[step_id] = AgentHandle(
            done=asyncio.get_running_loop().create_future(), kill=lambda _: signal.set()
        )
        token = secrets.token_hex(16)
        live.tokens[step_id] = token
        unregisters = []

        def track(key: str, handle: AgentHandle) -> Any:
            child_id = f"{step_id}/{key}"
            live.children[child_id] = handle
            if handle.pid and handle.pid > 0:
                record_child(live.run_dir, dict(pgid=handle.pid, pid=handle.pid))
            if live.stopped and handle.kill:
                handle.kill("SIGTERM")

            def unregister() -> None:
                if live.children.get(child_id) is handle:
                    live.children.pop(child_id, None)
                if handle.pid:
                    clear_child(live.run_dir, handle.pid)

            unregisters.append(unregister)
            return unregister

        agent = dict(
            starter=self.starter,
            step_token=token,
            acquire=self.acquire_slot,
            track=track,
            **self.timeout_opts(),
        )
        if self.channel is not None:
            agent["channel"] = self.channel
        params = dict(
            run_dir=live.run_dir,
            cwd=state["cwd"],
            step_run_id=step_run_id,
            step=step,
            items=parse_items(step["items"]),
            state=state,
            parent_env=self.env,
            agent=agent,
            signal=signal,
            on_usage=lambda phase, harness, usage, model: None
            if live.stopped
            else self.fold_usage(live, step_id, harness, usage, model),
            emit=lambda event: None if live.stopped else live.emit(event),
        )
        if "units_exec" in self.opts:
            params["exec"] = self.opts["units_exec"]
        try:
            result = await run_units_step(params)
            if self.current(live, step_id, step_run_id):
                self.complete_step(live, step_id, result["verdict"], result["outputs"])
        finally:
            for unregister in unregisters:
                unregister()

    def watch_child(self, live: LiveRun, step: Json, tracker: Any, child: AgentHandle) -> None:
        step_id = step["id"]

        def nudge(idle: float) -> bool:
            if not child.nudge:
                return False
            try:
                child.nudge(stale_nudge_text(idle))
            except Exception:
                return False
            live.emit(
                dict(
                    type="warn",
                    step=step_id,
                    message=f"{step_id}: idle for {max(1, math.floor(idle / 60_000 + 0.5))} min, nudged",
                )
            )
            return True

        def kill() -> None:
            if not child.kill:
                live.emit(dict(type="warn", step=step_id, message=f"{step_id}: stale, cannot kill"))
                return
            live.stale_killed.add(step_id)
            live.emit(
                dict(
                    type="warn",
                    step=step_id,
                    message=f"{step_id}: stale, killed"
                    + ("" if child.nudge else "; resume from cursor"),
                )
            )
            child.kill("SIGTERM")

        live.stale_watches[step_id] = start_stale_watch(
            dict(
                stale_ms=step.get(
                    "stale_after",
                    self.channel_opts.get("stale_after_secs", STALE_AFTER_SECS_DEFAULT),
                )
                * 1000,
                timers=self.timers,
                last_activity_ms=tracker.last_activity_ms,
                paused=lambda: any(
                    ask.step == step_id and ask.value is None and not ask.dropped
                    for ask in live.asks.values()
                ),
                nudge=nudge,
                kill=kill,
            )
        )

    def settle_agent(self, live: LiveRun, step: Json, resolved: Json, outcome: Json) -> None:
        harness, step_id = resolved["harness"], step["id"]
        usage = self.fold_usage(live, step_id, harness, outcome["usage"], resolved["model"], False)
        for warning in outcome["warnings"]:
            live.emit(dict(type="warn", step=step_id, message=headline(warning)))
        patch = {
            "harness": harness,
            **({"cursor": outcome["cursor"]} if "cursor" in outcome else {}),
        }
        if step_id in live.stale_killed:
            live.stale_killed.discard(step_id)
            self.fail_step(live, step_id, "stale", "failed: stale (no activity, killed)", patch)
        elif outcome["exit"] == "ok":
            self.complete_step(
                live, step_id, outcome["verdict"], outcome["outputs"], {**patch, "usage": usage}
            )
        elif outcome["exit"] == "rate_limited":
            self.rate_limited(live, step, harness, outcome.get("error", "rate limited"))
        elif outcome["exit"] == "auth":
            self.auth_failed(live, step, resolved, outcome.get("error", "not logged in"))
        else:
            self.fail_step(
                live,
                step_id,
                str(outcome.get("error") or outcome["exit"]),
                outcome["verdict"],
                patch,
            )
        if not live.stopped:
            self.check_ceiling(live, read_state(live.run_dir), step_id)

    def fold_usage(
        self, live: LiveRun, step_id: str, harness: str, raw: Json, model: str, ceiling: bool = True
    ) -> Json:
        priced = price_usage(raw, harness, model)
        unknown = priced.get("unknownModel")
        if unknown is not None and unknown not in live.pricing_warned:
            live.pricing_warned.add(unknown)
            live.emit(
                dict(
                    type="warn",
                    step=step_id,
                    harness=harness,
                    message=f"{step_id}: no price for {harness} model {unknown}; api-key cost not counted",
                )
            )
        usage = priced["usage"]
        state = read_state(live.run_dir)
        fold_usage_views(state, dict(step=step_id, harness=harness, usage=usage))
        state["last_activity_at"] = utc_now()
        write_state(live.run_dir, state)
        live.emit(dict(type="usage", step=step_id, harness=harness, usage=usage))
        if ceiling:
            self.check_ceiling(live, state, step_id)
        return usage

    def check_ceiling(self, live: LiveRun, state: Json, step_id: str) -> None:
        limit = state["caps"].get("tokens")
        if limit is None or state["status"] != "running" or state.get("gate"):
            return
        used = usage_tokens(usage_total(state["usage"]))
        if used < limit:
            return
        message = f"Run used {used} tokens, ceiling {limit}. Continue?"
        if live.control_mode == "synchronous":
            live.emit(
                dict(
                    type="warn",
                    step=step_id,
                    message=headline(f"ceiling: {message} no (synchronous)"),
                )
            )
            self.fail_run(live, f"ceiling: used {used} tokens, ceiling {limit}")
            return
        gate = dict(
            gate_id=new_ulid(),
            step=step_id,
            kind="approval",
            message=message,
            options=[dict(option) for option in APPROVAL_OPTIONS],
            ceiling=dict(used=used, limit=limit),
        )
        update_run(live.run_dir, dict(status="gated", gate=gate))
        live.emit(dict(type="gate.opened", step=step_id, verdict=headline(message)))

    def rate_limited(self, live: LiveRun, step: Json, harness: str, error: str) -> None:
        previous = self.parked.get(harness)
        attempts = (previous["attempts"] if previous else 0) + 1
        delay = self.backoff_ms(attempts)
        if previous:
            self.timers.clear_timeout(previous["timer"])
        park = dict(until=self.timers.now() + delay, attempts=attempts)

        def unpark() -> None:
            if self.parked.get(harness) is park:
                del self.parked[harness]
            self.wake_slots()
            self.defer_all()

        park["timer"] = self.timers.set_timeout(unpark, delay)
        self.parked[harness] = park
        update_step(live.run_dir, step["id"], dict(status="pending", error=headline(error)))
        live.emit(
            dict(
                type="warn",
                step=step["id"],
                harness=harness,
                message=headline(
                    f"rate limited on {harness}; backoff {math.floor(delay / 1000 + 0.5)}s: {error}"
                ),
            )
        )

    def auth_failed(self, live: LiveRun, step: Json, resolved: Json, error: str) -> None:
        from .auth import LOGIN_CMDS

        harness = resolved["harness"]
        self.fail_step(live, step["id"], f"AUTH_REQUIRED: {error}")
        self.kill_children(live)
        state = read_state(live.run_dir)
        for key, current in state["steps"].items():
            if key != step["id"] and current["status"] == "running":
                current["status"] = "pending"
                current.pop("started_at", None)
                current.pop("step_run_id", None)
        state.update(
            status="failed",
            error=f"AUTH_REQUIRED: {harness} ({resolved['model']}) not logged in; run `{LOGIN_CMDS[harness]}`",
            completed_at=utc_now(),
            last_activity_at=utc_now(),
        )
        state.pop("gate", None)
        write_state(live.run_dir, state)
        live.emit(dict(type="run.failed", harness=harness, verdict=headline(state["error"])))
        self.drop_live(live)

    def open_gate(self, live: LiveRun, definition: Json) -> bool:
        start_step(live.run_dir, definition["id"])
        step = render_step(definition, read_state(live.run_dir), live.workflow_dir, live.run_dir)
        if step["type"] == "approval" and live.control_mode == "synchronous":
            verdict = "auto-approved (control-mode synchronous)"
            update_step(
                live.run_dir,
                step["id"],
                dict(status="completed", verdict=verdict, completed_at=utc_now()),
            )
            live.emit(
                dict(
                    type="warn",
                    step=step["id"],
                    message=f"approval {step['id']} auto-approved: control-mode synchronous",
                )
            )
            live.emit(dict(type="step.done", step=step["id"], verdict=verdict))
            return False
        gate = build_gate(step, new_ulid())
        update_run(live.run_dir, dict(status="gated", gate=gate))
        live.emit(dict(type="gate.opened", step=step["id"], verdict=headline(gate["message"])))
        return True

    def open_child_asks(self, live: LiveRun) -> None:
        if live.stopped:
            return
        state = read_state(live.run_dir)
        if state["status"] != "running" or state.get("gate"):
            return
        ask = next(
            (
                ask
                for ask in live.asks.values()
                if ask.gate_id is None and ask.value is None and not ask.dropped
            ),
            None,
        )
        if ask is None:
            return
        gate: Json = dict(
            gate_id=new_ulid(), step=ask.step, kind="ask", message=ask.question.strip(JS_WHITESPACE)
        )
        if ask.options:
            gate["options"] = [dict(value=option, label=option) for option in ask.options]
        if ask.allow_text is not None:
            gate["allow_text"] = ask.allow_text
        ask.gate_id = gate["gate_id"]
        update_run(live.run_dir, dict(status="gated", gate=gate))
        live.emit(dict(type="gate.opened", step=ask.step, verdict=headline(gate["message"])))

    def close_child_asks(self, live: LiveRun, step_id: str) -> None:
        closed = False
        for key, ask in list(live.asks.items()):
            if ask.step != step_id:
                continue
            del live.asks[key]
            ask.drop()
            state = read_state(live.run_dir)
            if ask.gate_id is not None and state.get("gate", {}).get("gate_id") == ask.gate_id:
                state.update(status="running", last_activity_at=utc_now())
                state.pop("gate", None)
                write_state(live.run_dir, state)
                closed = True
        if closed:
            live.emit(
                dict(
                    type="warn",
                    step=step_id,
                    message=f"{step_id}: child exited with an open question; gate closed",
                )
            )
        self.open_child_asks(live)

    def answer_child_ask(self, live: LiveRun, ask: PendingAsk, value: Any) -> Json:
        synthetic: Json = dict(id=ask.step, type="ask", message=ask.question)
        if ask.options is not None:
            synthetic["options"] = ask.options
        if ask.allow_text is not None:
            synthetic["allow_text"] = ask.allow_text
        decision = decide_gate(synthetic, value)
        text = decision.get("output", {}).get("value", "")
        self.reopen(live)
        ask.value = text
        ask.changed.set()
        live.emit(dict(type="gate.answered", step=ask.step, verdict=headline(f"answered: {text}")))
        self.nudge(live.run_id, ask.step, f"Answer to your question: {text}")
        if ask.step in live.trackers:
            live.trackers[ask.step].touch()
        self.open_child_asks(live)
        self.defer(live)
        return {"accepted": True}

    def reopen(self, live: LiveRun) -> None:
        state = read_state(live.run_dir)
        state.update(status="running", last_activity_at=utc_now())
        state.pop("gate", None)
        write_state(live.run_dir, state)

    def answer_ceiling(self, live: LiveRun, gate: Json, value: Any) -> Json:
        text = (", ".join(value) if isinstance(value, list) else value).strip(JS_WHITESPACE)
        if text not in ("approve", "reject"):
            raise RpcError(
                RPC_INVALID_PARAMS,
                f'answer: ceiling gate takes "approve" or "reject", got {_json(text)}',
            )
        state = read_state(live.run_dir)
        if text == "reject":
            live.emit(dict(type="gate.answered", step=gate["step"], verdict="rejected: ceiling"))
            self.fail_run(
                live,
                f"ceiling: used {gate['ceiling']['used']} tokens, ceiling {gate['ceiling']['limit']}, rejected",
            )
        else:
            increase = (
                live.definition.get("profiles", {})
                .get(state["profile"], {})
                .get("caps", {})
                .get("tokens", state["caps"].get("tokens", 0))
            )
            limit = state["caps"].get("tokens", gate["ceiling"]["limit"]) + increase
            state["caps"]["tokens"] = limit
            state.update(status="running", last_activity_at=utc_now())
            state.pop("gate", None)
            write_state(live.run_dir, state)
            live.emit(
                dict(
                    type="gate.answered",
                    step=gate["step"],
                    verdict=headline(f"approved: ceiling raised to {limit} tokens"),
                )
            )
            self.open_child_asks(live)
            self.defer(live)
        return {"accepted": True}

    def assert_permissions(self, answers: Json) -> None:
        invalid = invalid_provider_permission_answers(answers)
        if invalid:
            raise domain_error(
                "MISSING_ANSWERS",
                f"invalid provider permission answer(s): {', '.join(invalid)}",
                dict(missing=invalid, invalid=invalid),
            )

    async def preflight(self, params: Any, ctx: Any = None) -> Json:
        from .auth import installed_harnesses
        from .preflight import build_questionary_with_auth

        rec = as_record(params, "preflight")
        workflow = require_string(rec, "workflow", "preflight")
        require_string(rec, "cwd", "preflight")
        answers = dict(optional_record(rec, "answers", "preflight"))
        self.assert_permissions(answers)
        located = self.locate(workflow)
        definition = validated(located)
        harnesses = installed_harnesses(definition, self.get_adapter, self.env)
        questionary = await build_questionary_with_auth(
            definition, {"harnesses": harnesses}, answers, self.get_adapter
        )
        return dict(
            workflow=located["name"],
            version=definition["version"],
            questions=questionary["questions"],
            defaults=questionary["defaults"],
            requires_missing=self.requires_of(definition)["missing"],
        )

    async def run(self, params: Any, ctx: Any = None) -> Json:
        from .auth import installed_harnesses, probe_harnesses, collect_needs
        from .units import resolve_unit_phases, phase_key

        rec = as_record(params, "run")
        workflow = require_string(rec, "workflow", "run")
        cwd = require_string(rec, "cwd", "run")
        given = dict(optional_record(rec, "answers", "run"))
        self.assert_permissions(given)
        context = optional_record(rec, "context", "run")
        explicit = optional_record(rec, "inputs", "run")
        located = self.locate(workflow)
        definition = validated(located)
        harnesses = installed_harnesses(definition, self.get_adapter, self.env)
        seeded = {**given, **{f"input.{key}": value for key, value in explicit.items()}}
        completed = complete_answers(
            definition, {"harnesses": harnesses, "context": context}, seeded
        )
        answers = completed["answers"]
        unanswered = [
            question
            for question in completed["questions"]
            if not question.get("locked")
            and not question["id"].startswith("input.")
            and question["id"] not in given
        ]
        applied = apply_answers(definition, answers)
        resolved = {}
        for step in definition["steps"]:
            if step["id"] not in applied["enabled_steps"]:
                continue
            if step["type"] == "agent":
                group = applied["tuning"].get(step.get("group"), {})
                harness = step.get("harness", group.get("harness", "claude"))
                result = resolve_model_dict(
                    step.get("model", group.get("model", "")),
                    step.get("effort", group.get("effort", "")),
                    applied["profile"],
                    dict(harness=harness, env=self.env),
                )
                resolved[step["id"]] = {
                    key: result[key]
                    for key in ("harness", "model", "effort", "reason")
                    if key in result
                }
            elif step["type"] == "units":
                for phase, result in resolve_unit_phases(
                    step, applied["tuning"], applied["profile"], self.env
                ).items():
                    resolved[phase_key(step["id"], phase)] = result
        required = self.requires_of(definition)
        if not required["ok"]:
            raise domain_error(
                "REQUIRES_MISSING",
                f"workflow requires {', '.join(required['missing'])}; install them and retry",
                {"missing": required["missing"]},
            )
        inputs = {**applied["inputs"], **explicit}
        for item in definition.get("inputs", []):
            name = item["name"]
            answer_id = f"input.{name}"
            if input_choice_values(item) is not None:
                if name in explicit:
                    inputs[name] = explicit[name]
                elif answer_id in given:
                    inputs[name] = given[answer_id]
                else:
                    preset = choice_input_preset(item, context)
                    if preset is None:
                        inputs.pop(name, None)
                    else:
                        inputs[name] = preset
                continue
            explicitly_unset = (
                item.get("optional") and inputs.get(name) == "" and answer_id in seeded
            )
            needs_context = not inputs.get(name) and not explicitly_unset
            if item.get("from-context") and needs_context:
                value = resolve_from_context(item["from-context"], context)
                if value is not None:
                    inputs[name] = value
        invalid_inputs = invalid_choice_input_ids(definition, inputs)
        missing = list(
            dict.fromkeys(
                [question["id"] for question in unanswered]
                + [
                    key
                    for key in completed["missing"]
                    if not (
                        key.startswith("input.")
                        and inputs.get(key[6:])
                        and key not in invalid_inputs
                    )
                ]
                + invalid_inputs
            )
        )
        if missing:
            questions = {question["id"]: question for question in completed["questions"]}
            if invalid_inputs:
                retry_answers = {
                    key: value for key, value in answers.items() if key not in invalid_inputs
                }
                for question in build_questionary(
                    definition, {"harnesses": harnesses}, retry_answers
                )["questions"]:
                    if question["id"] in invalid_inputs:
                        questions[question["id"]] = question
            raise domain_error(
                "MISSING_ANSWERS",
                f"pre-flight questions left unanswered (ask them, never default them): {', '.join(missing)}",
                dict(
                    missing=missing,
                    questions=[questions[key] for key in missing if key in questions],
                ),
            )
        await probe_harnesses(
            collect_needs(definition, applied["enabled_steps"], resolved), self.get_adapter
        )
        run_id = new_ulid()
        run_dir = str(Path(self.rt.paths.runs_root) / cwd_slug(cwd) / run_id)
        state = init_state(
            run_dir=run_dir,
            run_id=run_id,
            workflow=dict(name=located["name"], version=definition["version"], dir=located["dir"]),
            step_ids=[step["id"] for step in definition["steps"]],
            cwd=cwd,
            profile=applied["profile"],
        )
        for step in definition["steps"]:
            if step["id"] not in applied["enabled_steps"]:
                state["steps"][step["id"]].update(
                    status="skipped",
                    verdict="skipped: deselected in pre-flight",
                    completed_at=utc_now(),
                )
        write_state(run_dir, state)
        legacy = answers.get("permissions")
        if legacy not in ("allowlist", "full"):
            legacy = definition.get("preflight", {}).get("permissions")
        run_context = dict(
            project=self.project_of(cwd),
            inputs=inputs,
            answers=answers,
            context=persist_context(run_dir, context),
            profile=applied["profile"],
            provider_permissions=applied["provider_permissions"],
            resolved=resolved,
            caps=applied["caps"],
        )
        if legacy is not None:
            run_context["permissions"] = legacy
        start_run(run_dir, run_context)
        mode = control_mode_of(definition, answers)
        live = LiveRun(run_id, run_dir, definition, located["dir"], mode)
        self.lives[run_id] = live
        live.emit(
            dict(
                type="run.started",
                verdict=headline(
                    f"{located['name']} control={mode} steps={len(applied['enabled_steps'])}/{len(definition['steps'])}"
                ),
            )
        )
        self.rt.log(f"run {run_id}: started {located['name']} in {cwd}")
        self.defer(live)
        return dict(run_id=run_id, status="running")

    def answer(self, params: Any, ctx: Any = None) -> Json:
        rec = as_record(params, "answer")
        run_id = require_string(rec, "run_id", "answer")
        gate_id = require_string(rec, "gate_id", "answer")
        value = rec.get("value")
        if not isinstance(value, str) and not (
            isinstance(value, list) and all(isinstance(item, str) for item in value)
        ):
            raise RpcError(RPC_INVALID_PARAMS, "answer: value must be a string or string[]")
        directory = self.rt.require_run_dir(run_id)
        state = read_state(directory)
        gate = state.get("gate")
        if state["status"] != "gated" or not gate or gate["gate_id"] != gate_id:
            raise domain_error(
                "GATE_STALE",
                f"answer: run {run_id} has no open gate {gate_id}",
                dict(
                    run_id=run_id,
                    gate_id=gate_id,
                    **({"current_gate_id": gate["gate_id"]} if gate else {}),
                ),
            )
        live = self.ensure_live(directory, state)
        if gate.get("ceiling"):
            return self.answer_ceiling(live, gate, value)
        ask = next((ask for ask in live.asks.values() if ask.gate_id == gate_id), None)
        if ask:
            return self.answer_child_ask(live, ask, value)
        definition = next(
            (step for step in live.definition["steps"] if step["id"] == gate["step"]), None
        )
        if definition is None or not is_gate_step(definition):
            raise domain_error(
                "GATE_STALE",
                f"answer: gate step {gate['step']} is not a gate",
                dict(run_id=run_id, gate_id=gate_id),
            )
        decision = decide_gate(definition, value)
        step = state["steps"].get(definition["id"])
        if step is not None:
            step.update(
                status=decision["status"], verdict=decision["verdict"], completed_at=utc_now()
            )
            if decision["status"] == "failed":
                step["error"] = decision["verdict"]
            if "output" in decision:
                output = decision["output"]
                step["outputs"] = {output["name"]: output["value"]}
                state["outputs"].update(step["outputs"])
        state.update(status="running", last_activity_at=utc_now())
        state.pop("gate", None)
        write_state(directory, state)
        answered: Json = dict(
            type="gate.answered", step=definition["id"], verdict=decision["verdict"]
        )
        if "output" in decision:
            answered["outputs"] = {
                decision["output"]["name"]: headline(decision["output"]["value"])
            }
        live.emit(answered)
        live.emit(dict(type="step.done", step=definition["id"], verdict=decision["verdict"]))
        self.open_child_asks(live)
        self.defer(live)
        return {"accepted": True}

    async def resume(self, params: Any, ctx: Any = None) -> Json:
        result = await _await(self.ledger["resume"](params, ctx))
        if result["status"] == "running":
            directory = self.rt.require_run_dir(result["run_id"])
            self.defer(self.ensure_live(directory, read_state(directory)))
        return result

    def cancel(self, params: Any, ctx: Any = None) -> Any:
        rec = as_record(params, "cancel")
        live = self.lives.get(require_string(rec, "run_id", "cancel"))
        if live:
            self.kill_children(live)
            self.drop_live(live)
        return self.ledger["cancel"](params, ctx)

    def report(self, params: Any, ctx: Any = None) -> Json:
        from .units import unit_row

        rec = as_record(params, "report")
        directory = self.rt.require_run_dir(require_string(rec, "run_id", "report"))
        state = read_state(directory)
        return dict(
            units=[unit_row(unit) for unit in list_units(directory)],
            usage={**state["usage"], "by_step": state["usage"].get("by_step", {})},
            usage_total=usage_total(state["usage"]),
            resolved=state["resolved"],
            verdicts={
                key: step["verdict"] for key, step in state["steps"].items() if "verdict" in step
            },
        )

    async def status(self, params: Any, ctx: Any = None) -> Any:
        result = await _await(self.ledger["status"](params, ctx))

        def decorate(summary: Json) -> Json:
            live = self.lives.get(summary["run_id"])
            return (
                {**summary, "children": [tracker.snapshot() for tracker in live.trackers.values()]}
                if live and live.trackers
                else summary
            )

        if isinstance(result, list):
            return [decorate(row) for row in result]
        directory = self.rt.find_run_dir(result["run_id"])
        return (
            {**decorate(result), "usage_total": usage_total(read_state(directory)["usage"])}
            if directory
            else decorate(result)
        )

    def nudge(self, run_id: str, step_id: str, text: str) -> bool:
        live = self.lives.get(run_id)
        child = live.children.get(step_id) if live else None
        if child is None or not child.nudge:
            return False
        try:
            child.nudge(text)
            return True
        except Exception:
            return False

    def nudge_handler(self, params: Any, ctx: Any = None) -> Json:
        rec = as_record(params, "nudge")
        run_id = require_string(rec, "run_id", "nudge")
        step = require_string(rec, "step", "nudge")
        message = require_string(rec, "message", "nudge")
        self.rt.require_run_dir(run_id)
        return {"delivered": self.nudge(run_id, step, message)}

    def require_token(self, rec: Json, method: str) -> tuple[LiveRun, str]:
        token = require_string(rec, "token", method)
        for live in self.lives.values():
            for step, expected in live.tokens.items():
                if secrets.compare_digest(token, expected):
                    if step in live.trackers:
                        live.trackers[step].touch()
                    return live, step
        raise domain_error("TOKEN_INVALID", f"{method}: token does not match a running step")

    def child_report(self, params: Any, ctx: Any = None) -> Json:
        rec = as_record(params, "child_report")
        live, step = self.require_token(rec, "child_report")
        kind = rec.get("kind")
        if not isinstance(kind, str) or kind not in REPORT_KINDS:
            raise RpcError(
                RPC_INVALID_PARAMS, f"child_report: kind must be one of {' | '.join(REPORT_KINDS)}"
            )
        text = require_string(rec, "text", "child_report")
        if step in live.trackers:
            live.trackers[step].report()
        event: Json = dict(
            run_id=live.run_id,
            type="step.progress",
            step=step,
            kind=kind,
            message=clip_report_text(text),
        )
        data = clip_report_data(rec.get("data"))
        if data is not None:
            event["data"] = data
        full = append_event(live.run_dir, event)
        return dict(accepted=True, seq=full["seq"])

    async def child_ask(self, params: Any, ctx: Any = None) -> Json:
        rec = as_record(params, "child_ask")
        live, step = self.require_token(rec, "child_ask")
        question = require_string(rec, "question", "child_ask")
        options = rec.get("options")
        if options is not None and not (
            isinstance(options, list) and all(isinstance(option, str) for option in options)
        ):
            raise RpcError(RPC_INVALID_PARAMS, "child_ask: options must be a string array")
        timeout = rec.get("timeout_ms", WAIT_DEFAULT_MS)
        if timeout is None:
            timeout = WAIT_DEFAULT_MS
        if type(timeout) not in (int, float) or not math.isfinite(timeout):
            raise RpcError(RPC_INVALID_PARAMS, "child_ask: timeout_ms must be a number")
        timeout = min(max(0, timeout), WAIT_MAX_MS)
        ask_id = rec.get("ask_id")
        if isinstance(ask_id, str):
            ask = live.asks.get(ask_id)
            if ask is None or ask.step != step:
                raise domain_error(
                    "GATE_STALE", f"child_ask: question {ask_id} is gone", {"ask_id": ask_id}
                )
        elif live.control_mode == "synchronous":
            state = read_state(live.run_dir)
            value = answer_from_decisions(question, options, state["context"].get("decisions"))
            identifier = new_ulid()
            if value is None:
                live.emit(
                    dict(
                        type="warn",
                        step=step,
                        message=headline(
                            f'{step} asked "{question}": no decision in a synchronous run'
                        ),
                    )
                )
                return dict(ask_id=identifier, status="needs-human")
            live.emit(
                dict(
                    type="step.progress",
                    step=step,
                    kind="decision",
                    message=headline(f'asked "{question}" -> {value} (synchronous)'),
                )
            )
            return dict(ask_id=identifier, status="answered", value=value)
        else:
            ask = PendingAsk(
                new_ulid(),
                step,
                question,
                options=options,
                allow_text=rec.get("allow_text")
                if isinstance(rec.get("allow_text"), bool)
                else None,
            )
            live.asks[ask.ask_id] = ask
            self.open_child_asks(live)
        if ask.value is None and not ask.dropped and timeout > 0:
            started = asyncio.get_running_loop().time()
            changed = asyncio.create_task(ask.changed.wait())
            disconnected = asyncio.create_task(ctx.signal.wait()) if ctx else None

            async def progress() -> None:
                while True:
                    await asyncio.sleep(WAIT_PROGRESS_MS / 1000)
                    if ctx:
                        ctx.notify(
                            "progress",
                            dict(
                                run_id=live.run_id,
                                waiting_ms=(asyncio.get_running_loop().time() - started) * 1000,
                            ),
                        )

            ticker = asyncio.create_task(progress())
            try:
                await asyncio.wait(
                    [changed, *([disconnected] if disconnected else [])],
                    timeout=timeout / 1000,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                tasks = [changed, ticker, *([disconnected] if disconnected else [])]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        if ask.value is not None:
            live.asks.pop(ask.ask_id, None)
            return dict(ask_id=ask.ask_id, status="answered", value=ask.value)
        if ask.dropped:
            raise domain_error(
                "GATE_STALE",
                f"child_ask: question {ask.ask_id} was dropped",
                {"ask_id": ask.ask_id},
            )
        return dict(ask_id=ask.ask_id, status="pending")

    def child_context(self, params: Any, ctx: Any = None) -> Json:
        rec = as_record(params, "child_context")
        live, _ = self.require_token(rec, "child_context")
        key = require_string(rec, "key", "child_context")
        return {"value": resolve_context_key(read_state(live.run_dir), key)}

    def child_checkpoint(self, params: Any, ctx: Any = None) -> Json:
        rec = as_record(params, "child_checkpoint")
        live, step = self.require_token(rec, "child_checkpoint")
        if "data" not in rec:
            raise RpcError(RPC_INVALID_PARAMS, "child_checkpoint: data is required")
        return {"path": write_checkpoint(live.run_dir, step, rec["data"])}

    def pick_up(self) -> list[str]:
        picked = []
        for directory in self.rt.list_run_dirs():
            try:
                state = read_state(directory)
            except (OSError, ValueError):
                continue
            if state["status"] != "running" or state["run_id"] in self.lives:
                continue
            try:
                live = self.ensure_live(directory, state)
                picked.append(live.run_id)
                self.defer(live)
            except Exception as exc:
                self.rt.log(f"pickup: run {state['run_id']} skipped: {exc}")
        return picked

    def stop(self) -> None:
        for live in list(self.lives.values()):
            self.kill_children(live)
            self.drop_live(live)
        for park in self.parked.values():
            self.timers.clear_timeout(park["timer"])
        self.parked.clear()
        for _, future in self.slot_waiters:
            if not future.done():
                future.cancel()
        self.slot_waiters.clear()

    def is_busy(self) -> bool:
        return (
            self.in_flight_global > 0
            or bool(self.parked)
            or bool(self.tasks)
            or any(live.children for live in self.lives.values())
        )

    def step_token(self, run_id: str, step_id: str) -> str | None:
        live = self.lives.get(run_id)
        return live.tokens.get(step_id) if live else None

    def step_by_token(self, token: str) -> Json | None:
        for live in self.lives.values():
            for step, expected in live.tokens.items():
                if token == expected:
                    return dict(run_id=live.run_id, step=step)
        return None

    def live_runs(self) -> list[str]:
        return list(self.lives)


def create_executor(rt: DaemonRuntime, opts: Json | None = None) -> Executor:
    return Executor(rt, opts)


def executor_handlers(opts: Json | None = None, on_create: Any = None) -> Any:
    def factory(rt: DaemonRuntime) -> Any:
        executor = create_executor(rt, opts)
        if on_create:
            on_create(executor)
        return executor.handlers

    return factory
