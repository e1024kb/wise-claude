from __future__ import annotations

import asyncio
import fcntl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from .constants import ATTACHED_PIPELINES, PHASES, PIPELINE_PHASES
from .fanout import (
    FAILURE_VERDICTS,
    SUCCESS_VERDICTS,
    concurrency_of,
    dependency_edges,
    is_terminal_state,
    normalize_items,
    parse_repo_paths,
    repo_slug,
    resolve_repo,
)
from .ledger import add_usage, empty_usage, read_unit, utc_now, write_log, write_unit
from .pricing import price_usage
from .spawn import clean_env
from .phases.claim import claim_phase
from .phases.cleanup import cleanup_phase
from .phases.common import (
    Json,
    err_text,
    fail,
    make_unit,
    ok,
    parse_items,
    pass_,
    spawn_runner,
    unit_key,
    worktree_slug,
)
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
from .phases.pr import pr_phase, view_pr
from .phases.push import push_phase, rebase_onto_base, sequence_collisions
from .phases.remote import GITHUB_DEFAULT, detect_remote, no_pr_reason
from .phases.request_review import request_review_phase
from .phases.verify import commit_time_ms, human_commenters, verify_reviews
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
# Phases skipped, per detected remote kind, for the branch-owning pipelines
# (`ticket` / `plan`). `none`: nothing to push to, so no push either. `other`:
# the branch is pushed to origin, only the GitHub steps are skipped.
NO_GITHUB_SKIPS = {
    "none": ("push", "pr", "request-review", "watch"),
    "other": ("pr", "request-review", "watch"),
}
# Finite ceilings for every overrideable cap, checked before the float conversion.
CAP_MAX = {
    "max_review_cycles": 100,
    "max_fix_attempts": 1000,
    "watch_minutes": 1440,
    "watch_poll_seconds": 3600,
    "watch_stable_passes": 100,
}
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


def _cap_overrides(step: Json, inputs: Json) -> Json:
    """A workflow input named after a cap overrides it when it holds a positive integer in bounds."""
    out: Json = {}
    for name in step.get("caps", []):
        raw = str(inputs.get(name, "") or "").strip()
        if raw.isdigit() and len(raw) <= 6 and 1 <= int(raw) <= CAP_MAX.get(name, 1000):
            out[name] = float(raw)
    return out


def config_for(step: Json, state: Json) -> Json:
    inputs = state.get("inputs", {})
    config = {
        "pipeline": step["pipeline"],
        "worktree_mode": "current"
        if step["pipeline"] in ATTACHED_PIPELINES
        else inputs.get("worktree_mode", "new"),
        "base": str(inputs.get("base_branch", "") or "").strip(),
        # The `substitute_review` input (pr-watch) declines the stuck-bot
        # review at pre-flight; every other workflow leaves it on.
        "substitute_review": str(inputs.get("substitute_review", "") or "").strip().lower() != "no",
        "reviewers": step.get("reviewers", DEFAULT_REVIEWERS),
        "tickets": state["context"].get("ticket", []),
        "caps": {
            **{name: state["caps"][name] for name in step.get("caps", []) if name in state["caps"]},
            **_cap_overrides(step, inputs),
        },
        "groups": step["groups"],
        "profile": state["profile"],
        "resume": step.get("resume", "fresh"),
    }
    if step["pipeline"] == "ticket-plan":
        # Epic children never ask mid-run: `ask` settles on `auto`.
        mode = str(inputs.get("branch_mode", "") or "").strip()
        config["branch_mode"] = "current" if mode == "current" else "auto"
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
    # A dependency-blocked child has no PR; it is counted apart from the
    # `blocked` verdict of a PR whose bot item needs a human.
    held = sum("blocked_by" in row for row in rows)
    opened = sum(
        row.get("verdict") in ("all-green", "blocked", "partial", "human-intervention")
        and "blocked_by" not in row
        for row in rows
    )
    failed = sum(row.get("verdict") in ("failed", "exhausted", None) for row in rows)
    skipped = sum(row.get("verdict") == "skipped" for row in rows)
    no_pr = sum(row.get("verdict") == "no-pr" for row in rows)
    planned = sum(row.get("verdict") == "plan-written" for row in rows)
    summary = f"units={len(rows)} merged={merged} open={opened} failed={failed} skipped={skipped}"
    for name, count in (("no-pr", no_pr), ("plan-written", planned), ("blocked", held)):
        if count:
            summary += f" {name}={count}"
    return summary


async def pr_state(ctx: Json) -> str | None:
    """The recorded PR's state as GitHub reports it (OPEN, MERGED, CLOSED)."""
    pr = ctx["unit"].get("pr")
    if not pr:
        return None
    viewed = await view_pr(ctx, js_string(pr["number"]))
    return viewed["state"] if viewed else None


async def first_push(ctx: Json, runners: Json, hooks: Json) -> Json:
    """The unit's first push. The branch is rebased onto the fresh base (F7:
    an earlier child may have merged while this one worked), then checked
    for numbered-sequence collisions with the base (F3: two children in one
    repo each adding migration 0042). A collision goes to one fix pass; one
    that survives it fails the unit before anything is pushed."""
    hooks["emit_phase"]("push")
    rebased = await rebase_onto_base(ctx)
    if not rebased["ok"]:
        return rebased
    collisions = await sequence_collisions(ctx)
    if collisions:
        path = Path(findings_path(ctx))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(f"{n}. {line}" for n, line in enumerate(collisions, 1)) + "\n")
        ctx["log"](f"push: {len(collisions)} numbered-sequence collision(s) with the base")
        hooks["emit_phase"]("fix")
        fixed = await runners["fix"](
            {**ctx, "fix": {"source": "sequence", "findings_path": str(path)}}
        )
        hooks["fold"]("fix", fixed)
        if not fixed["ok"]:
            return fixed
        remaining = await sequence_collisions(ctx)
        if remaining:
            return fail("sequence-collision: " + "; ".join(remaining))
    pushed = await runners["push"](ctx)
    hooks["fold"]("push", pushed)
    return pushed


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

    if "started" not in watch:
        watch["started"] = ctx["now"]()
        save()
    started = watch["started"]
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
        if pushed["ok"]:
            # The pushed head's age starts now: the verification request
            # waits for an automatic review before asking for one.
            watch["head_since"] = {"sha": await head_sha(ctx), "at": ctx["now"]()}
            save()
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
        if watch.get("head_since", {}).get("sha") != head:
            # A head this run did not push (the initial one, or someone
            # else's push): its commit time bounds when notices about it
            # can have appeared.
            watch["head_since"] = {"sha": head, "at": await commit_time_ms(ctx) or ctx["now"]()}
            save()
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
            # F2: the verdict comes from GitHub, never from the watch child.
            state = await pr_state(ctx)
            if state == "MERGED":
                return pass_({"verdict": "merged", "watch": dict(watch)})
            ctx["log"](
                f"watch: the child reported merged, gh reports {state or 'unknown'}; ignored"
            )
        if output["human_comment"] or output["verdict"] == "needs-human":
            # F4: only a User-type, non-bot commenter other than this run's
            # own login stands the run down. When GitHub cannot be read, the
            # child's claim stands.
            humans = await human_commenters(ctx, started)
            if humans is None or humans:
                who = f" ({', '.join(humans)})" if humans else ""
                return fail(
                    f"a human commented on the PR{who}; standing down",
                    "human-intervention",
                    {"watch": dict(watch)},
                )
            ctx["log"](
                "watch: the child reported a human comment, GitHub shows only bots; continuing"
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
        # The fix batch is settled on this head: reconcile the review
        # providers' state for it and request the one verification review
        # the head still needs. A requested or running review holds the
        # pass: neither covered nor stuck until it answers.
        verification = await verify_reviews(ctx, watch, head, output)
        save()
        if verification["hold"]:
            if watch["stable"] != 0:
                watch["stable"] = 0
                save()
            await ctx["sleep"](poll_ms)
            continue
        covered = output["bot_reviews"] == "resolved"
        if output["bot_reviews"] == "stuck":
            if watch.get("fallback_sha") == head:
                covered = True
            elif not ctx["config"].get("substitute_review", True):
                return fail(
                    "review-consent-declined: a review bot is stuck and the substitute "
                    "review was declined at pre-flight",
                    "all-green",
                    {"watch": dict(watch)},
                )
            else:
                hooks["emit_phase"]("review")
                substitute = await runners["review"](
                    {**ctx, "review": {"shape": "universal", "cycle": watch["passes"]}}
                )
                hooks["fold"]("review", substitute)
                if not substitute["ok"]:
                    return fail(f"substitute review failed: {substitute['reason']}", "all-green")
                review = parse_review(substitute.get("output"))
                if review is None:
                    return fail("substitute review: unusable structured output", "all-green")
                if review["verdict"] == "changes-requested":
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
                merged = await merge_pr(ctx, head)
                if not merged["ok"]:
                    return fail(merged["reason"], "all-green", {"watch": dict(watch)})
                state = await pr_state(ctx)
                if state != "MERGED":
                    # A merge queue or auto-merge accepted the request; the
                    # PR is not merged until GitHub says so.
                    return fail(
                        f"merge requested, gh reports {state or 'unknown'}",
                        "all-green",
                        {"watch": dict(watch)},
                    )
                ctx["log"](f"watch: merged #{js_string(ctx['unit']['pr']['number'])}")
                return pass_({"verdict": "merged", "watch": dict(watch)})
        elif watch["stable"] != 0:
            watch["stable"] = 0
            save()
        await ctx["sleep"](poll_ms)


async def run_units_step(input: Json) -> Json:
    if config_for(input["step"], input["state"])["worktree_mode"] != "current":
        return await _run_units_step(input)
    execute = input.get("exec", spawn_runner)
    result = await execute(
        "git",
        ["rev-parse", "--path-format=absolute", "--git-path", "wise-current-tree.lock"],
        {"cwd": input["cwd"], "env": clean_env(parent=input.get("parent_env"))},
    )
    if not ok(result) or not result["stdout"].strip():
        raise RuntimeError(f"current-tree lock: cannot locate Git directory: {err_text(result)}")
    path = Path(result["stdout"].strip()).resolve()
    owned = input.get("checkout_lock")
    if owned is not None and not owned.closed and Path(owned.name).resolve() == path:
        return await _run_units_step(input)
    with acquire_checkout_lock(path):
        return await _run_units_step(input)


def acquire_checkout_lock(path: Path) -> TextIO:
    handle = path.open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException as error:
        handle.close()
        if isinstance(error, BlockingIOError):
            raise RuntimeError(
                "current-tree lock: another workflow is using this checkout"
            ) from error
        raise
    return handle


async def _run_units_step(input: Json) -> Json:
    run_dir, cwd, step, state = (input[key] for key in ("run_dir", "cwd", "step", "state"))
    config = config_for(step, state)
    inputs = state.get("inputs", {})
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
    pipeline = config["pipeline"]

    def flush_log() -> str:
        return write_log(run_dir, step["id"], input["step_run_id"], "\n".join(lines) + "\n")

    async def process_unit(node: Json) -> Json:
        unit, unit_cwd, unit_config = dict(node["unit"]), node["cwd"], node["config"]
        if pipeline == "implement":
            # Key the ledger by the branch the claim will attach to, not the plan slug.
            head = await execute(
                "git", ["symbolic-ref", "--quiet", "--short", "HEAD"], {"cwd": unit_cwd, "env": env}
            )
            if ok(head) and head["stdout"].strip():
                unit["branch"] = head["stdout"].strip()
        key = unit_key(unit)

        def log(line: str) -> None:
            lines.append(f"[{unit['ref']}] {line}")

        existing = read_unit(run_dir, key)
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
                "caps": unit_config["caps"],
            }
        )
        if existing:
            log(f"resume from {resume_phase}")

        def persist() -> None:
            write_unit(run_dir, key, ledger)

        def checkpoint(patch: Json) -> None:
            _apply_patch(ledger, patch)
            persist()

        def make_ctx() -> Json:
            ctx = {
                "unit": ledger["unit"],
                "ledger": ledger,
                "cwd": unit_cwd,
                "run_dir": run_dir,
                "env": env,
                "exec": execute,
                "config": unit_config,
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
        # ticket-plan epic children with branch_mode=current plan in the
        # checkout as it is: no claim, no branch, no worktree.
        in_place = pipeline == "ticket-plan" and unit_config.get("branch_mode") == "current"
        for phase in PIPELINE_PHASES[pipeline]:
            if input.get("signal") is not None and input["signal"].is_set():
                break
            if in_place and phase in ("claim", "worktree"):
                continue
            remote = unit_config.get("remote", GITHUB_DEFAULT)
            skips = (
                NO_GITHUB_SKIPS.get(remote["kind"], ()) if pipeline in ("ticket", "plan") else ()
            )
            # The GitHub-phase skip is decided by the detected remote, not the
            # resume cursor, so handle it before `_should_run`. Otherwise a unit
            # whose remote changed to none/other on resume filters out the
            # already-persisted `pr` phase and finishes `failed: no verdict
            # recorded` instead of a truthful verdict.
            if phase in skips and not stopped:
                log(f"{phase}: skipped, no GitHub remote")
                if phase == "pr" and "verdict" not in ledger:
                    pr_opened = resume_phase in PHASES and PHASES.index("pr") <= PHASES.index(
                        resume_phase
                    )
                    if pr_opened:
                        # A PR was opened on an earlier GitHub run; the remote is
                        # no longer GitHub, so there is nothing left to do.
                        ledger["verdict"] = "skipped"
                        ledger["reason"] = (
                            "no-github-remote: origin is no longer GitHub; the PR for "
                            f"{ledger['unit']['branch']} was already opened"
                        )
                    else:
                        ledger["verdict"] = "no-pr"
                        ledger["reason"] = no_pr_reason(
                            remote, ledger["unit"]["branch"], ledger["unit"]["worktree"]
                        )
                    # Do not advance last_phase to `pr`: leaving it at the last
                    # completed phase lets a resume (after a GitHub `origin` is
                    # added) re-enter `pr` instead of skipping past it.
                    persist()
                continue
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
                elif phase == "push":
                    result = await first_push(ctx, runners, hooks)
                else:
                    emit_phase(phase)
                    result = await runners[phase](ctx)
                    fold(phase, result)
            except Exception as error:
                result = fail(f"{phase}: {error}")
            if phase in ("review", "watch", "push") and result.get("patch"):
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
            elif phase == "implement" and pipeline == "implement":
                # The implement pipeline ends here: nothing to push, review or watch.
                output = result.get("output", {})
                ledger["verdict"] = "all-green"
                ledger["reason"] = (
                    f"implemented: {output.get('done', 0)} of {output.get('tasks', 0)} tasks "
                    f"in {output.get('commits', 0)} commits (failed {output.get('failed', 0)})"
                )
            elif phase == "plan" and pipeline == "ticket-plan":
                # The ticket-plan pipeline ends with the written plan.
                ledger["verdict"] = "plan-written"
                ledger["reason"] = f"plan: {ledger.get('plan_path', '?')}"
            elif phase == "claim" and ledger["unit"].get("adopted") and resume_phase is None:
                # F1: an open PR for this ticket already exists; resume it at
                # the watch loop instead of planning a duplicate.
                resume_phase = "request-review"
                ledger["last_phase"] = resume_phase
                log(f"claim: adopted open PR #{ledger['unit']['pr']['number']}; resuming at watch")
            persist()
        if "verdict" not in ledger:
            ledger.update(verdict="failed", reason="no verdict recorded")
            persist()
        if ledger["verdict"] == "merged" and not ledger["unit"].get("pr"):
            # F2: a merged verdict always names the PR gh reported merged.
            ledger.update(verdict="failed", reason="merged claimed without a pull request")
            persist()
        event = {"type": "unit.done", "unit": unit["ref"], "verdict": ledger["verdict"]}
        if "reason" in ledger:
            event["message"] = ledger["reason"]
        input["emit"](event)
        flush_log()
        return unit_row(ledger)

    # Items: plain refs or expansion specs (ref, state, depends_on, repo,
    # serialize). Each becomes a node with its own checkout and config.
    specs = normalize_items(input["items"])

    async def origin_url(path: str) -> str | None:
        result = await execute("git", ["remote", "get-url", "origin"], {"cwd": path, "env": env})
        return result["stdout"].strip() if ok(result) and result["stdout"].strip() else None

    project_slug = None
    if any(spec.get("repo") for spec in specs):
        url = await origin_url(cwd)
        project_slug = repo_slug(url) if url else None
    repo_paths = parse_repo_paths(str(inputs.get("repo_paths", "") or ""))
    nodes: list[Json] = []
    fixed_rows: list[tuple[int, Json]] = []
    seen: set[str] = set()

    def settle(index: int, row: Json, message: str) -> None:
        lines.append(f"[{row['unit']['ref']}] {message}")
        fixed_rows.append((index, row))
        input["emit"](
            {
                "type": "unit.done",
                "unit": row["unit"]["ref"],
                "verdict": row["verdict"],
                "message": row["reason"],
            }
        )

    for index, spec in enumerate(specs):
        unit_cwd = (
            await resolve_repo(spec["repo"], cwd, project_slug, repo_paths, origin_url)
            if spec.get("repo")
            else cwd
        )
        foreign = unit_cwd is not None and Path(unit_cwd).resolve() != Path(cwd).resolve()
        unit = make_unit(
            pipeline, spec["ref"], unit_cwd or cwd, run_dir, "" if foreign else config["base"]
        )
        if spec.get("repo"):
            unit["repo"] = spec["repo"]
        if foreign:
            # Another repository: its own worktree root and ledger key, its
            # default branch as the base. It never runs in the current tree.
            name = worktree_slug(Path(unit_cwd or cwd).name)
            unit["key"] = f"{name}/{unit['branch']}"
            unit["worktree"] = str(
                Path(run_dir) / "worktrees" / name / worktree_slug(unit["branch"])
            )
        elif config["worktree_mode"] == "current" or pipeline in ATTACHED_PIPELINES:
            unit["worktree"] = str(Path(cwd).resolve())
        if pipeline == "ticket-plan" and config.get("branch_mode") == "current":
            unit["worktree"] = str(Path(unit_cwd or cwd).resolve())
        key = unit_key(unit)
        if key in seen:
            lines.append(
                f"[{spec['ref']}] duplicate of an earlier item (branch {unit['branch']}); skipped"
            )
            continue
        seen.add(key)
        if unit_cwd is None:
            # Kept in the DAG as a pre-settled skip, so its dependents are
            # blocked by it instead of running without it.
            reason = (
                f"repo {spec['repo']}: no local checkout found; map it with the repo_paths input"
            )
            skip: Json = {"unit": unit, "cleaned": False, "verdict": "skipped", "reason": reason}
            lines.append(f"[{unit['ref']}] {reason}")
            input["emit"](
                {"type": "unit.done", "unit": unit["ref"], "verdict": "skipped", "message": reason}
            )
            nodes.append(
                {
                    "index": index,
                    "spec": spec,
                    "unit": unit,
                    "key": key,
                    "cwd": cwd,
                    "config": dict(config),
                    "locks": set(),
                    "settled": skip,
                }
            )
            continue
        if is_terminal_state(spec.get("state")):
            reason = f"tracker state {spec['state']}: nothing to do"
            settle(
                index,
                {"unit": unit, "cleaned": False, "verdict": "skipped", "reason": reason},
                reason,
            )
            continue
        unit_config = dict(config)
        if foreign:
            unit_config["base"] = ""
        nodes.append(
            {
                "index": index,
                "spec": spec,
                "unit": unit,
                "key": key,
                "cwd": unit_cwd,
                "config": unit_config,
                "locks": {(unit_cwd, name) for name in spec.get("serialize", [])},
            }
        )
    edges = dependency_edges(nodes, lines.append)

    def _pending(node: Json) -> bool:
        existing = read_unit(run_dir, node["key"])
        return existing is None or not is_done(existing)

    # Classify each checkout's origin once, before any unit, and log the one
    # detection line. Skip it when every unit is already done (nothing would
    # read it), so a done-resume stays a pure no-op. `resolve_base` and the
    # phase skips read the result via `config["remote"]`; direct phase tests
    # default to github.
    remotes: dict[str, Json] = {}
    for node in nodes:
        if "settled" in node:
            continue
        if node["cwd"] not in remotes and _pending(node):
            remotes[node["cwd"]] = await detect_remote(
                {"cwd": node["cwd"], "env": env, "exec": execute, "log": lines.append}
            )
        node["config"]["remote"] = remotes.get(node["cwd"], GITHUB_DEFAULT)

    workers = (
        1
        if config["worktree_mode"] == "current"
        else max(1, min(concurrency_of(step, inputs), len(nodes) or 1))
    )
    success = SUCCESS_VERDICTS.get(pipeline, frozenset(("merged",)))
    stop_on_failure = str(inputs.get("on_child_failure", "") or "").strip() == "stop"
    by_key = {node["key"]: node for node in nodes}
    pending = [node["key"] for node in nodes if "settled" not in node]
    results: dict[str, Json] = {node["key"]: node["settled"] for node in nodes if "settled" in node}
    running: dict[asyncio.Task[Json], str] = {}
    held: set[tuple[str, str]] = set()
    stop_reason: str | None = None

    def signalled() -> bool:
        return input.get("signal") is not None and input["signal"].is_set()

    def block_failed_dependents() -> None:
        # A dependent of a child that ended without success never runs,
        # transitively: its own dependents are blocked in the same pass.
        changed = True
        while changed:
            changed = False
            for key in list(pending):
                failed = [
                    dep
                    for dep in edges[key]
                    if dep in results and results[dep].get("verdict") not in success
                ]
                if not failed:
                    continue
                pending.remove(key)
                refs = [by_key[dep]["unit"]["ref"] for dep in failed]
                reason = "dependency: " + ", ".join(
                    f"{by_key[dep]['unit']['ref']} ended {results[dep].get('verdict', 'failed')}"
                    for dep in failed
                )
                row = {
                    "unit": by_key[key]["unit"],
                    "cleaned": False,
                    "verdict": "blocked",
                    "reason": reason,
                    "blocked_by": refs,
                }
                results[key] = row
                lines.append(f"[{row['unit']['ref']}] blocked ({reason})")
                input["emit"](
                    {
                        "type": "unit.done",
                        "unit": row["unit"]["ref"],
                        "verdict": "blocked",
                        "message": reason,
                    }
                )
                changed = True

    try:
        while pending or running:
            if not signalled() and stop_reason is None:
                block_failed_dependents()
                for key in list(pending):
                    if len(running) >= workers:
                        break
                    if any(dep not in results for dep in edges[key]):
                        continue
                    locks = by_key[key]["locks"]
                    if locks & held:
                        continue
                    pending.remove(key)
                    held |= locks
                    running[asyncio.create_task(process_unit(by_key[key]))] = key
            if not running:
                break
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                key = running.pop(task)
                held -= by_key[key]["locks"]
                row = task.result()
                results[key] = row
                if (
                    stop_on_failure
                    and stop_reason is None
                    and row.get("verdict") in FAILURE_VERDICTS
                ):
                    stop_reason = (
                        f"on_child_failure=stop: {row['unit']['ref']} ended {row['verdict']}"
                    )
                    lines.append(stop_reason)
    except BaseException:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        raise
    if stop_reason is not None:
        for key in pending:
            row = {
                "unit": by_key[key]["unit"],
                "cleaned": False,
                "verdict": "skipped",
                "reason": stop_reason,
            }
            results[key] = row
            input["emit"](
                {
                    "type": "unit.done",
                    "unit": row["unit"]["ref"],
                    "verdict": "skipped",
                    "message": stop_reason,
                }
            )
    ordered = sorted(
        [(by_key[key]["index"], row) for key, row in results.items()] + fixed_rows,
        key=lambda pair: pair[0],
    )
    rows = [row for _, row in ordered]
    return {"verdict": _summarize(rows), "outputs": {"units": rows}, "log": flush_log()}
