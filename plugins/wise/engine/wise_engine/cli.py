from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .constants import HARNESSES
from .defs import default_roots, list_defs, load_and_validate, locate_def
from .models import catalog_for
from .version import runtime_version, source_build_id

USAGE = """wise-engine <command> [options]

Commands:
  preflight <workflow> [--answers <json>] [--context <json>]
                              questionary spec: {workflow, version, questions, defaults}; --answers
                              gives the answers so far and returns the next stage
  compile-check <workflow>...  validate definitions; exit 1 on any error
  migrate <workflow.yaml> [--write] [--out <path>]
                               rewrite a v1 workflow as v2; dry run unless --write (in place,
                               original kept as <file>.v1.bak) or --out; exit 1 if the result
                               still has validation errors
  list-defs                    bundled and user workflow definitions
  definition-roots             canonical user and bundled definition directories
  list-agents                  bundled role roster for workflow authors
  run <workflow> [--cwd <dir>] [--answers <json>] [--context <json>] [--input k=v] [--follow]
                               start a run through the daemon (auto-started)
  wait|status|answer|cancel|resume|report|nudge ...
                               daemon client commands; see each command's --help
  daemon serve|start|stop|status
                               background daemon wise-engined
  mcp [--no-start]             stdio MCP server (thin daemon client; used by managed host registration)
  unit-mcp [--token <t>]       child-side stdio MCP server (wise_report/ask/context/checkpoint);
                               token and socket from WISE_STEP_TOKEN / WISE_ENGINE_SOCKET / WISE_DATA_ROOT
  auth [harness...] [--json]   which harness CLIs are installed and logged in (subscription probe);
                               exit 1 when a checked provider is missing or logged out
  models [harness...] [--text] model catalog per harness: id, label, efforts (JSON by default)
  dispatch --harness <h> --prompt-file <path> [--model <id>] [--effort <e>]
           [--mode approval-required|auto|full-access] [--cwd <dir>] [--timeout-s <n>]
           [--add-dir <dir>] [--allowed-tools <a,b>] [--text]
                               one child run on any harness, no daemon or ledger; prints one
                               JSON result (or the child's text under --text); exit 1 on a
                               failed child
  setup-host --host <host> --plugin-root <path> [--apply]  preview or repair registration
  refresh-host --host <host> --plugin-root <path>  refresh an existing registration
  host-doctor --host <host>    inspect launch registration (does not prove host connectivity)
  host-rollback <transaction>  restore setup files if they have not changed
  version                      plugin version and runtime
  help                         this text

<workflow> is a definition name (user root shadows bundled) or a path to a .yaml file.
Options: --json (default) | --text   --user-root <dir>   --bundled-root <dir>
"""
Json = dict[str, Any]


@dataclass
class Io:
    out: Callable[[str], Any] = sys.stdout.write
    err: Callable[[str], Any] = sys.stderr.write
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)


def parse_args(argv: Sequence[str]) -> Json:
    cmd = argv[0] if argv else "help"
    positional = []
    flags: Json = {}
    index = 1
    while index < len(argv):
        token = argv[index]
        if token.startswith("--"):
            if "=" in token:
                key, value = token[2:].split("=", 1)
                flags[key] = value
            elif index + 1 < len(argv) and not argv[index + 1].startswith("--"):
                flags[token[2:]] = argv[index + 1]
                index += 1
            else:
                flags[token[2:]] = True
        else:
            positional.append(token)
        index += 1
    return dict(cmd=cmd, positional=positional, flags=flags)


def flag_string(flags: Json, name: str) -> str | None:
    value = flags.get(name)
    return value if isinstance(value, str) else None


def roots_from(parsed: Json, io: Io) -> dict[str, str]:
    roots = default_roots(env=io.env)
    for key in ("user-root", "bundled-root"):
        value = flag_string(parsed["flags"], key)
        if value:
            roots[key.replace("-", "_")] = value
    return roots


def locate(ref: str, parsed: Json, io: Io) -> Json | None:
    if ref.endswith((".yaml", ".yml")) or "/" in ref:
        path = Path(ref).absolute()
        if not path.is_file():
            return None
        name = path.parent.name if path.name == "workflow.yaml" else path.stem
        return dict(name=name, path=str(path), dir=str(path.parent), source="user")
    return locate_def(ref, roots_from(parsed, io))


def format_issue(issue: Json) -> str:
    hint = f"\n    -> {issue['hint']}" if issue.get("hint") else ""
    return f"  {issue['level'].upper()} {issue['path']}: {issue['message']}{hint}"


def emit(io: Io, parsed: Json, data: Any, text: Callable[[], str]) -> None:
    io.out(
        (
            text()
            if parsed["flags"].get("text") is True
            else json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
        )
        + "\n"
    )


def cmd_compile_check(parsed: Json, io: Io) -> int:
    if not parsed["positional"]:
        io.err("compile-check: missing <workflow>\n")
        return 64
    report = []
    for ref in parsed["positional"]:
        located = locate(ref, parsed, io)
        if located is None:
            report.append(
                dict(
                    workflow=ref,
                    path=None,
                    ok=False,
                    issues=[dict(level="error", path="", message="workflow not found")],
                )
            )
            continue
        validated = load_and_validate(located)
        report.append(
            dict(
                workflow=located["name"],
                path=located["path"],
                ok="def" in validated,
                issues=validated["issues"],
            )
        )
    emit(
        io,
        parsed,
        report,
        lambda: "\n".join(
            "\n".join(
                [
                    f"{'OK' if r['ok'] else 'FAIL'} {r['workflow']}"
                    + (f" ({r['path']})" if r["path"] else ""),
                    *[format_issue(i) for i in r["issues"]],
                ]
            )
            for r in report
        ),
    )
    return 0 if all(r["ok"] for r in report) else 1


def cmd_models(parsed: Json, io: Io) -> int:
    rows: list[Json] = []
    for name in parsed["positional"] or HARNESSES:
        if name not in HARNESSES:
            io.err(f"models: unknown harness {name} (one of {', '.join(HARNESSES)})\n")
            return 2
        rows.extend({"harness": name, **model} for model in catalog_for(name))
    if parsed["flags"].get("text") is True:
        for r in rows:
            io.out(
                f"{r['harness']}\t{r['id']}\t{r['label']}\t{','.join(r['efforts']) or '-'}\t{r['description']}\n"
            )
    else:
        io.out(json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


def cmd_migrate(parsed: Json, io: Io) -> int:
    from .migrate import format_migration, migrate_file

    if not parsed["positional"]:
        io.err("migrate: missing <workflow.yaml>\n")
        return 64
    ref = parsed["positional"][0]
    located = locate(ref, parsed, io)
    if located is None:
        io.err(f"migrate: workflow not found: {ref}\n")
        return 2
    result = migrate_file(
        located["path"],
        write=parsed["flags"].get("write") is True,
        out=flag_string(parsed["flags"], "out"),
        workflow=located["name"],
    )
    emit(io, parsed, result, lambda: format_migration(result))
    return 0 if result["ok"] else 1


def adapter_lookup(harness: str) -> Any:
    from .adapters import adapter_for, has_adapter

    return adapter_for(harness) if has_adapter(harness) else None


async def cmd_preflight(parsed: Json, io: Io) -> int:
    from .auth import installed_harnesses
    from .preflight import build_questionary_with_auth

    if not parsed["positional"]:
        io.err("preflight: missing <workflow>\n")
        return 64
    ref = parsed["positional"][0]
    located = locate(ref, parsed, io)
    if located is None:
        io.err(f"preflight: workflow not found: {ref}\n")
        emit(
            io,
            parsed,
            {"error": {"code": "WORKFLOW_NOT_FOUND", "workflow": ref}},
            lambda: "not found",
        )
        return 2
    validation = load_and_validate(located)
    definition, issues = validation.get("def"), validation["issues"]
    if definition is None:
        emit(
            io,
            parsed,
            {"error": {"code": "WORKFLOW_INVALID", "workflow": located["name"], "issues": issues}},
            lambda: "\n".join([f"{located['path']}: invalid", *map(format_issue, issues)]),
        )
        return 1
    answers: Json = {}
    context: Json | None = None
    for name in ("answers", "context"):
        value = flag_string(parsed["flags"], name)
        if value is None:
            continue
        try:
            decoded = json.loads(value)
        except ValueError as error:
            io.err(f"preflight: --{name} is not JSON: {error}\n")
            return 64
        if name == "answers":
            answers = decoded
        else:
            context = decoded
    ctx: Json = {"harnesses": installed_harnesses(definition, adapter_lookup, io.env)}
    if context is not None:
        ctx["context"] = context
    questionary = await build_questionary_with_auth(definition, ctx, answers, adapter_lookup)
    result = {
        "workflow": located["name"],
        "version": definition["version"],
        "questions": questionary["questions"],
        "defaults": questionary["defaults"],
        "warnings": [issue for issue in issues if issue["level"] == "warning"],
    }

    def render() -> str:
        lines = [f"{located['name']} v{definition['version']}"]
        for question in questionary["questions"]:
            default = (
                " (default: "
                + json.dumps(question["default"], ensure_ascii=False, separators=(",", ":"))
                + ")"
                if "default" in question
                else ""
            )
            lines.append(
                f"  {question['id']} [{question['kind']}{', locked' if question.get('locked') else ''}] {question['label']}{default}"
            )
        return "\n".join(lines)

    emit(io, parsed, result, render)
    return 0


async def cmd_auth(parsed: Json, io: Io) -> int:
    from .auth import LOGIN_CMDS, probe_one
    from .defs import on_path

    rows = []
    for harness in parsed["positional"] or HARNESSES:
        if harness not in HARNESSES:
            io.err(f"auth: unknown harness {harness} (one of {', '.join(HARNESSES)})\n")
            return 2
        adapter = adapter_lookup(harness)
        installed = on_path(adapter.bin if adapter is not None else harness, io.env)
        probe = (
            await probe_one(harness, "subscription", adapter_lookup)
            if installed
            else {"ok": False, "login_cmd": LOGIN_CMDS[harness]}
        )
        rows.append(
            {
                "harness": harness,
                "installed": installed,
                "login": "ok" if probe["ok"] else "missing",
                "login_cmd": probe["login_cmd"],
            }
        )
    if parsed["flags"].get("json"):
        io.out(json.dumps(rows, separators=(",", ":")) + "\n")
    else:
        for row in rows:
            io.out(
                f"HARNESS={row['harness']} INSTALLED={'yes' if row['installed'] else 'no'} LOGIN={row['login']} LOGIN_CMD={row['login_cmd']}\n"
            )
    return 1 if any(row["login"] != "ok" for row in rows) else 0


async def main(argv: Sequence[str], io: Io | None = None) -> int:
    io = io or Io()
    parsed = parse_args(argv)
    try:
        command = parsed["cmd"]
        if command == "daemon":
            from .daemon import daemon_command

            return await daemon_command(list(argv[1:]), io)
        if command in ("mcp", "unit-mcp"):
            from .mcp_server import mcp_command, unit_mcp_command

            return await (
                mcp_command(list(argv[1:]), io)
                if command == "mcp"
                else unit_mcp_command(list(argv[1:]), io)
            )
        if command in ("run", "status", "answer", "cancel", "resume", "report", "wait", "nudge"):
            from .cli_client import client_command

            return await client_command(list(argv), io)
        if command == "preflight":
            return await cmd_preflight(parsed, io)
        if command == "auth":
            return await cmd_auth(parsed, io)
        if command == "dispatch":
            from .dispatch import DispatchIo, cmd_dispatch

            return await cmd_dispatch(parsed["flags"], DispatchIo(io.out, io.err))
        if command == "compile-check":
            return cmd_compile_check(parsed, io)
        if command == "list-defs":
            rows = list_defs(roots_from(parsed, io))
            emit(
                io,
                parsed,
                rows,
                lambda: "\n".join(f"{r['name']}\t{r['source']}\t{r['path']}" for r in rows),
            )
            return 0
        if command in ("setup-host", "refresh-host", "host-doctor", "host-rollback"):
            from .host_setup import apply_plan, doctor, plan_setup, refresh_existing, rollback

            flags = parsed["flags"]
            if command == "host-rollback":
                if not parsed["positional"]:
                    raise ValueError("host-rollback requires the setup transaction path")
                rollback(parsed["positional"][0])
                io.out('{"rolled_back":true}\n')
                return 0
            host = flag_string(flags, "host")
            if host is None:
                raise ValueError("--host is required (claude, codex, cursor, grok)")
            home = flag_string(flags, "home") or io.env.get("HOME") or str(Path.home())
            config = flag_string(flags, "config")
            if config is None and host == "codex" and io.env.get("CODEX_HOME"):
                config = str(Path(io.env["CODEX_HOME"]) / "config.toml")
            if config is None and host == "claude" and io.env.get("CLAUDE_CONFIG_DIR"):
                config = str(Path(io.env["CLAUDE_CONFIG_DIR"]) / ".claude.json")
            if command == "host-doctor":
                report = doctor(home=home, host=host, config=config)
                io.out(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                return 0 if report["registration_ok"] and report["launcher_ok"] else 1
            plugin_root = flag_string(flags, "plugin-root")
            if plugin_root is None:
                raise ValueError("--plugin-root must identify the loaded Wise installation")
            if command == "refresh-host":
                result = refresh_existing(
                    plugin_root=plugin_root, host=host, home=home, config=config
                )
                io.out(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                return 0
            source = flag_string(flags, "source")
            plan = plan_setup(
                plugin_root=plugin_root,
                host=host,
                home=home,
                config=config,
                source=json.loads(source) if source else None,
                python=flag_string(flags, "python"),
            )
            result = plan.preview()
            if flags.get("apply") is True:
                transaction = apply_plan(plan)
                result["transaction"] = str(transaction) if transaction else None
                result["applied"] = True
            io.out(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            return 0
        if command == "definition-roots":
            io.out(json.dumps(roots_from(parsed, io), ensure_ascii=False, indent=2) + "\n")
            return 0
        if command == "list-agents":
            from .resolve import cmd_list_agents

            io.out(json.dumps(cmd_list_agents(), ensure_ascii=False, indent=2) + "\n")
            return 0
        if command == "models":
            return cmd_models(parsed, io)
        if command == "migrate":
            return cmd_migrate(parsed, io)
        if command == "version":
            io.out(f"wise-engine {source_build_id()} (python {runtime_version()})\n")
            return 0
        if command in ("help", "--help", "-h"):
            io.out(USAGE)
            return 0
        io.err(f"wise-engine: unknown command '{command}'\n\n{USAGE}")
        return 64
    except Exception as error:
        io.err(f"wise-engine: {error}\n")
        return 70
