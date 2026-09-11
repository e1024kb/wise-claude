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
  compile-check <workflow>...  validate definitions; exit 1 on any error
  migrate <workflow.yaml> [--write] [--out <path>]
                               rewrite a v1 workflow as v2; dry run unless --write or --out
  list-defs                    bundled and user workflow definitions
  models [harness...] [--text] model catalog per harness
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


async def main(argv: Sequence[str], io: Io | None = None) -> int:
    io = io or Io()
    parsed = parse_args(argv)
    try:
        command = parsed["cmd"]
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
