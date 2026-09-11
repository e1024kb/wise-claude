from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wise_engine.channel import (
    ChannelTimers,
    ChildTracker,
    answer_from_decisions,
    clip_report_data,
    clip_report_text,
    progress_line,
    resolve_context_key,
    start_stale_watch,
    stale_nudge_text,
    write_checkpoint,
)


class FakeTimers:
    def __init__(self):
        self.clock = 1_000_000.0
        self.queue: list[dict[str, Any]] = []
        self.timers = ChannelTimers(lambda: self.clock, self.set_timeout, self.clear_timeout)

    def set_timeout(self, fn, ms):
        entry = dict(at=self.clock + ms, fn=fn, cancelled=False)
        self.queue.append(entry)
        return entry

    def clear_timeout(self, entry):
        entry["cancelled"] = True

    def advance(self, ms):
        target = self.clock + ms
        while due := sorted(
            (e for e in self.queue if not e["cancelled"] and e["at"] <= target),
            key=lambda e: e["at"],
        ):
            entry = due[0]
            self.queue.remove(entry)
            self.clock = entry["at"]
            entry["fn"]()
        self.clock = target

    def pending(self):
        return sum(not e["cancelled"] for e in self.queue)


def raw(parsed):
    return {"parsed": parsed}


def assistant(tool=None, usage=None, *, inputs=None, text=None):
    content = []
    if text is not None or tool is None:
        content.append({"type": "text", "text": text if text is not None else "..."})
    if tool is not None:
        content.append({"type": "tool_use", "name": tool, "input": inputs or {}})
    return raw({"type": "assistant", "message": {"content": content, "usage": usage or {}}})


def test_tracker_throttling_and_counters() -> None:
    timers = FakeTimers()
    tracker = ChildTracker(step="work", now=timers.timers.now)
    emitted = []
    events = [
        (raw({"type": "system", "subtype": "init"}), 0),
        (assistant("Read", {"input_tokens": 1000, "output_tokens": 20}), 1000),
        (assistant("Read", {"input_tokens": 1000, "output_tokens": 20}), 1000),
        (assistant("Edit", {"input_tokens": 1000, "output_tokens": 20}), 1000),
        (assistant(None, {"output_tokens": 100}), 1000),
        (assistant("Edit"), 5000),
        (assistant("Edit"), 30000),
        (assistant("Edit"), 29000),
        (raw({"type": "result", "usage": {"input_tokens": 40000, "output_tokens": 2000}}), 1000),
    ]
    for event, delay in events:
        timers.advance(delay)
        if progress := tracker.ingest(event):
            emitted.append(progress_line(progress))
    assert emitted == [
        "turn 1, tool Read, 1k tokens, 1s",
        "turn 3, tool Edit, 3k tokens, 3s",
        "turn 6, tool Edit, 3k tokens, 39s: ...",
        "turn 7, tool Edit, 42k tokens, 1m09s: ...",
    ]
    assert tracker.snapshot() == dict(
        step="work",
        turn=7,
        tool="Edit",
        text="...",
        tokens=42000,
        reports=0,
        elapsed_ms=69000,
        last_activity="1970-01-01T00:17:49.000Z",
    )
    tracker.report()
    assert (
        progress_line(tracker.snapshot()) == "turn 7, tool Edit, 42k tokens, 1 report, 1m09s: ..."
    )
    assert (
        progress_line(dict(step="s", turn=0, tokens=12, reports=0, last_activity=""))
        == "turn 0, 12 tokens"
    )


def test_tracker_detail_gap_and_clipping() -> None:
    timers = FakeTimers()
    tracker = ChildTracker(step="work", now=timers.timers.now)
    emitted = []
    events = [
        (
            assistant(
                "Read", inputs={"file_path": "src/a.ts"}, text="Looking at the parser.\nMore."
            ),
            1000,
        ),
        (assistant("Read", inputs={"file_path": "src/b.ts"}), 1000),
        (assistant("Read", inputs={"file_path": "src/c.ts"}), 5000),
        (
            assistant(
                "Bash", inputs={"command": "git log --oneline -5\nwith a second line " + "x" * 100}
            ),
            1000,
        ),
        (assistant("Grep", inputs={"pattern": "foo"}, text=""), 1000),
    ]
    for event, delay in events:
        timers.advance(delay)
        if progress := tracker.ingest(event):
            emitted.append(progress_line(progress))
    assert emitted == [
        "turn 1, tool Read src/a.ts, 0 tokens, 1s: Looking at the parser. More.",
        "turn 3, tool Read src/c.ts, 0 tokens, 7s: Looking at the parser. More.",
        "turn 4, tool Bash git log --oneline -5 with a second line "
        + "x" * 39
        + "…, 0 tokens, 8s: Looking at the parser. More.",
        "turn 5, tool Grep foo, 0 tokens, 9s: Looking at the parser. More.",
    ]
    assert tracker.snapshot()["elapsed_ms"] == 9000
    assert tracker.snapshot()["detail"] == "foo"


def test_stale_watch_activity_pause_nudge_and_kill() -> None:
    timers = FakeTimers()
    activity = timers.clock
    paused = False
    nudges = []
    kills = []

    def nudge(idle: float) -> bool:
        nudges.append(idle)
        return True

    watch = start_stale_watch(
        dict(
            stale_ms=10000,
            timers=timers.timers,
            last_activity_ms=lambda: activity,
            paused=lambda: paused,
            nudge=nudge,
            kill=lambda: kills.append(True),
        )
    )
    timers.advance(6000)
    activity = timers.clock
    timers.advance(4000)
    assert nudges == []
    timers.advance(6000)
    assert nudges == [10000] and kills == []
    timers.advance(5000)
    activity = timers.clock
    timers.advance(5000)
    assert kills == []
    paused = True
    timers.advance(30000)
    assert len(nudges) == 1 and kills == []
    paused = False
    timers.advance(10000)
    assert len(nudges) == 2
    timers.advance(10000)
    assert kills == [True] and timers.pending() == 0
    watch.stop()
    start_stale_watch(
        dict(
            stale_ms=5000,
            timers=timers.timers,
            last_activity_ms=lambda: timers.clock - 5000,
            nudge=lambda idle: False,
            kill=lambda: kills.append(True),
        )
    )
    timers.advance(5000)
    assert len(kills) == 2
    watch = start_stale_watch(
        dict(
            stale_ms=1,
            timers=timers.timers,
            last_activity_ms=lambda: 0,
            nudge=lambda idle: False,
            kill=lambda: kills.append(True),
        )
    )
    watch.stop()
    timers.advance(10)
    assert len(kills) == 2 and timers.pending() == 0
    assert (
        stale_nudge_text(600000)
        == "You have been idle for 10 minutes. Finish with your structured result now."
    )
    assert "1 minute." in stale_nudge_text(20000)


def test_reports_decisions_context_and_checkpoint(tmp_path: Path) -> None:
    assert clip_report_text("  a\n b  c ") == "a"
    assert len(clip_report_text("x" * 300)) == 200
    assert clip_report_data({"a": 1}) == {"a": 1}
    assert clip_report_data({"a": "y" * 1100}) is None
    assert clip_report_data("not an object") is None
    cyclic: dict[str, Any] = {}
    cyclic["cycle"] = cyclic
    assert clip_report_data(cyclic) is None
    decisions = {"Base branch?": "main", "scope": "backend only"}
    for question, options, expected in [
        ("base branch?", ["main", "dev"], "main"),
        ("What scope do we keep?", None, "backend only"),
        ("Pick one", ["x", "backend only"], "backend only"),
        ("Pick one", ["x", "y"], "x"),
        ("Anything?", None, None),
    ]:
        assert answer_from_decisions(question, options, decisions) == expected
    assert answer_from_decisions("Anything?", []) is None
    state = dict(
        context=dict(guidance="g", ticket=[dict(ref="T-1", body="body")], links=["u"]),
        inputs=dict(topic="t", only_input="input"),
        outputs=dict(plan="the plan", topic="output wins"),
        steps=dict(a=dict(outputs=dict(plan="the plan", n=2)), b=dict(status="pending")),
    )
    for key, context_expected in {
        "guidance": "g",
        "ticket.0.body": "body",
        "links": ["u"],
        "plan": "the plan",
        "topic": "output wins",
        "only_input": "input",
        "a": {"plan": "the plan", "n": 2},
        "a.n": 2,
        "b": None,
        "zzz": None,
        "": None,
    }.items():
        assert resolve_context_key(state, key) == context_expected
    path = Path(write_checkpoint(tmp_path, "a", {"partial": True}))
    assert path == tmp_path / "checkpoints" / "a.json"
    assert json.loads(path.read_text()) == {"partial": True}
    write_checkpoint(tmp_path, "a", None)
    assert path.read_text() == "null\n" and not Path(str(path) + ".tmp").exists()
