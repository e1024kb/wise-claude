from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapter_types import Adapter, Json
from .adapters import adapter_for, has_adapter
from .adapters._common import dumps
from .constants import EFFORTS, HARNESSES, RUN_MODES
from .models import catalog_for, catalog_model, default_model
from .steps.agent import headline


@dataclass
class DispatchIo:
    out: Callable[[str], Any]
    err: Callable[[str], Any]


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def cmd_models(positional: list[str], flags: Json, io: DispatchIo) -> int:
    rows = []
    for name in positional or HARNESSES:
        if name not in HARNESSES:
            io.err(f"models: unknown harness {name} (one of {', '.join(HARNESSES)})\n")
            return 2
        rows += [dict(harness=name, **model) for model in catalog_for(name)]
    if flags.get("text") is True:
        for row in rows:
            io.out(
                f"{row['harness']}\t{row['id']}\t{row['label']}\t{','.join(row['efforts']) or '-'}\t{row['description']}\n"
            )
    else:
        io.out(dumps(rows) + "\n")
    return 0


async def cmd_dispatch(
    flags: Json, io: DispatchIo, adapters: Callable[[str], Adapter] = adapter_for
) -> int:
    harness = _string(flags.get("harness"))
    if harness is None or harness not in HARNESSES:
        io.err(f"dispatch: --harness must be one of {', '.join(HARNESSES)}\n")
        return 64
    if not has_adapter(harness):
        io.err(f"dispatch: no adapter for harness {harness}\n")
        return 64
    prompt_file, prompt_inline = _string(flags.get("prompt-file")), _string(flags.get("prompt"))
    if prompt_file is None and prompt_inline is None:
        io.err("dispatch: --prompt-file <path> (or --prompt <text>) is required\n")
        return 64
    try:
        prompt = (
            prompt_inline
            if prompt_inline is not None
            else Path(prompt_file or "").read_text(encoding="utf-8")
        )
    except OSError as error:
        io.err(f"dispatch: cannot read prompt file: {error}\n")
        return 66
    if not prompt.strip():
        io.err("dispatch: prompt is empty\n")
        return 64
    warnings = []
    model_flag = _string(flags.get("model"))
    in_catalog = catalog_model(harness, model_flag)
    model = (
        (in_catalog["id"] if in_catalog else model_flag)
        if model_flag is not None
        else default_model(harness)["id"]
    )
    if model_flag is not None and in_catalog is None:
        warnings.append(f"model {model_flag} is not in the catalog; passed through as typed")
    effort = _string(flags.get("effort"))
    if effort is not None:
        if effort not in EFFORTS:
            io.err(f"dispatch: --effort must be one of {', '.join(EFFORTS)}\n")
            return 64
        known = in_catalog or (default_model(harness) if model_flag is None else None)
        if known and effort not in known["efforts"]:
            io.err(
                f"dispatch: model {known['id']} takes {', '.join(known['efforts']) or 'no effort flag'}, not {effort}\n"
            )
            return 64
    mode = _string(flags.get("mode"))
    if mode is None:
        mode = "auto"
    if mode not in RUN_MODES:
        io.err(f"dispatch: --mode must be one of {', '.join(RUN_MODES)}\n")
        return 64
    timeout_s = _string(flags.get("timeout-s"))
    try:
        timeout_ms = (float(timeout_s.strip() or "0") if timeout_s is not None else 3600) * 1000
    except ValueError:
        timeout_ms = math.nan
    if not math.isfinite(timeout_ms) or timeout_ms <= 0:
        io.err("dispatch: --timeout-s must be a positive number\n")
        return 64
    req = dict(
        prompt=prompt,
        model=model,
        cwd=_string(flags.get("cwd")) if isinstance(flags.get("cwd"), str) else os.getcwd(),
        mode=mode,
        timeout_ms=timeout_ms,
        auth="subscription",
    )
    if effort is not None:
        req["effort"] = effort
    if isinstance(flags.get("add-dir"), str):
        req["add_dirs"] = [flags["add-dir"]]
    if isinstance(flags.get("allowed-tools"), str):
        req["allowed_tools"] = [tool for tool in flags["allowed-tools"].split(",") if tool]
    res = await adapters(harness).run(req, lambda event: None)
    result = dict(
        ok=res["exit"] == "ok",
        exit=res["exit"],
        harness=harness,
        model=res.get("model", model),
        effort=effort,
        mode=mode,
        verdict=headline(res["text"]),
        text=res["text"],
        usage=res["usage"],
        warnings=[*warnings, *res.get("warnings", [])],
    )
    if "error" in res:
        result["error"] = res["error"]
    if flags.get("text") is True:
        io.out(res["text"] if not res["text"] or res["text"].endswith("\n") else res["text"] + "\n")
        if not result["ok"]:
            io.err(
                f"dispatch: child exit {res['exit']}{': ' + res['error'] if res.get('error') else ''}\n"
            )
    else:
        io.out(dumps(result) + "\n")
    return 0 if result["ok"] else 1
