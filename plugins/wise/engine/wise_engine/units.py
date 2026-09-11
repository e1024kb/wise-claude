from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timezone

from .constants import PHASES
from .ledger import add_usage, empty_usage, read_unit, utc_now, write_log, write_unit
from .pricing import price_usage
from .spawn import clean_env
from .phases.claim import claim_phase
from .phases.cleanup import cleanup_phase
from .phases.common import Json, fail, make_unit, parse_items, pass_, spawn_runner
from .phases.model import (
    NO_AGENT_RUNTIME,
    findings_path,
    fix_phase,
    head_sha,
    implement_phase,
    merge_pr,
    plan_phase,
    resolve_unit_phases,
    resolved_for,
    review_phase,
    watch_phase,
)
from .phases.pr import pr_phase
from .phases.push import push_phase
from .phases.request_review import request_review_phase
from .phases.worktree import worktree_phase
from .prompts.units.schemas import (
    MODEL_PHASES,
    is_model_phase,
    parse_fix,
    parse_review,
    parse_watch,
    phase_key,
)
from .yaml_compat import js_string

__all__ = [
    "make_unit",
    "parse_items",
    "NO_AGENT_RUNTIME",
    "resolve_unit_phases",
    "MODEL_PHASES",
    "phase_key",
]
DEFAULT_REVIEWERS = ["copilot-pull-request-reviewer"]
CAP_DEFAULTS = {
    "max_review_cycles": 2,
    "max_fix_attempts": 3,
    "watch_minutes": 45,
    "watch_poll_seconds": 60,
    "watch_stable_passes": 2,
}
DEFAULT_RUNNERS = {
    "claim": claim_phase,
    "worktree": worktree_phase,
    "plan": plan_phase,
    "implement": implement_phase,
    "review": review_phase,
    "fix": fix_phase,
    "push": push_phase,
    "pr": pr_phase,
    "request-review": request_review_phase,
    "watch": watch_phase,
    "cleanup": cleanup_phase,
}


def unit_row(ledger: Json) -> Json:
    return {
        key: ledger[key]
        for key in ("unit", "cleaned", "verdict", "reason", "review")
        if key in ledger
    }


def is_done(ledger: Json) -> bool:
    return ledger["cleaned"] or ledger["last_phase"] == "cleanup" and "verdict" in ledger


def config_for(step: Json, state: Json) -> Json:
    config = {
        "pipeline": step["pipeline"],
        "reviewers": step.get("reviewers", DEFAULT_REVIEWERS),
        "tickets": state["context"].get("ticket", []),
        "caps": {
            name: state["caps"][name] for name in step.get("caps", []) if name in state["caps"]
        },
        "groups": step["groups"],
        "profile": state["profile"],
        "resume": step.get("resume", "fresh"),
    }
    for key in ("permissions", "provider_permissions"):
        if key in state:
            config[key] = dict(state[key]) if key == "provider_permissions" else state[key]
    for key in ("guidance", "decisions"):
        if key in state["context"]:
            config[key] = state["context"][key]
    for key in ("mcp", "timeout", "max_turns"):
        if key in step:
            config[key] = step[key]
    return config


def cap_of(caps: Json, name: str) -> float:
    value = caps.get(name)
    return value if value is not None and value >= 0 else CAP_DEFAULTS[name]


def resolved_phases(step: Json, state: Json) -> Json:
    return {
        phase: state["resolved"][phase_key(step["id"], phase)]
        for phase in MODEL_PHASES
        if phase_key(step["id"], phase) in state["resolved"]
    }


def _apply_patch(ledger: Json, patch: Json) -> None:
    for key, value in patch.items():
        if key in ("cursors", "unit"):
            ledger[key] = {**ledger[key], **value}
        else:
            ledger[key] = value


def _should_run(phase: str, resume_phase: str | None) -> bool:
    if resume_phase is None or phase in ("claim", "worktree"):
        return True
    index = PHASES.index(resume_phase) if resume_phase in PHASES else -1
    return PHASES.index(phase) > index


async def _default_sleep(ms: float, signal: asyncio.Event | None = None) -> None:
    if ms <= 0 or signal is not None and signal.is_set():
        return
    if signal is None:
        await asyncio.sleep(ms / 1000)
    else:
        try:
            await asyncio.wait_for(signal.wait(), ms / 1000)
        except TimeoutError:
            pass


def _summarize(rows: list[Json]) -> str:
    merged = sum(row.get("verdict") == "merged" for row in rows)
    opened = sum(
        row.get("verdict") in ("all-green", "blocked", "partial", "human-intervention")
        for row in rows
    )
    failed = sum(row.get("verdict") in ("failed", "exhausted", None) for row in rows)
    skipped = sum(row.get("verdict") == "skipped" for row in rows)
    return f"units={len(rows)} merged={merged} open={opened} failed={failed} skipped={skipped}"


async def review_fix_loop(ctx: Json, runners: Json, hooks: Json) -> Json:
    maximum = max(1, cap_of(ctx["config"]["caps"], "max_review_cycles"))
    cycles = 0
    converged = False
    while True:
        cycles += 1
        hooks["emit_phase"]("review")
        result = await runners["review"]({**ctx, "review": {"shape": "panel", "cycle": cycles}})
        hooks["fold"]("review", result)
        if not result["ok"]:
            return result
        output = parse_review(result.get("output"))
        if output is None:
            return fail("review: no structured output")
        if output["verdict"] == "approve":
            converged = True
            break
        if cycles >= maximum:
            break
        hooks["emit_phase"]("fix")
        request = {"source": "review", "findings_path": findings_path(ctx)}
        if ctx["config"]["resume"] == "unit" and "review" in ctx["ledger"]["cursors"]:
            request["cursor"] = ctx["ledger"]["cursors"]["review"]
        fixed = await runners["fix"]({**ctx, "fix": request})
        hooks["fold"]("fix", fixed)
        if not fixed["ok"]:
            return fixed
        ctx["checkpoint"]({"review": {"converged": False, "cycles": cycles}})
    ctx["log"](
        f"review: converged after {cycles} cycle(s)"
        if converged
        else f"review: not converged after {cycles} cycle(s); pushing anyway"
    )
    return pass_({"review": {"converged": converged, "cycles": cycles}})


async def watch_loop(ctx: Json, runners: Json, hooks: Json) -> Json:
    if not ctx["unit"].get("pr"):
        return fail("watch: no PR recorded")
    caps = ctx["config"]["caps"]
    max_fix, minutes = cap_of(caps, "max_fix_attempts"), cap_of(caps, "watch_minutes")
    poll_ms = cap_of(caps, "watch_poll_seconds") * 1000
    stable_target = max(1, cap_of(caps, "watch_stable_passes"))
    watch = ctx["ledger"].get("watch", {"passes": 0, "fix_attempts": 0, "stable": 0})

    def save() -> None:
        ctx["checkpoint"]({"watch": dict(watch)})

    started = ctx["now"]()
    run_started = utc_now(datetime.fromtimestamp(started / 1000, timezone.utc))
    last = None
    hooks["emit_phase"]("watch")

    async def fix_and_push(source: str) -> Json:
        if watch["fix_attempts"] >= max_fix:
            return fail(
                f"max_fix_attempts ({js_string(max_fix)}) reached; {source} still needs a fix",
                "exhausted",
                {"watch": dict(watch)},
            )
        hooks["emit_phase"]("fix")
        fixed = await runners["fix"](
            {**ctx, "fix": {"source": source, "findings_path": findings_path(ctx)}}
        )
        hooks["fold"]("fix", fixed)
        if not fixed["ok"]:
            return fixed
        output = parse_fix(fixed.get("output"))
        if output is None or output["commits"] == 0:
            return fail(f"fix produced no commit for {source}", "partial", {"watch": dict(watch)})
        watch["fix_attempts"] += 1
        watch["stable"] = 0
        save()
        hooks["emit_phase"]("push")
        pushed = await runners["push"](ctx)
        hooks["fold"]("push", pushed)
        return pushed

    while True:
        if ctx.get("signal") is not None and ctx["signal"].is_set():
            return fail("watch: cancelled")
        if ctx["now"]() - started >= minutes * 60_000:
            detail = f" (ci={last['ci']}, bots={last['bot_reviews']})" if last is not None else ""
            return fail(
                f"watch_minutes ({js_string(minutes)}) cap reached{detail}",
                "all-green" if last is not None and last["ci"] == "green" else "exhausted",
                {"watch": dict(watch)},
            )
        head = await head_sha(ctx)
        watch["passes"] += 1
        result = await runners["watch"](
            {
                **ctx,
                "watch": {"pass": watch["passes"], "head_sha": head, "run_started": run_started},
            }
        )
        hooks["fold"]("watch", result)
        save()
        if not result["ok"]:
            return result
        output = parse_watch(result.get("output"))
        if output is None:
            return fail("watch: no structured output")
        last = output
        if output["merged"]:
            return pass_({"verdict": "merged", "watch": dict(watch)})
        if output["human_comment"] or output["verdict"] == "needs-human":
            return fail(
                "a human commented on the PR; standing down",
                "human-intervention",
                {"watch": dict(watch)},
            )
        if output["verdict"] == "blocked":
            return fail(
                "a bot review item needs a human (see the findings file)",
                "blocked",
                {"watch": dict(watch)},
            )
        if output["ci"] == "red" or output["bot_reviews"] == "open":
            fixed = await fix_and_push("ci" if output["ci"] == "red" else "bot-reviews")
            if not fixed["ok"]:
                return fixed
            await ctx["sleep"](poll_ms)
            continue
        covered = output["bot_reviews"] == "resolved"
        if output["bot_reviews"] == "stuck":
            if watch.get("fallback_sha") == head:
                covered = True
            else:
                hooks["emit_phase"]("review")
                substitute = await runners["review"](
                    {**ctx, "review": {"shape": "universal", "cycle": watch["passes"]}}
                )
                hooks["fold"]("review", substitute)
                if not substitute["ok"]:
                    return fail(f"substitute review failed: {substitute['reason']}", "all-green")
                review = parse_review(substitute.get("output"))
                if review and review["verdict"] == "changes-requested":
                    fixed = await fix_and_push("review")
                    if not fixed["ok"]:
                        return fixed
                    await ctx["sleep"](poll_ms)
                    continue
                watch["fallback_sha"] = head
                covered = True
                save()
                ctx["log"](f"watch: substitute review covered {head[:12]}")
        if output["ci"] == "green" and covered:
            watch["stable"] += 1
            save()
            if watch["stable"] >= stable_target:
                merged = await merge_pr(ctx)
                if not merged["ok"]:
                    return fail(merged["reason"], "all-green", {"watch": dict(watch)})
                ctx["log"](f"watch: merged #{js_string(ctx['unit']['pr']['number'])}")
                return pass_({"verdict": "merged", "watch": dict(watch)})
        elif watch["stable"] != 0:
            watch["stable"] = 0
            save()
        await ctx["sleep"](poll_ms)


async def run_units_step(input: Json) -> Json:
    run_dir, cwd, step, state = (input[key] for key in ("run_dir", "cwd", "step", "state"))
    config = config_for(step, state)
    resolved = resolved_phases(step, state)
    command_runner = input.get("exec", spawn_runner)
    lock = asyncio.Lock()

    async def execute(cmd: str, args: list[str], opts: Json) -> Json:
        if cmd != "git":
            return await command_runner(cmd, args, opts)
        async with lock:
            return await command_runner(cmd, args, opts)

    env = clean_env(
        parent=input.get("parent_env"),
        extra={"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"},
    )
    runners = {**DEFAULT_RUNNERS, **input.get("runners", {})}
    agent = (
        {**input["agent"], "step_id": step["id"], "step_run_id": input["step_run_id"]}
        if input.get("agent")
        else None
    )
    sleep = input.get("sleep", _default_sleep)
    now = input.get("now", lambda: time.time() * 1000)
    lines: list[str] = []
    pricing_warned: set[str] = set()

    def flush_log() -> str:
        return write_log(run_dir, step["id"], input["step_run_id"], "\n".join(lines) + "\n")

    async def process_unit(item: str) -> Json:
        unit = make_unit(config["pipeline"], item, cwd, run_dir, config.get("base", ""))

        def log(line: str) -> None:
            lines.append(f"[{unit['ref']}] {line}")

        existing = read_unit(run_dir, unit["branch"])
        if existing and is_done(existing):
            log(f"already {existing.get('verdict', 'done')}; skipped (resume)")
            input["emit"](
                {
                    "type": "unit.done",
                    "unit": unit["ref"],
                    "verdict": existing.get("verdict", "failed"),
                    "message": existing.get("reason", "already done"),
                }
            )
            return unit_row(existing)
        resume_phase = existing["last_phase"] if existing is not None else None
        ledger = (
            existing
            if existing is not None
            else {
                "unit": unit,
                "last_phase": "claim",
                "cleaned": False,
                "cursors": {},
                "usage": empty_usage(),
                "caps": config["caps"],
            }
        )
        if existing:
            log(f"resume from {resume_phase}")

        def persist() -> None:
            write_unit(run_dir, unit["branch"], ledger)

        def checkpoint(patch: Json) -> None:
            _apply_patch(ledger, patch)
            persist()

        def make_ctx() -> Json:
            ctx = {
                "unit": ledger["unit"],
                "ledger": ledger,
                "cwd": cwd,
                "run_dir": run_dir,
                "env": env,
                "exec": execute,
                "config": config,
                "log": log,
                "checkpoint": checkpoint,
                "sleep": lambda ms: sleep(ms, input.get("signal")),
                "now": now,
                "resolved": resolved,
            }
            if agent:
                ctx["agent"] = agent
            if input.get("signal") is not None:
                ctx["signal"] = input["signal"]
            return ctx

        def emit_phase(phase: str) -> None:
            event = {"type": "unit.phase", "unit": unit["ref"], "phase": phase}
            if is_model_phase(phase) and agent:
                resolution = resolved_for(make_ctx(), phase)
                event.update(harness=resolution["harness"], model=resolution["model"])
                if resolution["effort"]:
                    event["effort"] = resolution["effort"]
            input["emit"](event)

        def fold(phase: str, result: Json) -> None:
            if result.get("patch"):
                _apply_patch(ledger, result["patch"])
            if result.get("usage"):
                harness = result.get("resolved", {}).get("harness", "claude")
                model = result.get("resolved", {}).get("model", "inherit")
                priced = price_usage(result["usage"], harness, model)
                unknown = priced.get("unknownModel")
                if unknown is not None and unknown not in pricing_warned:
                    pricing_warned.add(unknown)
                    log(
                        f"{phase}: no price for {harness} model {unknown}; api-key cost not counted"
                    )
                add_usage(ledger["usage"], priced["usage"])
                add_usage(
                    ledger.setdefault("usage_by_phase", {}).setdefault(
                        phase, empty_usage(priced["usage"]["pool"])
                    ),
                    priced["usage"],
                )
                if input.get("on_usage"):
                    input["on_usage"](phase, harness, priced["usage"], model)
            persist()

        hooks = {"emit_phase": emit_phase, "fold": fold}
        stopped = False
        for phase in PHASES:
            if input.get("signal") is not None and input["signal"].is_set():
                break
            if (
                stopped
                and phase != "cleanup"
                or not _should_run(phase, resume_phase)
                or phase == "fix"
            ):
                continue
            ctx = make_ctx()
            try:
                if phase == "review":
                    result = await review_fix_loop(ctx, runners, hooks)
                elif phase == "watch":
                    result = await watch_loop(ctx, runners, hooks)
                else:
                    emit_phase(phase)
                    result = await runners[phase](ctx)
                    fold(phase, result)
            except Exception as error:
                result = fail(f"{phase}: {error}")
            if phase in ("review", "watch") and result.get("patch"):
                _apply_patch(ledger, result["patch"])
            if not (
                resume_phase in PHASES
                and phase in ("claim", "worktree")
                and PHASES.index(phase) < PHASES.index(resume_phase)
            ):
                ledger["last_phase"] = phase
            if not result["ok"]:
                ledger["verdict"] = result.get("verdict", "failed")
                ledger["reason"] = result["reason"]
                stopped = True
                log(f"{phase}: {ledger['verdict']} ({result['reason']})")
            persist()
        if "verdict" not in ledger:
            ledger.update(verdict="failed", reason="no verdict recorded")
            persist()
        event = {"type": "unit.done", "unit": unit["ref"], "verdict": ledger["verdict"]}
        if "reason" in ledger:
            event["message"] = ledger["reason"]
        input["emit"](event)
        flush_log()
        return unit_row(ledger)

    items = []
    seen: set[str] = set()
    for item in input["items"]:
        branch = make_unit(config["pipeline"], item, cwd, run_dir)["branch"]
        if branch in seen:
            lines.append(f"[{item}] duplicate of an earlier item (branch {branch}); skipped")
        else:
            seen.add(branch)
            items.append(item)
    queue = deque(items)
    rows: list[Json] = []

    async def worker() -> None:
        while queue:
            if input.get("signal") is not None and input["signal"].is_set():
                return
            rows.append(await process_unit(queue.popleft()))

    workers = max(1, min(step.get("parallel", 1), len(queue)))
    await asyncio.gather(*(worker() for _ in range(workers)))
    order = {
        make_unit(config["pipeline"], item, cwd, run_dir)["branch"]: i
        for i, item in enumerate(items)
    }
    rows.sort(key=lambda row: order.get(row["unit"]["branch"], 0))
    return {"verdict": _summarize(rows), "outputs": {"units": rows}, "log": flush_log()}
