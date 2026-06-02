#!/usr/bin/env python3
# wise plugin — workflow subsystem helper
#
# Single entry point with git-style subcommands. The calling SKILL.md
# bodies run this script for every YAML-reading or state-mutating
# operation so the SKILL body never parses YAML itself (same
# separation-of-concerns as engine.sh for routing).
#
# Workflow definitions resolve to one of two layouts under each root
# (`${CLAUDE_PLUGIN_DATA}/workflows/definitions/` for user-authored, and
# `${CLAUDE_PLUGIN_ROOT}/workflows/` for bundled):
#
#   <root>/<name>/workflow.yaml   ← preferred: folder form; artifacts
#                                    (templates/, prompts/, fixtures)
#                                    live alongside and are addressable
#                                    from steps via `{{workflow.dir}}`.
#   <root>/<name>.yaml            ← legacy flat form; still accepted so
#                                    existing user-authored files keep
#                                    working. No artifacts dir.
#
# Folder form wins on same-root collision. User root wins over bundled
# root on cross-root collision (same as before).
#
# Subcommands:
#   locate-def         <name>                           → abs path of def YAML
#   probe-requires     <def-yaml>                       → exit 0 OK / 2 missing
#   new-ulid                                            → new ULID on stdout
#   init-state         <def> <run-dir> <run-id> <json>  → stub state.yaml (status: initializing)
#   start-run          <state> <json>                   → add control_mode+worktree+project+inputs, flip to running
#   runs-root                                           → abs path of the per-workspace runs root (single shell-side seam — callers never hard-code the path)
#   get-preflight      <def-yaml>                       → CONTROL_MODE=, WORKTREE=, RENAME= (KEY=VALUE lines)
#   write-log          <run-dir> <step-id> <step-run-id>  → read stdin, write to <run-dir>/logs/<step-id>.<step-run-id>.log (bypasses the Write-tool permission prompt by going through bash + workflows.py, which already has an allowed-tools grant in every conductor skill)
#   list-inputs        <def-yaml>                       → JSON [{name, prompt, validate?, extract?, optional?}] of declared inputs
#   validate-input     <raw> <extract> <validate>       → cleaned value on stdout; exit 2 on INVALID
#   next-wave          <def-yaml> <state-yaml>          → JSON of runnable
#   update-step        <state> <step-id> key=val...     → mutate one step
#   update-run         <state>            key=val...    → mutate top-level
#   record-output      <state> <name> <value>           → capture named output
#   reset-running      <state>                          → running → pending
#   list-runs          <runs-root>                      → summary lines
#   dump-state         <state>                          → pretty YAML
#   render             <template> <state>               → expand {{project.*}}, {{<output>}}, {{run.dir}}, {{run.id}} (note: does NOT expand {{workflow.dir}} — that's resolved at step-render time in cmd_next_wave, which has access to the def path)
#   current-session-id                                  → UUID of the active Claude Code session, inferred from ~/.claude/projects/<cwd-slug>/
#   session-path       <session-id>                     → path to the .jsonl; exit 2 if stale
#   session-label      <run-id> <workflow-name>         → <run-id>_<first-7-hyphen-tokens>
#   find-runs-by-session <session-id>                   → non-terminal runs in cwd that claim this session
#   list-defs                                           → JSON [{name, description, source}] of bundled + user workflow definitions
#   list-resumable-runs                                 → JSON [{run_id, workflow_name, status, last_activity_at, session_label}] of non-terminal runs in cwd
#   prune-runs                                          → delete oldest terminal runs in cwd so total run count ≤ WISE_RUN_HISTORY_CAP (default 25)

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
    import ulid
except ImportError as exc:
    print(f"workflows.py: {exc}. Run scripts/bootstrap-deps.sh first.", file=sys.stderr)
    sys.exit(1)

HOME = Path.home()
SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
BUNDLED_DEFS = PLUGIN_ROOT / "workflows"
PLUGIN_DATA = Path(os.environ.get("CLAUDE_PLUGIN_DATA") or HOME / ".claude/plugins/data/wise")
USER_DEFS = PLUGIN_DATA / "workflows/definitions"


# ---- persistent per-user data root -----------------------------------------
#
# Single source of truth for "where does wise put its persistent state". This
# lives under ~/.local/share/wise/ (honouring XDG_DATA_HOME) — off the project
# tree, off `.claude/**` (which Claude Code treats as sensitive and prompts
# on), persistent across reboots (unlike /tmp which auto-cleans).
# Any new wise feature that needs to persist something should route through
# `wise_data_root()` — never hard-code paths.

def wise_data_root() -> Path:
    """Return wise's per-user data root.

    `$XDG_DATA_HOME/wise` when set; else `~/.local/share/wise`. XDG Base
    Directory Spec semantics.
    """
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else HOME / ".local" / "share"
    return base / "wise"


def _cwd_slug() -> str:
    """Absolute cwd with `/` → `-`, usable as a namespace directory name.

    Same slug shape Claude Code uses for session discovery (see
    `_cwd_session_dir` below), so the two namespaces line up when we
    cross-reference.
    """
    return str(Path.cwd().resolve()).replace("/", "-")


def wise_runs_root_for_cwd() -> Path:
    """Per-workspace runs root: `<data_root>/runs/<cwd-slug>/`.

    Every site that builds a runs path — Python or shell (via the
    `runs-root` subcommand) — goes through here. When the base moves
    again, only `wise_data_root()` changes.
    """
    return wise_data_root() / "runs" / _cwd_slug()

RESERVED_NAMES = {"list", "create", "run", "resume", "remove", "status"}
STEP_TYPES = {"skill", "prompt", "bash", "approval", "ask", "interactive"}
TERMINAL_STEP = {"completed", "failed", "skipped", "cancelled"}
TERMINAL_RUN = {"completed", "failed", "cancelled"}
RUN_HISTORY_CAP_DEFAULT = 25
TRIGGER_RULES = {
    "all-success",
    "one-success",
    "all-done",
    "none-failed-min-one-success",
}


# ---------- utilities -------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_yaml(path: Path) -> dict:
    with path.open() as fh:
        data = yaml.safe_load(fh)
    return data or {}


def save_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)
    tmp.replace(path)


def parse_kv_args(tokens: list[str]) -> dict:
    out = {}
    for tok in tokens:
        if "=" not in tok:
            raise SystemExit(f"expected key=value, got: {tok}")
        k, v = tok.split("=", 1)
        out[k] = v
    return out


def coerce(value: str):
    if value in ("true", "false"):
        return value == "true"
    if value == "null":
        return None
    try:
        return int(value)
    except ValueError:
        pass
    return value


# ---------- locate-def ------------------------------------------------------

def cmd_locate_def(name: str) -> int:
    if name in RESERVED_NAMES:
        print(f"reserved name: {name}", file=sys.stderr)
        return 2
    # Two layouts are accepted:
    #   <root>/<name>/workflow.yaml   (preferred — artifacts live beside the def)
    #   <root>/<name>.yaml            (legacy flat form)
    # Folder form wins on collision so new bundled workflows supersede any
    # stale flat file left over from a previous install.
    for root in (USER_DEFS, BUNDLED_DEFS):
        folder_candidate = root / name / "workflow.yaml"
        if folder_candidate.is_file():
            print(folder_candidate)
            return 0
        flat_candidate = root / f"{name}.yaml"
        if flat_candidate.is_file():
            print(flat_candidate)
            return 0
    print(f"workflow not found: {name}", file=sys.stderr)
    return 1


# ---------- probe-requires --------------------------------------------------

def installed_plugins() -> set[str]:
    """Return bare names of plugins Claude Code reports as installed.

    The authoritative source is `~/.claude/plugins/installed_plugins.json`,
    which Claude Code maintains — its top-level `plugins` dict is keyed
    by `<name>@<marketplace>`. We return the bare `<name>` so workflow
    `requires: [{plugin: <name>}]` entries can match without the caller
    having to know the marketplace.

    Falls back to a filesystem walk of `~/.claude/plugins/` for layouts
    where the JSON registry is absent (dev installs, older Claude
    versions). The walk recurses deep enough to handle marketplace
    installs at `cache/<marketplace>/<plugin>[/<version>]/`.
    """
    root = HOME / ".claude/plugins"
    if not root.is_dir():
        return set()

    names: set[str] = set()

    # Primary: read installed_plugins.json.
    registry = root / "installed_plugins.json"
    if registry.is_file():
        try:
            with registry.open() as fh:
                data = json.load(fh)
            for key in (data.get("plugins") or {}).keys():
                # Keys are "<name>@<marketplace>" or bare "<name>".
                bare = key.split("@", 1)[0]
                if bare:
                    names.add(bare)
        except Exception:
            # Malformed JSON — fall through to filesystem walk.
            pass

    if names:
        return names

    # Fallback: walk looking for `.claude-plugin/plugin.json`. Goes up to
    # four levels deep to cover cache/<marketplace>/<plugin>/<version>/.
    def _walk(p: Path, depth: int) -> None:
        if depth > 4 or not p.is_dir():
            return
        pj = p / ".claude-plugin" / "plugin.json"
        if pj.is_file():
            names.add(p.name)
            return
        for child in p.iterdir():
            if child.is_dir():
                _walk(child, depth + 1)

    for child in root.iterdir():
        if child.is_dir():
            _walk(child, 1)
    return names


def cmd_probe_requires(def_path: str) -> int:
    data = load_yaml(Path(def_path))
    requires = data.get("requires") or []
    installed = installed_plugins()
    missing: list[str] = []
    for entry in requires:
        if not isinstance(entry, dict):
            continue
        if "plugin" in entry:
            if entry["plugin"] not in installed:
                missing.append(f"plugin:{entry['plugin']}")
        elif "skill" in entry:
            # skill id is "plugin:skill" — the owning plugin must be installed.
            plugin = entry["skill"].split(":", 1)[0]
            if plugin not in installed:
                missing.append(f"skill:{entry['skill']}")
    if missing:
        for m in missing:
            print(f"MISSING:{m}")
        return 2
    print("OK")
    return 0


# ---------- new-ulid --------------------------------------------------------

def cmd_new_ulid() -> int:
    print(ulid.ULID())
    return 0


# ---------- init-state ------------------------------------------------------

def cmd_init_state(def_path: str, run_dir: str, run_id: str, ctx_json: str) -> int:
    """Phase-A init — write a stub state.yaml with session tag + steps.

    Called before pre-flight prompts so the skill can persist the Claude
    Code session ID and the human-readable session label as early as
    possible. `ctx_json` carries just the session fields the skill has
    at this point:

        {"claude_session_id": "<uuid-or-null>",
         "session_label":      "<run-id>_<slug-or-null>"}

    Control mode, worktree, and project are filled in later by
    `start-run` once pre-flight has answered them.
    """
    ctx = json.loads(ctx_json)
    definition = load_yaml(Path(def_path))
    steps = []
    for step in definition.get("steps") or []:
        steps.append({
            "id": step["id"],
            "status": "pending",
        })
    state = {
        "version": 1,
        "run_id": run_id,
        "workflow_name": definition.get("name"),
        "workflow_version": definition.get("version", 1),
        "workspace": os.getcwd(),
        "claude_session_id": ctx.get("claude_session_id"),
        "session_label": ctx.get("session_label"),
        "started_at": utc_now(),
        "last_activity_at": utc_now(),
        "completed_at": None,
        "status": "initializing",
        "control_mode": None,
        "worktree": None,
        "project": None,
        "outputs": {},
        "steps": steps,
    }
    state_path = Path(run_dir) / "state.yaml"
    (Path(run_dir) / "logs").mkdir(parents=True, exist_ok=True)
    save_yaml(state_path, state)
    print(state_path)
    return 0


def cmd_start_run(state_path: str, ctx_json: str) -> int:
    """Phase-B init — fill in pre-flight answers and flip to running.

    `ctx_json` carries the nested pre-flight fields:

        {"control_mode": "wave-sync" | "synchronous",
         "worktree":     null | {"path": "...", "branch": "...", "created_by_ws": true},
         "project":      null | {"path": "...", "name": "...", "kind": "..."},
         "inputs":       {"<name>": "<value>", ...}}

    `inputs` is optional and merged into `state.outputs` so `{{name}}`
    templates in step definitions resolve the same way captured outputs
    do — inputs are just pre-populated outputs collected during
    pre-flight.
    """
    ctx = json.loads(ctx_json)
    state = load_yaml(Path(state_path))
    state["control_mode"] = ctx.get("control_mode", "wave-sync")
    state["worktree"] = ctx.get("worktree")
    state["project"] = ctx.get("project")
    inputs = ctx.get("inputs") or {}
    if inputs:
        outputs = dict(state.get("outputs") or {})
        outputs.update(inputs)
        state["outputs"] = outputs
    state["status"] = "running"
    state["last_activity_at"] = utc_now()
    save_yaml(Path(state_path), state)
    return 0


# ---------- write-log -------------------------------------------------------


def cmd_write_log(run_dir: str, step_id: str, step_run_id: str) -> int:
    """Write the step's output (from stdin) to the canonical log path.

    Canonical layout: `<run-dir>/logs/<step-id>.<step-run-id>.log`.
    The log directory is created if missing.

    Why this exists: the conductor SKILL body used to use the `Write`
    tool to persist step logs, but Claude Code prompts the user on
    every new file creation via `Write`. Going through
    `workflows.py write-log` + a bash heredoc piped to stdin reuses
    the conductor's existing `Bash(${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py:*)`
    allowed-tools grant, so the log write runs without a per-file
    prompt.
    """
    path = Path(run_dir) / "logs" / f"{step_id}.{step_run_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = sys.stdin.read()
    path.write_text(content)
    print(path)
    return 0


# ---------- runs-root -------------------------------------------------------


def cmd_runs_root() -> int:
    """Print the per-workspace runs root path.

    Shell consumers (SKILL.md bodies) use this so they never hard-code
    the path. Exactly one seam on the Python side (`wise_data_root`) and
    one seam on the shell side (this subcommand) — moving the base
    again is a one-function change.
    """
    print(wise_runs_root_for_cwd())
    return 0


# ---------- get-preflight ---------------------------------------------------

# Valid values for each preflight key. `prompt` is the default — the
# conductor asks the user via AskUserQuestion. Any other listed value
# pins the answer and skips the prompt entirely.
PREFLIGHT_KEYS = {
    "control-mode":   {"prompt", "wave-sync", "synchronous"},
    "worktree":       {"prompt", "current", "new"},
    "rename_session": {"prompt", "skip"},
}


def cmd_get_preflight(def_path: str) -> int:
    """Emit the workflow's preflight pin map as KEY=VALUE lines.

    Always emits all three keys so the conductor can `source` the
    output without branching on presence:

        CONTROL_MODE=<prompt|wave-sync|synchronous>
        WORKTREE=<prompt|current|new>
        RENAME_SESSION=<prompt|skip>

    Missing keys (or a missing `preflight:` block entirely) default to
    `prompt` — current behaviour, zero risk for workflows authored
    before this schema addition.

    Unknown values (typos, wrong enum) default to `prompt` with a
    warning on stderr so the workflow still runs interactively rather
    than failing.
    """
    data = load_yaml(Path(def_path))
    block = data.get("preflight") or {}
    if not isinstance(block, dict):
        print(f"INVALID:preflight-block:expected-mapping", file=sys.stderr)
        block = {}
    out = {}
    for key, allowed in PREFLIGHT_KEYS.items():
        value = block.get(key) or "prompt"
        if value not in allowed:
            print(
                f"WARN:preflight.{key}={value!r} not in {sorted(allowed)}; "
                f"falling back to 'prompt'",
                file=sys.stderr,
            )
            value = "prompt"
        out[key] = value
    # Emit as KEY=VALUE (uppercase, hyphens → underscores) so the
    # conductor can eval/source the output.
    for key, value in out.items():
        var = key.upper().replace("-", "_")
        print(f"{var}={value}")
    return 0


# ---------- list-inputs / validate-input -----------------------------------

def cmd_list_inputs(def_path: str) -> int:
    """Emit the workflow's declared `inputs:` as JSON.

    Each item is `{name, prompt, validate?, extract?, optional?}`.
    Empty list if the workflow declares none. Used by workflow-run's
    pre-flight to know which questions to ask before the DAG launches.
    `optional: true` lets the conductor skip the prompt when no value
    was supplied positionally (the input defaults to empty).
    """
    data = load_yaml(Path(def_path))
    inputs = data.get("inputs") or []
    normalised: list[dict] = []
    seen: set[str] = set()
    for entry in inputs:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not re.match(r"^[a-z][a-z0-9_]*$", name):
            print(f"INVALID:input-name:{name!r}", file=sys.stderr)
            return 2
        if name in seen:
            print(f"INVALID:duplicate-input:{name}", file=sys.stderr)
            return 2
        seen.add(name)
        item = {
            "name": name,
            "prompt": entry.get("prompt") or f"Value for {name}?",
        }
        if entry.get("validate"):
            item["validate"] = entry["validate"]
        if entry.get("extract"):
            item["extract"] = entry["extract"]
        if entry.get("optional"):
            item["optional"] = True
        normalised.append(item)
    print(json.dumps(normalised))
    return 0


def cmd_validate_input(raw: str, extract: str, validate: str) -> int:
    """Extract (optional) + validate (optional) a user-supplied input.

    Arguments are always passed as strings (empty string = no
    regex). Behaviour:

    - If `extract` is non-empty, run it against `raw`. If it has at
      least one capture group, use group(1); otherwise use group(0).
      If it doesn't match at all, exit 2 with `INVALID:no-match`.
    - If `validate` is non-empty, the extracted value (or raw, if
      no extract) must match it fully. Non-match exits 2 with
      `INVALID:validate`.
    - On success, print the cleaned value on stdout and exit 0.
    """
    value = raw
    if extract:
        try:
            m = re.search(extract, raw)
        except re.error as exc:
            print(f"INVALID:bad-extract-regex:{exc}", file=sys.stderr)
            return 2
        if not m:
            print("INVALID:no-match", file=sys.stderr)
            return 2
        value = m.group(1) if m.groups() else m.group(0)
    if validate:
        try:
            m = re.fullmatch(validate, value)
        except re.error as exc:
            print(f"INVALID:bad-validate-regex:{exc}", file=sys.stderr)
            return 2
        if not m:
            print("INVALID:validate", file=sys.stderr)
            return 2
    print(value)
    return 0


# ---------- next-wave -------------------------------------------------------

def _step_by_id(steps: list[dict], sid: str) -> dict | None:
    for s in steps:
        if s.get("id") == sid:
            return s
    return None


def _trigger_rule_satisfied(rule: str, deps: list[dict]) -> tuple[bool, bool]:
    """Return (runnable, should_skip)."""
    if not deps:
        return True, False
    statuses = [d.get("status", "pending") for d in deps]
    terminal = [s for s in statuses if s in TERMINAL_STEP]
    done = [s for s in statuses if s == "completed"]
    failed = [s for s in statuses if s == "failed"]

    if rule == "all-success":
        if all(s == "completed" for s in statuses):
            return True, False
        if any(s in ("failed", "skipped", "cancelled") for s in statuses):
            return False, True  # propagate skip
        return False, False
    if rule == "one-success":
        if any(s == "completed" for s in statuses):
            return True, False
        if len(terminal) == len(statuses):
            return False, True
        return False, False
    if rule == "all-done":
        if len(terminal) == len(statuses):
            return True, False
        return False, False
    if rule == "none-failed-min-one-success":
        if len(terminal) == len(statuses) and not failed and done:
            return True, False
        if failed:
            return False, True
        return False, False
    # unknown rule — treat like all-success
    return _trigger_rule_satisfied("all-success", deps)


def _render_step(step_def: dict, state: dict, workflow_dir: str = "",
                 run_dir: str = "") -> dict:
    """Return a step descriptor with templates expanded.

    `workflow_dir` is the absolute path to the workflow folder when the
    definition is a `<name>/workflow.yaml`, or empty string for legacy
    flat `<name>.yaml` definitions. Exposed to templates as
    `{{workflow.dir}}` so prompts / bash steps can read sibling artifacts
    (e.g. `{{workflow.dir}}/templates/pr-template.md`).

    `run_dir` is the absolute path to this run's directory (the parent of
    `state.yaml`). Exposed as `{{run.dir}}` so steps can write run-scoped
    artifacts beside the state file (e.g. `{{run.dir}}/plans/PLAN-<ref>.md`)
    instead of polluting the project tree. `{{run.id}}` is the run ULID.
    """
    outputs = dict(state.get("outputs") or {})
    project = state.get("project") or {}
    run_id = str(state.get("run_id") or "")

    def render(value):
        if isinstance(value, str):
            out = value
            out = out.replace("{{workflow.dir}}", workflow_dir)
            out = out.replace("{{run.dir}}", run_dir)
            out = out.replace("{{run.id}}", run_id)
            for k, v in project.items():
                out = out.replace("{{project." + k + "}}", str(v))
            for k, v in outputs.items():
                out = out.replace("{{" + k + "}}", str(v))
            return out
        if isinstance(value, list):
            return [render(x) for x in value]
        if isinstance(value, dict):
            return {k: render(v) for k, v in value.items()}
        return value

    return {
        "id": step_def["id"],
        "type": step_def["type"],
        "definition": render(step_def),
    }


def cmd_next_wave(def_path: str, state_path: str) -> int:
    definition = load_yaml(Path(def_path))
    state = load_yaml(Path(state_path))
    step_defs = {s["id"]: s for s in (definition.get("steps") or [])}
    # Folder-form defs resolve to `<root>/<name>/workflow.yaml`; the
    # workflow dir is the parent of that file. Flat-form defs resolve to
    # `<root>/<name>.yaml` and have no workflow dir (empty string).
    def_path_p = Path(def_path)
    workflow_dir = str(def_path_p.parent) if def_path_p.name == "workflow.yaml" else ""
    # The run dir is the parent of state.yaml; exposed to steps as {{run.dir}}.
    run_dir = str(Path(state_path).parent)
    runnable = []
    skipped_ids = []

    for step in state.get("steps") or []:
        if step.get("status") != "pending":
            continue
        sid = step["id"]
        sdef = step_defs.get(sid)
        if not sdef:
            continue
        deps = sdef.get("depends_on") or []
        dep_steps = [_step_by_id(state["steps"], d) for d in deps]
        dep_steps = [d for d in dep_steps if d is not None]
        rule = sdef.get("trigger-rule", "all-success")
        ok, should_skip = _trigger_rule_satisfied(rule, dep_steps)

        # Evaluate `when:` — supports trivial forms only: `name == 'literal'`
        # or `name != 'literal'`. Anything else is treated as truthy.
        when = sdef.get("when")
        when_ok = True
        if when:
            m = re.match(r"\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(==|!=)\s*'([^']*)'\s*", when)
            if m:
                var, op, lit = m.group(1), m.group(2), m.group(3)
                val = (state.get("outputs") or {}).get(var)
                when_ok = (val == lit) if op == "==" else (val != lit)

        if should_skip or (ok and not when_ok):
            skipped_ids.append(sid)
            continue
        if ok:
            runnable.append(_render_step(sdef, state, workflow_dir, run_dir))

    # If we found skip markers, emit them so the caller can transition status.
    result = {"runnable": runnable, "to_skip": skipped_ids}
    # Terminal classification (caller uses this to decide summary / exit).
    in_flight = any(s.get("status") == "running" for s in state.get("steps") or [])
    any_pending = any(s.get("status") == "pending" for s in state.get("steps") or [])
    any_failed = any(s.get("status") == "failed" for s in state.get("steps") or [])
    all_done = all(s.get("status") in TERMINAL_STEP for s in state.get("steps") or [])
    if not runnable and not skipped_ids and not in_flight:
        if any_failed:
            result["terminal"] = "failed"
        elif all_done:
            result["terminal"] = "completed"
        elif any_pending:
            # Dependencies unreachable but still pending — treat as failed.
            result["terminal"] = "failed"
    print(json.dumps(result, indent=2))
    return 0


# ---------- update-step / update-run ---------------------------------------

def cmd_update_step(state_path: str, step_id: str, kvs: list[str]) -> int:
    state = load_yaml(Path(state_path))
    step = _step_by_id(state["steps"], step_id)
    if step is None:
        print(f"no such step: {step_id}", file=sys.stderr)
        return 1
    for k, v in parse_kv_args(kvs).items():
        step[k] = coerce(v)
    state["last_activity_at"] = utc_now()
    save_yaml(Path(state_path), state)
    return 0


def cmd_update_run(state_path: str, kvs: list[str]) -> int:
    state = load_yaml(Path(state_path))
    for k, v in parse_kv_args(kvs).items():
        state[k] = coerce(v)
    state["last_activity_at"] = utc_now()
    save_yaml(Path(state_path), state)
    return 0


def cmd_record_output(state_path: str, name: str, value: str) -> int:
    state = load_yaml(Path(state_path))
    state.setdefault("outputs", {})[name] = value
    state["last_activity_at"] = utc_now()
    save_yaml(Path(state_path), state)
    return 0


# ---------- reset-running ---------------------------------------------------

def cmd_reset_running(state_path: str) -> int:
    state = load_yaml(Path(state_path))
    for step in state.get("steps") or []:
        if step.get("status") == "running":
            step["status"] = "pending"
            step.pop("started_at", None)
            step.pop("run_id", None)
            step.pop("log", None)
    state["status"] = "running"
    state["last_activity_at"] = utc_now()
    save_yaml(Path(state_path), state)
    return 0


# ---------- list-runs / dump-state -----------------------------------------

def cmd_list_runs(runs_root: str) -> int:
    root = Path(runs_root)
    if not root.is_dir():
        print("(no runs in this workspace yet)")
        return 0
    entries = []
    for child in sorted(root.iterdir()):
        state_path = child / "state.yaml"
        if not state_path.is_file():
            continue
        try:
            state = load_yaml(state_path)
        except Exception as exc:
            entries.append((child.name, f"<unreadable: {exc}>", "", ""))
            continue
        entries.append((
            child.name,
            state.get("status", "?"),
            state.get("workflow_name", "?"),
            state.get("last_activity_at", "?"),
        ))
    if not entries:
        print("(no runs in this workspace yet)")
        return 0
    print(f"{'RUN ID':26}  {'STATUS':10}  {'WORKFLOW':24}  LAST ACTIVITY")
    for row in entries:
        print(f"{row[0]:26}  {row[1]:10}  {row[2]:24}  {row[3]}")
    return 0


def cmd_dump_state(state_path: str) -> int:
    state = load_yaml(Path(state_path))
    print(yaml.safe_dump(state, sort_keys=False, default_flow_style=False))
    return 0


# ---------- session helpers -------------------------------------------------

# Claude Code writes one .jsonl per session at
#   ~/.claude/projects/<cwd-slug>/<session-uuid>.jsonl
# where <cwd-slug> is the absolute cwd path with every `/` replaced by `-`.
# No documented env var surfaces the session UUID into a skill's shell, so
# we pick the most-recently-modified .jsonl in that directory: the active
# session is being appended to right now, which keeps its mtime freshest.

def _cwd_session_dir() -> Path:
    # Shares the slug shape with wise_runs_root_for_cwd so the two namespaces
    # stay in lockstep — useful when correlating a run with its originating
    # Claude Code session transcript.
    return HOME / ".claude/projects" / _cwd_slug()


def _current_session_id() -> str | None:
    session_dir = _cwd_session_dir()
    if not session_dir.is_dir():
        return None
    newest: tuple[float, str] | None = None
    for entry in session_dir.iterdir():
        if entry.suffix != ".jsonl" or not entry.is_file():
            continue
        mtime = entry.stat().st_mtime
        if newest is None or mtime > newest[0]:
            newest = (mtime, entry.stem)
    return newest[1] if newest else None


def cmd_current_session_id() -> int:
    sid = _current_session_id()
    if not sid:
        # Not fatal — the skill decides how to handle. Empty stdout,
        # non-zero exit so callers can detect it.
        return 2
    print(sid)
    return 0


def cmd_session_path(session_id: str) -> int:
    path = _cwd_session_dir() / f"{session_id}.jsonl"
    if not path.is_file():
        return 2
    print(path)
    return 0


def cmd_session_label(run_id: str, workflow_name: str) -> int:
    # Take the first seven hyphen-separated tokens of the workflow name
    # so the label carries enough context in `/resume`'s picker without
    # growing unboundedly for verbose names.
    tokens = [t for t in workflow_name.split("-") if t][:7]
    slug = "-".join(tokens) or "workflow"
    print(f"{run_id}_{slug}")
    return 0


def cmd_list_defs() -> int:
    """JSON of every workflow definition Claude can run.

    Scans both layouts under each root:
      <root>/<name>/workflow.yaml   (folder form, preferred)
      <root>/<name>.yaml            (legacy flat form)

    User-authored definitions take precedence on name collision — the
    picker shows them first and the bundled duplicate is flagged.
    Within a single root, if both layouts exist for the same name, the
    folder form wins.
    """
    seen: set[str] = set()
    items: list[dict] = []
    for source, root in (("user", USER_DEFS), ("bundled", BUNDLED_DEFS)):
        if not root.is_dir():
            continue
        # Collect (name, path) pairs from both layouts, folder first so it
        # wins on same-root collisions.
        entries: list[tuple[str, Path]] = []
        seen_in_root: set[str] = set()
        for child in sorted(root.iterdir()):
            if child.is_dir():
                wf = child / "workflow.yaml"
                if wf.is_file():
                    entries.append((child.name, wf))
                    seen_in_root.add(child.name)
        for child in sorted(root.glob("*.yaml")):
            name = child.stem
            if name in seen_in_root:
                continue
            entries.append((name, child))
        for name, path in entries:
            try:
                data = load_yaml(path)
            except Exception as exc:
                items.append({
                    "name": name,
                    "description": f"<unreadable: {exc}>",
                    "source": source,
                    "shadowed": name in seen,
                })
                continue
            items.append({
                "name": data.get("name") or name,
                "description": (data.get("description") or "").strip() or None,
                "source": source,
                "shadowed": name in seen,
            })
            seen.add(name)
    print(json.dumps(items, indent=2))
    return 0


def cmd_list_resumable_runs() -> int:
    """JSON of non-terminal runs in the current workspace.

    Sorted by last_activity_at descending so the picker shows the most
    recently touched run at the top.
    """
    runs_root = wise_runs_root_for_cwd()
    items: list[dict] = []
    if runs_root.is_dir():
        for child in runs_root.iterdir():
            state_path = child / "state.yaml"
            if not state_path.is_file():
                continue
            try:
                state = load_yaml(state_path)
            except Exception:
                continue
            status = state.get("status")
            if status in TERMINAL_RUN:
                continue
            items.append({
                "run_id": state.get("run_id") or child.name,
                "workflow_name": state.get("workflow_name"),
                "status": status,
                "last_activity_at": state.get("last_activity_at"),
                "session_label": state.get("session_label"),
                "claude_session_id": state.get("claude_session_id"),
            })
    items.sort(key=lambda r: r.get("last_activity_at") or "", reverse=True)
    print(json.dumps(items, indent=2))
    return 0


def cmd_prune_runs() -> int:
    """Cap the per-workspace run history at WISE_RUN_HISTORY_CAP (default 25).

    Non-terminal runs (initializing/running/paused/failed) are
    protected and kept regardless of the cap — they may be actively
    resumable. The remaining budget is filled by the most recently
    active terminal runs (completed/cancelled); everything older is
    deleted from disk along with its step logs.

    Emits one `PRUNED:<run-id>` line per deleted run on stdout and
    `PRUNE-FAILED:<run-id>:<reason>` on stderr for any that couldn't
    be removed.
    """
    try:
        cap = int(os.environ.get("WISE_RUN_HISTORY_CAP", RUN_HISTORY_CAP_DEFAULT))
    except ValueError:
        cap = RUN_HISTORY_CAP_DEFAULT
    if cap < 1:
        cap = RUN_HISTORY_CAP_DEFAULT

    runs_root = wise_runs_root_for_cwd().resolve()
    if not runs_root.is_dir():
        return 0

    entries: list[tuple[str, bool, Path]] = []
    for child in runs_root.iterdir():
        if not child.is_dir():
            continue
        state_path = child / "state.yaml"
        if not state_path.is_file():
            # Orphan dir (no state.yaml) — treat as oldest terminal so
            # it's the first to be reclaimed.
            entries.append(("", True, child))
            continue
        try:
            state = load_yaml(state_path)
        except Exception:
            entries.append(("", True, child))
            continue
        last = (
            state.get("last_activity_at")
            or state.get("started_at")
            or ""
        )
        is_term = state.get("status") in TERMINAL_RUN
        entries.append((str(last), is_term, child))

    if len(entries) <= cap:
        return 0

    non_term = sorted(
        (e for e in entries if not e[1]),
        key=lambda e: (e[0], e[2].name),
        reverse=True,
    )
    term = sorted(
        (e for e in entries if e[1]),
        key=lambda e: (e[0], e[2].name),
        reverse=True,
    )
    keep = non_term + term
    to_delete = keep[cap:]

    root_str = str(runs_root)
    for _last, _term, path in to_delete:
        # Safety rail: never rmtree outside the runs directory.
        try:
            resolved = str(path.resolve())
        except Exception:
            continue
        if not resolved.startswith(root_str + os.sep):
            continue
        try:
            shutil.rmtree(path)
            print(f"PRUNED:{path.name}")
        except Exception as exc:
            print(f"PRUNE-FAILED:{path.name}:{exc}", file=sys.stderr)
    return 0


def cmd_find_runs_by_session(session_id: str) -> int:
    runs_root = wise_runs_root_for_cwd()
    if not runs_root.is_dir():
        return 0
    for child in sorted(runs_root.iterdir()):
        state_path = child / "state.yaml"
        if not state_path.is_file():
            continue
        try:
            state = load_yaml(state_path)
        except Exception:
            continue
        if state.get("claude_session_id") != session_id:
            continue
        if state.get("status") in TERMINAL_RUN:
            continue
        print(f"{state.get('run_id', child.name)}\t"
              f"{state.get('workflow_name', '?')}\t"
              f"{state.get('status', '?')}")
    return 0


# ---------- render-template -------------------------------------------------

def cmd_render(template: str, state_path: str) -> int:
    state = load_yaml(Path(state_path))
    outputs = dict(state.get("outputs") or {})
    project = state.get("project") or {}
    out = template
    out = out.replace("{{run.dir}}", str(Path(state_path).parent))
    out = out.replace("{{run.id}}", str(state.get("run_id") or ""))
    for k, v in project.items():
        out = out.replace("{{project." + k + "}}", str(v))
    for k, v in outputs.items():
        out = out.replace("{{" + k + "}}", str(v))
    print(out)
    return 0


# ---------- CLI -------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(prog="workflows.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("new-ulid")

    p = sub.add_parser("locate-def"); p.add_argument("name")
    p = sub.add_parser("probe-requires"); p.add_argument("def_path")

    p = sub.add_parser("init-state")
    p.add_argument("def_path"); p.add_argument("run_dir")
    p.add_argument("run_id"); p.add_argument("ctx_json")

    p = sub.add_parser("start-run")
    p.add_argument("state_path"); p.add_argument("ctx_json")

    sub.add_parser("runs-root")
    p = sub.add_parser("get-preflight"); p.add_argument("def_path")
    p = sub.add_parser("write-log")
    p.add_argument("run_dir")
    p.add_argument("step_id")
    p.add_argument("step_run_id")
    p = sub.add_parser("list-inputs"); p.add_argument("def_path")

    p = sub.add_parser("validate-input")
    p.add_argument("raw")
    p.add_argument("extract")
    p.add_argument("validate")

    p = sub.add_parser("next-wave")
    p.add_argument("def_path"); p.add_argument("state_path")

    p = sub.add_parser("update-step")
    p.add_argument("state_path"); p.add_argument("step_id")
    p.add_argument("kvs", nargs="+")

    p = sub.add_parser("update-run")
    p.add_argument("state_path"); p.add_argument("kvs", nargs="+")

    p = sub.add_parser("record-output")
    p.add_argument("state_path"); p.add_argument("name"); p.add_argument("value")

    p = sub.add_parser("reset-running"); p.add_argument("state_path")
    p = sub.add_parser("list-runs"); p.add_argument("runs_root")
    p = sub.add_parser("dump-state"); p.add_argument("state_path")

    p = sub.add_parser("render")
    p.add_argument("template"); p.add_argument("state_path")

    sub.add_parser("current-session-id")
    p = sub.add_parser("session-path"); p.add_argument("session_id")
    p = sub.add_parser("session-label")
    p.add_argument("run_id"); p.add_argument("workflow_name")
    p = sub.add_parser("find-runs-by-session"); p.add_argument("session_id")
    sub.add_parser("list-defs")
    sub.add_parser("list-resumable-runs")
    sub.add_parser("prune-runs")

    args = parser.parse_args()

    dispatch = {
        "new-ulid": lambda: cmd_new_ulid(),
        "locate-def": lambda: cmd_locate_def(args.name),
        "probe-requires": lambda: cmd_probe_requires(args.def_path),
        "init-state": lambda: cmd_init_state(args.def_path, args.run_dir, args.run_id, args.ctx_json),
        "start-run": lambda: cmd_start_run(args.state_path, args.ctx_json),
        "runs-root": lambda: cmd_runs_root(),
        "get-preflight": lambda: cmd_get_preflight(args.def_path),
        "write-log": lambda: cmd_write_log(args.run_dir, args.step_id, args.step_run_id),
        "list-inputs": lambda: cmd_list_inputs(args.def_path),
        "validate-input": lambda: cmd_validate_input(args.raw, args.extract, args.validate),
        "next-wave": lambda: cmd_next_wave(args.def_path, args.state_path),
        "update-step": lambda: cmd_update_step(args.state_path, args.step_id, args.kvs),
        "update-run": lambda: cmd_update_run(args.state_path, args.kvs),
        "record-output": lambda: cmd_record_output(args.state_path, args.name, args.value),
        "reset-running": lambda: cmd_reset_running(args.state_path),
        "list-runs": lambda: cmd_list_runs(args.runs_root),
        "dump-state": lambda: cmd_dump_state(args.state_path),
        "render": lambda: cmd_render(args.template, args.state_path),
        "current-session-id": lambda: cmd_current_session_id(),
        "session-path": lambda: cmd_session_path(args.session_id),
        "session-label": lambda: cmd_session_label(args.run_id, args.workflow_name),
        "find-runs-by-session": lambda: cmd_find_runs_by_session(args.session_id),
        "list-defs": lambda: cmd_list_defs(),
        "list-resumable-runs": lambda: cmd_list_resumable_runs(),
        "prune-runs": lambda: cmd_prune_runs(),
    }
    return dispatch[args.cmd]()


if __name__ == "__main__":
    sys.exit(main() or 0)
