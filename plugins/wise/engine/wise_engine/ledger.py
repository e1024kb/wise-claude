from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .constants import TERMINAL_RUN

RUN_HISTORY_CAP_DEFAULT = 25
SESSION_STALE_SECS_DEFAULT = 1800
HISTORY_TERMINAL_RUN = TERMINAL_RUN
STEP_ID_RE = re.compile(r"[a-z][a-z0-9_-]*\Z")
STEP_RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_ISO_SECONDS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ulid_lock = threading.Lock()
_ulid_last_ms = -1
_ulid_last_rand = 0
Json = dict[str, Any]


class LedgerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def utc_now(date: datetime | None = None) -> str:
    return (
        (date or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def env_positive_int(name: str, default: int, env: Mapping[str, str] | None = None) -> int:
    raw = (os.environ if env is None else env).get(name, "").strip()
    if not re.fullmatch(r"[+-]?[0-9]+", raw):
        return default
    return value if (value := int(raw)) > 0 else default


def session_is_fresh(iso_ts: str | None, stale_after: float, now_ms: float | None = None) -> bool:
    if not iso_ts or not _ISO_SECONDS_RE.fullmatch(iso_ts):
        return False
    try:
        stamp = datetime.fromisoformat(iso_ts).timestamp() * 1000
    except ValueError:
        return False
    return ((time.time() * 1000 if now_ms is None else now_ms) - stamp) / 1000 <= stale_after


def new_ulid(now_ms: float | None = None) -> str:
    global _ulid_last_ms, _ulid_last_rand
    with _ulid_lock:
        stamp = max(0, int(time.time() * 1000 if now_ms is None else now_ms))
        if stamp <= _ulid_last_ms:
            stamp = _ulid_last_ms
            _ulid_last_rand = (_ulid_last_rand + 1) % (1 << 80)
        else:
            _ulid_last_ms = stamp
            _ulid_last_rand = secrets.randbits(80)
        value = (stamp << 80) | _ulid_last_rand
        return "".join(_B32[(value >> (5 * i)) & 31] for i in reversed(range(26)))


def state_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "state.json"


def write_json_atomic(path: str | Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_state(run_dir: str | Path, state: Json) -> None:
    write_json_atomic(state_path(run_dir), state)


def read_state(run_dir: str | Path) -> Json:
    return json.loads(state_path(run_dir).read_text(encoding="utf-8"))


def _read_state_loose(run_dir: str | Path) -> Json | None:
    try:
        value = read_state(run_dir)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return None


def empty_usage(pool: str = "subscription") -> Json:
    return dict(input=0, output=0, cache_read=0, cache_write=0, pool=pool)


def empty_usage_by_pool() -> Json:
    return {
        "subscription": empty_usage(),
        "api-key": empty_usage("api-key"),
        "by_harness": {},
        "by_step": {},
    }


def add_usage(into: Json, usage: Json) -> None:
    for key in ("input", "output", "cache_read", "cache_write"):
        into[key] += usage[key]
    if "cost_usd" in usage:
        into["cost_usd"] = into.get("cost_usd", 0) + usage["cost_usd"]
    incoming = usage.get("cost_source", "reported" if "cost_usd" in usage else "none")
    current = into.get("cost_source", "none")
    into["cost_source"] = (
        current if incoming == "none" else incoming if current in ("none", incoming) else "priced"
    )


def fold_usage_views(state: Json, fold: Json) -> None:
    step, harness, usage = fold["step"], fold["harness"], fold["usage"]
    pool = usage["pool"]
    views = state["usage"]
    add_usage(views[pool], usage)
    add_usage(views["by_harness"].setdefault(harness, empty_usage(pool)), usage)
    add_usage(views.setdefault("by_step", {}).setdefault(step, empty_usage(pool)), usage)
    if step in state["steps"]:
        add_usage(state["steps"][step].setdefault("usage", empty_usage(pool)), usage)


def usage_tokens(usage: Json) -> int:
    return usage["input"] + usage["output"] + usage["cache_write"]


def usage_total(usage: Json) -> Json:
    total = empty_usage(
        "api-key"
        if usage_tokens(usage["subscription"]) == 0 and usage_tokens(usage["api-key"]) > 0
        else "subscription"
    )
    add_usage(total, usage["subscription"])
    add_usage(total, usage["api-key"])
    return total


def _check_step_id(step_id: str) -> None:
    if not STEP_ID_RE.fullmatch(step_id):
        raise LedgerError(
            "INVALID_STEP_ID", f"step-id {json.dumps(step_id)} must match /^[a-z][a-z0-9_-]*$/"
        )


def init_state(
    *,
    run_dir: str | Path,
    run_id: str,
    workflow: Json,
    step_ids: list[str],
    cwd: str | None = None,
    harness_session: str | None = None,
    profile: str = "medium",
    now: str | None = None,
) -> Json:
    steps: Json = {}
    for step_id in step_ids:
        _check_step_id(step_id)
        if step_id in steps:
            raise LedgerError("DUPLICATE_STEP_ID", f"duplicate step-id {json.dumps(step_id)}")
        steps[step_id] = {"status": "pending", "attempts": 0}
    now = now or utc_now()
    state = dict(
        version=2,
        run_id=run_id,
        workflow=workflow,
        cwd=cwd or os.getcwd(),
        project=None,
        status="initializing",
        profile=profile,
        answers={},
        context={},
        inputs={},
        resolved={},
        caps={},
        usage=empty_usage_by_pool(),
        steps=steps,
        outputs={},
        started_at=now,
        last_activity_at=now,
    )
    if harness_session:
        state["harness_session"] = harness_session
    (Path(run_dir) / "logs").mkdir(parents=True, exist_ok=True)
    write_state(run_dir, state)
    return state


def start_run(run_dir: str | Path, ctx: Json, *, now: str | None = None) -> Json:
    state = read_state(run_dir)
    for key in ("project", "profile", "permissions", "harness_session"):
        if key in ctx:
            state[key] = ctx[key]
    for key in ("answers", "context", "resolved", "caps", "inputs"):
        if key in ctx:
            state[key].update(ctx[key])
    if "provider_permissions" in ctx:
        state["provider_permissions"] = dict(ctx["provider_permissions"])
    state["outputs"].update(ctx.get("inputs", {}))
    state.update(status="running", last_activity_at=now or utc_now())
    write_state(run_dir, state)
    return state


def _step(state: Json, step_id: str) -> Json:
    if step_id not in state["steps"]:
        raise LedgerError("NO_SUCH_STEP", f"no such step: {step_id}")
    return state["steps"][step_id]


def update_step(run_dir: str | Path, step_id: str, patch: Json, *, now: str | None = None) -> Json:
    state = read_state(run_dir)
    _step(state, step_id).update(patch)
    state["last_activity_at"] = now or utc_now()
    write_state(run_dir, state)
    return state


def start_step(run_dir: str | Path, step_id: str, *, now: str | None = None) -> str:
    state = read_state(run_dir)
    step = _step(state, step_id)
    ident, now = new_ulid(), now or utc_now()
    step.update(
        status="running", step_run_id=ident, started_at=now, attempts=step.get("attempts", 0) + 1
    )
    for key in ("completed_at", "verdict", "error"):
        step.pop(key, None)
    state["last_activity_at"] = now
    write_state(run_dir, state)
    return ident


def update_run(run_dir: str | Path, patch: Json, *, now: str | None = None) -> Json:
    state = read_state(run_dir)
    state.update(patch)
    state["last_activity_at"] = now or utc_now()
    write_state(run_dir, state)
    return state


def record_output(run_dir: str | Path, name: str, value: Any, *, now: str | None = None) -> Json:
    state = read_state(run_dir)
    state["outputs"][name] = value
    return update_run(run_dir, state, now=now)


def reset_running(run_dir: str | Path, *, now: str | None = None) -> Json:
    state = read_state(run_dir)
    for step in state.get("steps", {}).values():
        if step["status"] == "running":
            step["status"] = "pending"
            step.pop("started_at", None)
            step.pop("step_run_id", None)
    state["status"] = "running"
    return update_run(run_dir, state, now=now)


def dump_state(run_dir: str | Path) -> str:
    return json.dumps(read_state(run_dir), ensure_ascii=False, indent=2)


def _run_dirs(root: str | Path) -> list[Path]:
    try:
        return sorted(Path(root).iterdir())
    except OSError:
        return []


def _str(state: Json, key: str, default: str = "") -> str:
    value = state.get(key)
    return value if isinstance(value, str) else default


def _workflow(state: Json, default: str = "") -> str:
    workflow = state.get("workflow")
    return _str(workflow, "name", default) if isinstance(workflow, dict) else default


def list_runs(runs_root: str | Path) -> list[Json]:
    rows = []
    for directory in _run_dirs(runs_root):
        if not state_path(directory).exists():
            continue
        state = _read_state_loose(directory)
        rows.append(
            dict(
                run_id=directory.name,
                status="<unreadable>" if state is None else _str(state, "status", "?"),
                workflow="" if state is None else _workflow(state, "?"),
                last_activity_at="" if state is None else _str(state, "last_activity_at", "?"),
            )
        )
    return rows


def format_runs_table(rows: list[Json]) -> str:
    if not rows:
        return "(no runs in this workspace yet)"
    return "\n".join(
        [f"{'RUN ID':<26}  {'STATUS':<10}  {'WORKFLOW':<24}  LAST ACTIVITY"]
        + [
            f"{r['run_id']:<26}  {r['status']:<10}  {r['workflow']:<24}  {r['last_activity_at']}"
            for r in rows
        ]
    )


def list_resumable_runs(runs_root: str | Path) -> list[Json]:
    rows = []
    for directory in _run_dirs(runs_root):
        state = _read_state_loose(directory)
        if state is None or state.get("status") in HISTORY_TERMINAL_RUN:
            continue
        row = dict(
            run_id=_str(state, "run_id") or directory.name,
            workflow=_workflow(state),
            status=_str(state, "status", "initializing"),
            started_at=_str(state, "started_at"),
            last_activity_at=_str(state, "last_activity_at"),
            cwd=_str(state, "cwd"),
        )
        if isinstance(state.get("completed_at"), str):
            row["completed_at"] = state["completed_at"]
        if state.get("gate"):
            row["gate"] = state["gate"]
        rows.append(row)
    return sorted(rows, key=lambda r: r["last_activity_at"], reverse=True)


def prune_runs(runs_root: str | Path, env: Mapping[str, str] | None = None) -> Json:
    result: Json = {"pruned": [], "failed": []}
    cap = env_positive_int("WISE_RUN_HISTORY_CAP", RUN_HISTORY_CAP_DEFAULT, env)
    root = Path(runs_root).resolve()
    if not root.is_dir():
        return result
    entries = []
    for child in _run_dirs(root):
        if not child.is_dir():
            continue
        state = _read_state_loose(child)
        # Legacy run history stays protected until an explicit archive operation.
        legacy = (child / "state.yaml").exists()
        terminal = not legacy and (state is None or state.get("status") in HISTORY_TERMINAL_RUN)
        last = (_str(state, "last_activity_at") or _str(state, "started_at")) if state else ""
        entries.append((last, child.name, terminal, child))
    protected = sum(not e[2] for e in entries)
    terminal_entries = sorted((e for e in entries if e[2]), reverse=True)
    for _, name, _, child in terminal_entries[max(0, cap - protected) :]:
        try:
            if not child.resolve().is_relative_to(root) or child.resolve() == root:
                continue
            if child.is_symlink():
                child.unlink()
            else:
                shutil.rmtree(child)
            result["pruned"].append(name)
        except OSError as error:
            result["failed"].append({"run_id": name, "reason": str(error)})
    return result


def find_runs_by_session(
    runs_root: str | Path,
    session_id: str,
    env: Mapping[str, str] | None = None,
    now_ms: float | None = None,
) -> list[Json]:
    stale_after = env_positive_int("WISE_SESSION_STALE_SECS", SESSION_STALE_SECS_DEFAULT, env)
    rows = []
    for directory in _run_dirs(runs_root):
        state = _read_state_loose(directory)
        if (
            state is None
            or state.get("harness_session") != session_id
            or state.get("status") in HISTORY_TERMINAL_RUN
        ):
            continue
        last = state.get("last_activity_at")
        last = last if isinstance(last, str) else None
        rows.append(
            dict(
                run_id=_str(state, "run_id") or directory.name,
                workflow=_workflow(state, "?"),
                status=_str(state, "status", "?"),
                last_activity_at=last,
                fresh=session_is_fresh(last, stale_after, now_ms),
            )
        )
    return rows


def format_session_run_row(row: Json) -> str:
    return "\t".join(
        [
            row["run_id"],
            row["workflow"],
            row["status"],
            row["last_activity_at"] if row["last_activity_at"] is not None else "?",
            "fresh" if row["fresh"] else "stale",
        ]
    )


def events_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "events.jsonl"


def _last_seq(data: bytes) -> int | float | None:
    for line in reversed(data.decode("utf-8", errors="replace").split("\n")):
        try:
            value = json.loads(line)
            if isinstance(value, dict) and type(value.get("seq")) in (int, float):
                return value["seq"]
        except ValueError:
            continue
    return None


def append_event(run_dir: str | Path, event: Json, *, now: str | None = None) -> Json:
    path = events_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    seq: int | float = 0
    lead = ""
    if path.exists():
        with path.open("rb") as source:
            size = source.seek(0, 2)
            source.seek(max(0, size - 65536))
            tail = source.read()
            if size > 65536 and b"\n" not in tail:
                source.seek(0)
                tail = source.read()
        lead = "\n" if tail and not tail.endswith(b"\n") else ""
        last = _last_seq(tail)
        if last is None and size > len(tail):
            last = _last_seq(path.read_bytes())
        seq = last if last is not None else 0
    full = {**event, "seq": seq + 1, "ts": now or utc_now()}
    with path.open("a", encoding="utf-8") as dest:
        dest.write(lead + json.dumps(full, ensure_ascii=False, separators=(",", ":")) + "\n")
    return full


def read_events(run_dir: str | Path, after: int = 0) -> list[Json]:
    path = events_path(run_dir)
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8", errors="replace").split("\n"):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(event, dict)
            and type(event.get("seq")) in (int, float)
            and event["seq"] > after
        ):
            result.append(event)
    return result


def unit_path(run_dir: str | Path, branch: str) -> Path:
    return Path(run_dir) / "units" / (quote(branch, safe="~!*'()") + ".json")


def read_unit(run_dir: str | Path, branch: str) -> Json | None:
    path = unit_path(run_dir, branch)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def write_unit(run_dir: str | Path, branch: str, ledger: Json) -> None:
    write_json_atomic(unit_path(run_dir, branch), ledger)


def list_units(run_dir: str | Path) -> list[Json]:
    result = []
    for path in _run_dirs(Path(run_dir) / "units"):
        if path.suffix == ".json":
            try:
                result.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return result


def log_paths(run_dir: str | Path, step_id: str, step_run_id: str) -> dict[str, str]:
    _check_step_id(step_id)
    if not STEP_RUN_ID_RE.fullmatch(step_run_id):
        raise LedgerError(
            "INVALID_STEP_RUN_ID",
            f"step-run-id {json.dumps(step_run_id)} must be a bare alphanumeric/_/- token",
        )
    base = Path(run_dir) / "logs" / f"{step_id}.{step_run_id}"
    return {"raw": str(base) + ".raw.jsonl", "log": str(base) + ".log"}


def write_log(run_dir: str | Path, step_id: str, step_run_id: str, content: str) -> str:
    path = Path(log_paths(run_dir, step_id, step_run_id)["log"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def append_raw_log(run_dir: str | Path, step_id: str, step_run_id: str, record: Any) -> str:
    path = Path(log_paths(run_dir, step_id, step_run_id)["raw"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as dest:
        dest.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return str(path)


def _exec(cmd: str, args: list[str]) -> str:
    return subprocess.check_output([cmd, *args], text=True, stderr=subprocess.PIPE)


def apply_worktree_include(
    repo_root: str | Path,
    worktree_dir: str | Path,
    *,
    exec_fn: Callable[[str, list[str]], str] = _exec,
) -> Json:
    result: Json = dict(copied=0, skipped=0, notices=[])
    repo, dest = Path(repo_root), Path(worktree_dir)
    inc = repo / ".worktreeinclude"
    if not inc.is_file():
        result["notices"].append("worktree-include: no .worktreeinclude - nothing to copy")
        return result
    try:
        stdout = exec_fn(
            "git",
            [
                "-C",
                str(repo),
                "ls-files",
                "-z",
                "--others",
                "--ignored",
                "--directory",
                "--no-empty-directory",
                f"--exclude-from={inc}",
            ],
        )
    except Exception as error:
        result["notices"].append(f"worktree-include: git ls-files failed ({error}); skipping")
        return result
    try:
        root_res, dest_res = repo.resolve(strict=True), dest.resolve(strict=True)
    except OSError as error:
        result["notices"].append(f"worktree-include: cannot resolve paths ({error}); skipping")
        return result
    for rel in filter(None, stdout.split("\0")):
        src, dst = repo / rel, dest / rel
        if not src.resolve().is_relative_to(root_res) or not dst.resolve().is_relative_to(dest_res):
            result["notices"].append(f"worktree-include: skip out-of-tree path {json.dumps(rel)}")
            result["skipped"] += 1
            continue
        if not src.exists():
            result["skipped"] += 1
            continue
        try:
            if rel.endswith("/") or src.is_dir():
                shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            result["copied"] += 1
        except OSError as error:
            result["notices"].append(
                f"worktree-include: failed to copy {json.dumps(rel)} ({error})"
            )
            result["skipped"] += 1
    result["notices"].append(
        f"worktree-include: copied {result['copied']} (skipped {result['skipped']})"
    )
    return result
