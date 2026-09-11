from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ledger import write_json_atomic
from .render import _json
from .scheduler import JS_WHITESPACE, UNDEFINED, number_text
from .steps.agent import headline, utf16_length, utf16_slice

PROGRESS_THROTTLE_MS = 30_000
STALE_AFTER_SECS_DEFAULT = 600
REPORT_TEXT_MAX = 200
REPORT_DATA_MAX_BYTES = 1024
DETAIL_MAX = 80
TEXT_MAX = 100
DETAIL_GAP_MS = 5_000
DETAIL_KEYS = (
    "file_path",
    "path",
    "notebook_path",
    "command",
    "pattern",
    "query",
    "url",
    "skill",
    "description",
    "prompt",
)
_SPACE = re.compile(f"[{re.escape(JS_WHITESPACE)}]+")
Json = dict[str, Any]


def now_ms() -> float:
    return time.time() * 1000


@dataclass
class ChannelTimers:
    now: Callable[[], float]
    set_timeout: Callable[[Callable[[], None], float], Any]
    clear_timeout: Callable[[Any], None]


def real_timers() -> ChannelTimers:
    return ChannelTimers(
        now_ms,
        lambda fn, ms: asyncio.get_running_loop().call_later(ms / 1000, fn),
        lambda handle: handle.cancel(),
    )


def _number(value: Any) -> float:
    return value if type(value) in (int, float) and math.isfinite(value) else 0


def _usage_tokens(usage: Any) -> float:
    return (
        sum(_number(value) for key, value in usage.items() if key.endswith("_tokens"))
        if isinstance(usage, dict)
        else 0
    )


def _first_line(text: str, maximum: int) -> str:
    line = _SPACE.sub(" ", text).strip(JS_WHITESPACE)
    return utf16_slice(line, 0, maximum - 1) + "…" if utf16_length(line) > maximum else line


def _last_tool_use(message: Any) -> Json | None:
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return None
    uses = [b for b in message["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
    if not uses:
        return None
    last = uses[-1]
    result = {"name": last["name"] if isinstance(last.get("name"), str) else "?"}
    if isinstance(last.get("input"), dict):
        for key in DETAIL_KEYS:
            value = last["input"].get(key)
            if isinstance(value, str) and value.strip(JS_WHITESPACE):
                result["detail"] = _first_line(value, DETAIL_MAX)
                break
    return result


def _last_text(message: Any) -> str | None:
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return None
    texts = [
        b["text"]
        for b in message["content"]
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    ]
    return (_first_line(texts[-1], TEXT_MAX) or None) if texts else None


class ChildTracker:
    def __init__(
        self,
        *,
        step: str,
        throttle_ms: float = PROGRESS_THROTTLE_MS,
        detail_gap_ms: float = DETAIL_GAP_MS,
        now: Callable[[], float] = now_ms,
    ):
        self.step = step
        self.throttle_ms = throttle_ms
        self.detail_gap_ms = detail_gap_ms
        self.now = now
        self.turn = 0
        self.tool: str | None = None
        self.detail: str | None = None
        self.text: str | None = None
        self.tokens: float = 0
        self.reports = 0
        self.started = self.last_activity = self.last_emit = now()

    def snapshot(self) -> Json:
        snap = dict(
            step=self.step,
            turn=self.turn,
            tokens=self.tokens,
            last_activity=datetime.fromtimestamp(self.last_activity / 1000, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            reports=self.reports,
            elapsed_ms=self.last_activity - self.started,
        )
        for key in ("tool", "detail", "text"):
            value = getattr(self, key)
            if value is not None:
                snap[key] = value
        return snap

    def ingest(self, event: Json) -> Json | None:
        self.last_activity = self.now()
        parsed = event.get("parsed")
        if not isinstance(parsed, dict):
            return None
        before_tool, before_detail = self.tool, self.detail
        if parsed.get("type") == "assistant":
            self.turn += 1
            use = _last_tool_use(parsed.get("message"))
            if use:
                self.tool = use["name"]
                self.detail = use.get("detail")
            text = _last_text(parsed.get("message"))
            if text is not None:
                self.text = text
            if isinstance(parsed.get("message"), dict):
                self.tokens += _usage_tokens(parsed["message"].get("usage"))
        elif parsed.get("type") == "result":
            self.tokens = max(self.tokens, _usage_tokens(parsed.get("usage")))
        elif "usage" in parsed:
            self.tokens += _usage_tokens(parsed["usage"])
        since = self.last_activity - self.last_emit
        changed = self.tool != before_tool
        detail_changed = (
            not changed and self.detail != before_detail and since >= self.detail_gap_ms
        )
        if not changed and not detail_changed and since < self.throttle_ms:
            return None
        self.last_emit = self.last_activity
        return self.snapshot()

    def report(self) -> None:
        self.reports += 1
        self.touch()

    def touch(self) -> None:
        self.last_activity = self.now()

    def last_activity_ms(self) -> float:
        return self.last_activity


def create_child_tracker(opts: Json) -> ChildTracker:
    return ChildTracker(**opts)


def _fmt_tokens(count: float) -> str:
    return f"{math.floor(count / 1000 + 0.5)}k" if count >= 1000 else number_text(count)


def _fmt_elapsed(milliseconds: float) -> str:
    seconds = math.floor(milliseconds / 1000)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    return (
        f"{minutes}m{seconds % 60:02}s" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02}m"
    )


def progress_line(progress: Json) -> str:
    parts = [f"turn {progress['turn']}"]
    if "tool" in progress:
        parts.append(
            f"tool {progress['tool']}"
            + (f" {progress['detail']}" if progress.get("detail") else "")
        )
    parts.append(f"{_fmt_tokens(progress['tokens'])} tokens")
    if progress["reports"] > 0:
        parts.append(f"{progress['reports']} report" + ("" if progress["reports"] == 1 else "s"))
    if "elapsed_ms" in progress:
        parts.append(_fmt_elapsed(progress["elapsed_ms"]))
    line = ", ".join(parts)
    return f"{line}: {progress['text']}" if progress.get("text") else line


class StaleWatch:
    def __init__(
        self,
        *,
        stale_ms: float,
        timers: ChannelTimers,
        last_activity_ms: Callable[[], float],
        nudge: Callable[[float], bool],
        kill: Callable[[], None],
        paused: Callable[[], bool] | None = None,
    ):
        self.stale_ms = stale_ms
        self.timers = timers
        self.last_activity_ms = last_activity_ms
        self.nudge = nudge
        self.kill = kill
        self.paused = paused
        self.nudged = False
        self.stopped = False
        self.handle = None
        self._arm(stale_ms)

    def _arm(self, ms: float) -> None:
        if not self.stopped:
            self.handle = self.timers.set_timeout(self._fire, max(1, ms))

    def _fire(self) -> None:
        if self.stopped:
            return
        if self.paused and self.paused():
            self._arm(self.stale_ms)
            return
        idle = self.timers.now() - self.last_activity_ms()
        if idle < self.stale_ms:
            self.nudged = False
            self._arm(self.stale_ms - idle)
        elif not self.nudged and self.nudge(idle):
            self.nudged = True
            self._arm(self.stale_ms)
        else:
            self.stopped = True
            self.kill()

    def stop(self) -> None:
        self.stopped = True
        if self.handle is not None:
            self.timers.clear_timeout(self.handle)


def start_stale_watch(opts: Json) -> StaleWatch:
    return StaleWatch(**opts)


def stale_nudge_text(idle_ms: float) -> str:
    minutes = max(1, math.floor(idle_ms / 60000 + 0.5))
    return (
        f"You have been idle for {minutes} minute"
        + ("" if minutes == 1 else "s")
        + ". Finish with your structured result now."
    )


def clip_report_text(text: str) -> str:
    return headline(text, REPORT_TEXT_MAX)


def clip_report_data(data: Any) -> Json | None:
    if not isinstance(data, dict):
        return None
    try:
        text = _json(data)
        return data if len(text.encode("utf-8")) <= REPORT_DATA_MAX_BYTES else None
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return None


def _path_lookup(root: Any, path: list[str]) -> Any:
    current = root
    for segment in path:
        if isinstance(current, list) and re.fullmatch("[0-9]+", segment):
            index = int(segment)
            current = current[index] if index < len(current) else UNDEFINED
        elif isinstance(current, dict):
            current = current.get(segment, UNDEFINED)
        else:
            return UNDEFINED
        if current is UNDEFINED:
            return UNDEFINED
    return current


def resolve_context_key(state: Json, key: str) -> Any:
    path = [p for p in key.split(".") if p]
    if not path:
        return None
    for owner in ("context", "outputs"):
        value = _path_lookup(state.get(owner, {}), path)
        if value is not UNDEFINED:
            return value
    step = state.get("steps", {}).get(path[0])
    if isinstance(step, dict) and "outputs" in step:
        value = _path_lookup(step["outputs"], path[1:])
        if value is not UNDEFINED:
            return value
    value = _path_lookup(state.get("inputs", {}), path)
    return None if value is UNDEFINED else value


def checkpoint_path(run_dir: str | Path, step_id: str) -> str:
    return str(Path(run_dir) / "checkpoints" / f"{step_id}.json")


def write_checkpoint(run_dir: str | Path, step_id: str, data: Any) -> str:
    path = checkpoint_path(run_dir, step_id)
    write_json_atomic(path, None if data is UNDEFINED else data)
    return path


def answer_from_decisions(
    question: str, options: list[str] | None = None, decisions: dict[str, str] | None = None
) -> str | None:
    def norm(text):
        return text.strip(JS_WHITESPACE).lower()

    q = norm(question)
    entries = list((decisions or {}).items())
    for key, value in entries:
        if norm(key) == q:
            return value
    for key, value in entries:
        normalized = norm(key)
        if normalized and (normalized in q or q in normalized):
            return value
    if options:
        for _, value in entries:
            if any(norm(option) == norm(value) for option in options):
                return value
        return options[0]
    return None
