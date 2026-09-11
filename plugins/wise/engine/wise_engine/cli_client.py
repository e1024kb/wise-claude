from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .client import Client, ConnectError, connect, ensure_daemon
from .preflight import fill_answers as fill_answers
from .protocol import WAIT_DEFAULT_MS, WAIT_MAX_MS
from .render import _json
from .rpc import RpcError, domain_code
from .scheduler import JS_WHITESPACE, UNDEFINED, js_string

Json = dict[str, Any]
CLIENT_COMMANDS = ("run", "status", "answer", "cancel", "resume", "report", "wait", "nudge")
CLIENT_USAGE = """wise-engine <command> [options]

Commands:
  run <workflow> [--cwd <dir>] [--answers <json>] [--context <json>] [--input name=value ...]
                 [--interactive] [--follow] [--timeout-ms <n>]
                              preflight, start a run; --interactive asks every question in the TUI,
                              otherwise defaults are filled for scripts; --follow answers gates
                              from stdin (approve|reject, an option value, or text)
  wait <run_id> [--after <seq>] [--timeout-ms <n>]
                              one wait call: events past <seq>, gate, status, done
  status [run_id]             one run or every run
  nudge <run_id> <step> <message>
  answer <run_id> <gate_id> <value>
  cancel <run_id> [--reason <text>]
  resume <run_id>
  report <run_id>

Options: --json (default) | --text   --data-root <dir>   --socket <path>   --no-start
Exit codes: 0 ok, 1 error or run failed/cancelled, 2 not found, 64 usage, 69 daemon unavailable
"""
BOOLEAN_FLAGS = frozenset(("text", "json", "interactive", "follow", "no-start"))


def parse_args(argv: list[str]) -> Json:
    flags: Json = {}
    positional = []
    index = 1
    value: str | bool
    while index < len(argv):
        token = argv[index]
        if not token.startswith("--"):
            positional.append(token)
        else:
            if "=" in token:
                name, value = token[2:].split("=", 1)
            else:
                name = token[2:]
                value = True
                if (
                    name not in BOOLEAN_FLAGS
                    and index + 1 < len(argv)
                    and not argv[index + 1].startswith("--")
                ):
                    index += 1
                    value = argv[index]
            flags.setdefault(name, []).append(value)
        index += 1
    return {"cmd": argv[0] if argv else "help", "positional": positional, "flags": flags}


def str_flag(parsed: Json, name: str) -> str | None:
    return next(
        (value for value in reversed(parsed["flags"].get(name, [])) if isinstance(value, str)), None
    )


def bool_flag(parsed: Json, name: str) -> bool:
    return any(value is True for value in parsed["flags"].get(name, []))


def strings(parsed: Json, name: str) -> list[str]:
    return [value for value in parsed["flags"].get(name, []) if isinstance(value, str)]


class UsageError(Exception):
    pass


def int_flag(parsed: Json, name: str) -> int | None:
    raw = str_flag(parsed, name)
    if raw is None:
        return None
    if not re.fullmatch(r"[0-9]+", raw):
        raise UsageError(f"--{name} must be a non-negative integer")
    return int(raw)


def json_flag(parsed: Json, name: str) -> Json | None:
    raw = str_flag(parsed, name)
    if raw is None:
        return None
    try:
        result = json.loads(
            raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value))
        )
    except ValueError as error:
        raise UsageError(f"--{name} is not JSON: {error}") from error
    if not isinstance(result, dict):
        raise UsageError(f"--{name} must be a JSON object")
    return result


@dataclass
class Out:
    io: Any
    text: bool

    def emit(self, data: Any, render: Callable[[], str]) -> None:
        self.io.out((render() if self.text else _json(data, 2)) + "\n")

    def line(self, data: Any, render: Callable[[], str]) -> None:
        self.io.out((render() if self.text else _json(data)) + "\n")

    def error(self, data: Json, render: Callable[[], str]) -> None:
        if self.text:
            self.io.err(render() + "\n")
        else:
            self.io.out(_json({"error": data}) + "\n")


def clock(ts: str) -> str:
    try:
        date = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.astimezone()
        return date.astimezone(timezone.utc).strftime("%H:%M:%S")
    except ValueError:
        return ts[11:19].ljust(8)


def _fixed(value: float, places: int) -> str:
    return str(
        Decimal.from_float(float(value)).quantize(
            Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP
        )
    )


def tokens(number: float) -> str:
    return f"{_fixed(number / 1000, 1)}k" if number >= 1000 else js_string(number)


def format_event(event: Json) -> str:
    subject = event.get("step", event.get("unit", ""))
    detail = event.get("verdict", event.get("message", ""))
    if not detail and event.get("phase"):
        detail = f"phase {event['phase']}"
    if not detail and event.get("usage"):
        detail = f"in {tokens(event['usage']['input'])} out {tokens(event['usage']['output'])}"
    if not detail and event.get("model"):
        detail = " ".join(event[key] for key in ("harness", "model", "effort") if event.get(key))
    return f"{clock(event['ts'])}  {event['type']:<12}  {subject}{'  ' + detail if detail else ''}".rstrip(
        JS_WHITESPACE
    )


def gate_options(gate: Json) -> list[str]:
    if gate.get("options"):
        return [option["value"] for option in gate["options"]]
    return ["approve", "reject"] if gate["kind"] == "approval" else []


def format_gate(gate: Json) -> str:
    lines = [f"GATE {gate['gate_id']} [{gate['kind']}] step {gate['step']}", f"  {gate['message']}"]
    options = gate_options(gate)
    if options:
        lines.append(f"  options: {', '.join(options)}")
    if gate["kind"] == "ask" and gate.get("allow_text"):
        lines.append("  free text accepted")
    if gate.get("expires_at"):
        lines.append(f"  expires {gate['expires_at']}")
    return "\n".join(lines)


def format_summary(summary: Json) -> str:
    gate = f"  gate {summary['gate']['gate_id']}" if summary.get("gate") else ""
    return f"{summary['run_id']}  {summary['workflow']}  {summary['status']}  {summary['started_at']}  {summary['cwd']}{gate}"


def format_status(result: Any) -> str:
    if isinstance(result, list):
        return "\n".join(format_summary(row) for row in result) if result else "no runs"
    return format_summary(result)


def format_wait(result: Json) -> str:
    lines = [format_event(event) for event in result["events"]]
    if result.get("gate"):
        lines.append(format_gate(result["gate"]))
    lines.append(f"status: {result['status']}{'  done' if result['done'] else ''}")
    return "\n".join(lines)


def money(usage: Json) -> str:
    if "cost_usd" not in usage:
        return "-"
    return (
        ("~" if usage.get("cost_source") == "priced" else "") + "$" + _fixed(usage["cost_usd"], 2)
    )


def table(header: list[str], rows: list[list[str]]) -> list[str]:
    def length(cell: str) -> int:
        return len(cell.encode("utf-16-le", errors="surrogatepass")) // 2

    widths = [
        max(length(cell), *(length(row[index]) for row in rows))
        for index, cell in enumerate(header)
    ]
    return [
        "  ".join(
            cell + " " * (widths[index] - length(cell))
            if index < 2
            else " " * (widths[index] - length(cell)) + cell
            for index, cell in enumerate(row)
        ).rstrip(JS_WHITESPACE)
        for row in [header, *rows]
    ]


def format_report(result: Json) -> str:
    verdicts = result["verdicts"]
    lines = ["verdicts:" if verdicts else "verdicts: none"]
    lines += [f"  {step}: {verdict}" for step, verdict in verdicts.items()]
    units = result["units"]
    lines.append(f"units ({len(units)}):" if units else "units: none")
    for unit in units:
        reason = f"  {unit['reason']}" if unit.get("reason") else ""
        lines.append(
            f"  {unit['unit']['ref']}  {unit.get('verdict') or '-'}  {unit['unit']['branch']}{reason}"
        )

    def row(label: str, who: str, usage: Json) -> list[str]:
        return [
            label,
            who,
            tokens(usage["input"]),
            tokens(usage["output"]),
            tokens(usage["cache_read"]),
            money(usage),
        ]

    steps = result["usage"].get("by_step", {})
    lines.append("usage:" if steps else "usage: none")
    if steps:
        rows = []
        for step, usage in steps.items():
            resolved = result["resolved"].get(step)
            who = (
                f"{resolved['harness']} {resolved['model']}"
                if resolved
                else "units"
                if any(key.startswith(step + ".") for key in result["resolved"])
                else "-"
            )
            rows.append(row(step, who, usage))
        lines += [
            "  " + line
            for line in table(["step", "harness model", "in", "out", "cache_read", "cost"], rows)
        ]
    totals = []
    for pool in ("subscription", "api-key"):
        usage = result["usage"].get(pool)
        if usage and (
            any(usage.get(key) for key in ("input", "output", "cache_read", "cache_write"))
            or "cost_usd" in usage
        ):
            totals.append(row("pool", pool, usage))
    totals += [
        row("harness", harness, usage)
        for harness, usage in result["usage"]["by_harness"].items()
        if usage
    ]
    if result.get("usage_total"):
        totals.append(row("total", "", result["usage_total"]))
    if totals:
        lines.append("totals:")
        lines += [
            "  " + line for line in table(["by", "", "in", "out", "cache_read", "cost"], totals)
        ]
    return "\n".join(lines)


class LineSource:
    def __init__(self, source: Any) -> None:
        self.source = source
        self._reader: asyncio.StreamReader | None = None
        self._transport: asyncio.ReadTransport | None = None
        self._closed = False
        self._started = False
        self._fd: int | None = None
        self._was_blocking = True

    async def next(self) -> str | None:
        if self._closed:
            return None
        if not self._started:
            self._started = True
            try:
                fd = self.source.fileno()
            except (AttributeError, OSError):
                fd = None
            if fd is not None and not stat.S_ISREG(os.fstat(fd).st_mode):
                self._fd = fd
                self._was_blocking = os.get_blocking(fd)
                reader = asyncio.StreamReader()
                protocol = asyncio.StreamReaderProtocol(reader)
                stream = os.fdopen(os.dup(fd), "rb", buffering=0)
                try:
                    self._transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                        lambda: protocol, stream
                    )
                except BaseException:
                    stream.close()
                    raise
                self._reader = reader
        value = self._reader.readline() if self._reader else self.source.readline()
        if inspect.isawaitable(value):
            value = await value
        if not value:
            return None
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        return str(value).removesuffix("\n").removesuffix("\r")

    def close(self) -> None:
        self._closed = True
        if self._transport:
            self._transport.close()
        if self._fd is not None:
            try:
                os.set_blocking(self._fd, self._was_blocking)
            except OSError:
                pass
            self._fd = None


def option_value(question: Json, raw: str) -> str | None:
    options = question.get("options", [])
    try:
        number = float(raw)
        if number.is_integer() and 1 <= number <= len(options):
            return options[int(number) - 1]["value"]
    except ValueError:
        pass
    normalized = raw.lower()
    return next(
        (
            option["value"]
            for option in options
            if option["value"].lower() == normalized or option["label"].lower() == normalized
        ),
        None,
    )


async def read_question(question: Json, stdin: LineSource, io: Any) -> Any:
    options = question.get("options", [])
    while True:
        io.err(f"\n{question['label']}\n")
        for index, option in enumerate(options):
            note = f" - {option['description']}" if option.get("description") else ""
            io.err(f"  {index + 1}. {option['label']}{note}\n")
        fallback = question.get("default", UNDEFINED)
        hint = ",".join(fallback) if isinstance(fallback, list) else js_string(fallback)
        io.err("> " if fallback is UNDEFINED else f"> [{hint}] ")
        line = await stdin.next()
        if line is None:
            return None
        raw = line.strip(JS_WHITESPACE)
        if raw == "" and fallback is not UNDEFINED:
            return fallback
        if question["kind"] == "text":
            if raw or question.get("optional"):
                return raw
            io.err("A value is required.\n")
        elif question["kind"] == "choice":
            value = option_value(question, raw)
            if value is not None:
                return value
            io.err(f"Choose 1-{len(options)}, an option label, or an option value.\n")
        else:
            if raw.lower() == "none":
                return []
            parts = raw.split(",")
            selected = [
                value
                for part in parts
                if (value := option_value(question, part.strip(JS_WHITESPACE))) is not None
            ]
            if selected and len(selected) == len(parts):
                return list(dict.fromkeys(selected))
            io.err(
                "Choose comma-separated option numbers, labels, or values; use 'none' for no selection.\n"
            )


async def collect_interactive_answers(
    client: Client, workflow: str, cwd: str, given: Json, stdin: LineSource, io: Any
) -> Json | None:
    answers = dict(given)
    for _ in range(256):
        pre = await client.call("preflight", {"workflow": workflow, "cwd": cwd, "answers": answers})
        question = next(
            (
                item
                for item in pre["questions"]
                if not item.get("locked") and item["id"] not in answers
            ),
            None,
        )
        if question is None or pre["requires_missing"]:
            return {"pre": pre, "answers": answers}
        answer = await read_question(question, stdin, io)
        if answer is None:
            return None
        answers[question["id"]] = answer
    raise UsageError("run: interactive preflight exceeded its question limit")


def client_options(parsed: Json, io: Any) -> Json:
    options = {
        "env": getattr(io, "env", None) if getattr(io, "env", None) is not None else os.environ,
        "client": "wise-engine-cli",
    }
    for flag, name in (("data-root", "data_root"), ("socket", "socket_path")):
        value = str_flag(parsed, flag)
        if value:
            options[name] = value
    return options


async def open_client(parsed: Json, io: Any) -> Client:
    options = client_options(parsed, io)
    return await (connect(**options) if bool_flag(parsed, "no-start") else ensure_daemon(**options))


def require_arg(parsed: Json, index: int, name: str) -> str:
    if index >= len(parsed["positional"]) or not parsed["positional"][index]:
        raise UsageError(f"{parsed['cmd']}: missing <{name}>")
    return parsed["positional"][index]


async def read_gate_answer(gate: Json, out: Out, stdin: LineSource) -> str | None:
    options = gate_options(gate)
    free_text = gate["kind"] == "ask" and (gate.get("allow_text") is True or not options)
    while True:
        if out.text:
            out.line(None, lambda: "> ")
        raw = await stdin.next()
        if raw is None:
            return None
        value = raw.strip(JS_WHITESPACE)
        if not value:
            continue
        if free_text or value in options:
            return value
        out.line(
            {"warn": "invalid answer", "value": value, "options": options},
            lambda: f"expected one of: {', '.join(options)}",
        )


async def follow(client: Client, run_id: str, timeout_ms: int, out: Out, stdin: LineSource) -> int:
    after = 0
    answered = set()
    while True:
        result = await client.call(
            "wait",
            {"run_id": run_id, "after": after, "timeout_ms": timeout_ms},
            timeout_ms=timeout_ms + 15000,
        )
        for event in result["events"]:
            out.line(event, lambda: format_event(event))
            after = max(after, event["seq"])
        if result["done"]:
            out.line(
                {"done": True, "run_id": run_id, "status": result["status"]},
                lambda: f"run {result['status']}",
            )
            return 0 if result["status"] == "completed" else 1
        gate = result.get("gate")
        if gate and gate["gate_id"] not in answered:
            out.line({"gate": gate}, lambda: format_gate(gate))
            value = await read_gate_answer(gate, out, stdin)
            if value is None:
                out.error(
                    {"code": "GATE_UNANSWERED", "run_id": run_id, "gate_id": gate["gate_id"]},
                    lambda: f"stdin closed before gate {gate['gate_id']} was answered; run stays gated.\nanswer later with: wise-engine answer {run_id} {gate['gate_id']} <value>",
                )
                return 1
            ack = await client.call(
                "answer", {"run_id": run_id, "gate_id": gate["gate_id"], "value": value}
            )
            answered.add(gate["gate_id"])
            out.line(
                {"answered": gate["gate_id"], "value": value, "accepted": ack["accepted"]},
                lambda: f"answered {gate['gate_id']}: {value}"
                if ack["accepted"]
                else f"answer to {gate['gate_id']} not accepted",
            )


async def cmd_run(parsed: Json, io: Any, out: Out) -> int:
    workflow = require_arg(parsed, 0, "workflow")
    cwd = str_flag(parsed, "cwd")
    cwd = os.getcwd() if cwd is None else cwd
    given = json_flag(parsed, "answers") or {}
    context = json_flag(parsed, "context") or {}
    for pair in strings(parsed, "input"):
        name, separator, value = pair.partition("=")
        if not separator or not name:
            raise UsageError(f"--input expects name=value, got '{pair}'")
        given["input." + name] = value
    timeout = int_flag(parsed, "timeout-ms")
    timeout = min(WAIT_DEFAULT_MS if timeout is None else timeout, WAIT_MAX_MS)
    client = await open_client(parsed, io)
    stdin = LineSource(getattr(io, "stdin", None) or sys.stdin)
    try:
        if bool_flag(parsed, "interactive"):
            collected = await collect_interactive_answers(client, workflow, cwd, given, stdin, io)
            if collected is None:
                out.error(
                    {"code": "PREFLIGHT_UNANSWERED", "workflow": workflow},
                    lambda: "stdin closed before interactive preflight was complete; no run started",
                )
                return 64
            pre, answers = collected["pre"], collected["answers"]
            inputs = {
                key[6:]: value
                for key, value in answers.items()
                if key.startswith("input.") and isinstance(value, str)
            }
        else:
            pre = await client.call(
                "preflight", {"workflow": workflow, "cwd": cwd, "answers": given}
            )
            filled = fill_answers(pre["questions"], given)
            known = len(given)
            for _ in range(32):
                if filled["missing"] or len(filled["answers"]) == known:
                    break
                known = len(filled["answers"])
                pre = await client.call(
                    "preflight", {"workflow": workflow, "cwd": cwd, "answers": filled["answers"]}
                )
                filled = fill_answers(pre["questions"], filled["answers"])
            if filled["missing"]:
                questions = [q for q in pre["questions"] if q["id"] in filled["missing"]]

                def missing_text() -> str:
                    return "\n".join(
                        [
                            f"run: missing required answers (no interactive prompting in this version): {', '.join(filled['missing'])}",
                            *[f"  {q['id']} [{q['kind']}] {q['label']}" for q in questions],
                            '  pass them with --input <name>=<value> or --answers \'{"<id>":"<value>"}\'',
                        ]
                    )

                out.error(
                    {
                        "code": "MISSING_ANSWERS",
                        "workflow": pre["workflow"],
                        "missing": filled["missing"],
                        "questions": questions,
                    },
                    missing_text,
                )
                return 64
            answers, inputs = filled["answers"], filled["inputs"]
        started = await client.call(
            "run",
            {
                "workflow": workflow,
                "cwd": cwd,
                "answers": answers,
                "context": context,
                "inputs": inputs,
            },
        )
        record = {**started, "workflow": pre["workflow"], "answers": answers}

        def render() -> str:
            return f"run {started['run_id']} started ({pre['workflow']})"

        if bool_flag(parsed, "follow"):
            out.line(record, render)
            return await follow(client, started["run_id"], timeout, out, stdin)
        out.emit(record, render)
        return 0
    finally:
        stdin.close()
        client.close()


async def cmd_wait(parsed: Json, io: Any, out: Out) -> int:
    run_id = require_arg(parsed, 0, "run_id")
    after = int_flag(parsed, "after")
    timeout = int_flag(parsed, "timeout-ms")
    timeout = min(WAIT_DEFAULT_MS if timeout is None else timeout, WAIT_MAX_MS)
    params: Json = {"run_id": run_id, "timeout_ms": timeout}
    if after is not None:
        params["after"] = after
    client = await open_client(parsed, io)
    try:
        result = await client.call("wait", params, timeout_ms=timeout + 15000)
        out.emit(result, lambda: format_wait(result))
        return 0
    finally:
        client.close()


async def cmd_direct(parsed: Json, io: Any, out: Out) -> int:
    client = await open_client(parsed, io)
    try:
        command = parsed["cmd"]
        if command == "status":
            run_id = parsed["positional"][0] if parsed["positional"] else None
            result = await client.call("status", {"run_id": run_id} if run_id else {})
            out.emit(result, lambda: format_status(result))
            return 0
        run_id = require_arg(parsed, 0, "run_id")
        if command == "answer":
            gate_id = require_arg(parsed, 1, "gate_id")
            result = await client.call(
                "answer",
                {"run_id": run_id, "gate_id": gate_id, "value": require_arg(parsed, 2, "value")},
            )
            out.emit(
                result,
                lambda: f"answer accepted for {gate_id}"
                if result["accepted"]
                else "answer not accepted",
            )
            return 0 if result["accepted"] else 1
        if command == "cancel":
            params = {"run_id": run_id}
            reason = str_flag(parsed, "reason")
            if reason is not None:
                params["reason"] = reason
            result = await client.call("cancel", params)
            out.emit(result, lambda: f"run {run_id} {result['status']}")
        elif command == "nudge":
            result = await client.call(
                "nudge",
                {
                    "run_id": run_id,
                    "step": require_arg(parsed, 1, "step"),
                    "message": require_arg(parsed, 2, "message"),
                },
            )
            out.emit(result, lambda: json.dumps(result))
        elif command == "resume":
            result = await client.call("resume", {"run_id": run_id})
            out.emit(result, lambda: f"run {result['run_id']} {result['status']}")
        elif command == "report":
            result = await client.call("report", {"run_id": run_id})
            out.emit(result, lambda: format_report(result))
        else:
            raise UsageError(f"unknown command '{command}'")
        return 0
    finally:
        client.close()


def report_error(error: Exception, out: Out, io: Any) -> int:
    if isinstance(error, UsageError):
        io.err(f"wise-engine {error}\n\n{CLIENT_USAGE}")
        return 64
    if isinstance(error, ConnectError):
        out.error(
            {"code": error.code, "message": str(error), "hint": "run /wise-init"},
            lambda: f"ERROR {error.code}: {error}\nrun /wise-init to set up the engine and its daemon",
        )
        return 69
    code = domain_code(error)
    if code is not None and isinstance(error, RpcError):
        data = error.data

        def render() -> str:
            lines = [f"ERROR {code}: {error}"]
            if code == "AUTH_REQUIRED" and isinstance(data.get("login_cmd"), str):
                lines.append(data["login_cmd"])
            return "\n".join(lines)

        out.error({**data, "code": code, "message": str(error)}, render)
        return 2 if code in ("WORKFLOW_NOT_FOUND", "RUN_NOT_FOUND") else 1
    if isinstance(error, RpcError):
        out.error(
            {"code": "RPC_ERROR", "rpc_code": error.code, "message": str(error)},
            lambda: f"ERROR RPC_ERROR ({error.code}): {error}",
        )
    else:
        out.error({"code": "INTERNAL", "message": str(error)}, lambda: f"ERROR INTERNAL: {error}")
    return 70


async def client_command(argv: list[str], io: Any) -> int:
    parsed = parse_args(argv)
    out = Out(io, bool_flag(parsed, "text"))
    if (
        bool_flag(parsed, "help")
        or bool_flag(parsed, "h")
        or parsed["cmd"] in ("help", "--help", "-h")
    ):
        io.out(CLIENT_USAGE)
        return 0
    try:
        if parsed["cmd"] == "run":
            return await cmd_run(parsed, io, out)
        if parsed["cmd"] == "wait":
            return await cmd_wait(parsed, io, out)
        if parsed["cmd"] in CLIENT_COMMANDS:
            return await cmd_direct(parsed, io, out)
        raise UsageError(f"unknown command '{parsed['cmd']}'")
    except Exception as error:
        return report_error(error, out, io)
