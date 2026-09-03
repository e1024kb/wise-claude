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
#   current-session-id                                  → harness session id: $CLAUDE_CODE_SESSION_ID / $WISE_SESSION_ID, else the newest ~/.claude/projects/<cwd-slug>/ transcript, else a synthetic per-workspace id (non-Claude harnesses)
#   session-path       <session-id>                     → path to the Claude .jsonl transcript; exit 2 if stale/absent (always absent off Claude)
#   session-label      <run-id> <workflow-name>         → <run-id>_<first-7-hyphen-tokens>
#   find-runs-by-session <session-id>                   → non-terminal runs in cwd claiming this session; per line: run-id, workflow, status, last_activity_at, fresh|stale (stale = abandoned, not a real conflict)
#   worker-heartbeat   <run-dir> <name> [phase] [task]  → refresh <run-dir>/workers/<name>.hb with the current UTC stamp (supervised workers call this every turn)
#   stale-workers      <run-dir> [expected-csv]         → supervised workers that look hung; per line: name, last-hb, stale|missing, age-secs (SILENT when all fresh — Monitor-safe)
#   supervise-config                                    → JSON {stale_secs, poll_secs, max_nudges, max_respawns} resolved from WISE_WORKER_* env (one shell call for the supervisor loop)
#   list-defs                                           → JSON [{name, description, source}] of bundled + user workflow definitions
#   list-resumable-runs                                 → JSON [{run_id, workflow_name, status, last_activity_at, session_label}] of non-terminal runs in cwd
#   prune-runs                                          → delete oldest terminal runs in cwd so total run count ≤ WISE_RUN_HISTORY_CAP (default 25)
#   apply-worktree-include <repo-root> <worktree-dir>   → copy .worktreeinclude-listed untracked files from repo-root into a new worktree (gitignore-syntax; git does the matching; overwrites; best-effort, always exit 0)

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
BUNDLED_AGENTS = PLUGIN_ROOT / "agents"
# PLUGIN_DATA / USER_DEFS are defined below, after wise_data_root(), so the
# non-Claude fallback can reuse it.


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


def plugin_data_root() -> Path:
    """Where user-authored workflow definitions live.

    On Claude Code this is `$CLAUDE_PLUGIN_DATA` (Claude-managed, wiped on
    reinstall). On any other harness that variable is unset, so we fall
    back to `$WISE_DATA_DIR` (an explicit harness-neutral override) and
    finally to `wise_data_root()` — co-locating definitions with runs and
    insights under one stable, XDG-honouring tree. Kept a function (not a
    frozen module constant) so the resolution is testable under a patched
    environment.
    """
    override = os.environ.get("CLAUDE_PLUGIN_DATA") or os.environ.get("WISE_DATA_DIR")
    return Path(override) if override else wise_data_root()


PLUGIN_DATA = plugin_data_root()
USER_DEFS = PLUGIN_DATA / "workflows/definitions"


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
STEP_TYPES = {
    "skill", "prompt", "bash", "approval", "ask", "interactive",
    "supervised-prompt",
}
TERMINAL_STEP = {"completed", "failed", "skipped", "cancelled"}
TERMINAL_RUN = {"completed", "cancelled"}
RUN_HISTORY_CAP_DEFAULT = 25

# A non-terminal run that shares the *current* session id is only a genuine
# conflict while its conductor is in-flight. A Claude Code session is
# single-threaded: the live conductor is the one running this check, so any
# OTHER run tagged with the same session is necessarily quiescent right now.
# We tell "the user just interrupted an active run" (worth a prompt) apart from
# "a run was abandoned mid-flight and its state froze at running/paused/failed"
# (false conflict, must not block) by the age of `last_activity_at`: an active
# run checkpoints every step/output transition, so a genuine interruption is
# seconds-to-minutes old, while an orphaned run's timestamp is frozen far in
# the past. Overridable via WISE_SESSION_STALE_SECS.
SESSION_STALE_SECS_DEFAULT = 1800

# Supervised-execution (watchdog) knobs. DISTINCT from the session staleness
# above on purpose: SESSION_STALE_SECS_DEFAULT (1800s) decides whether a whole
# *run* was abandoned mid-flight; WORKER_STALE_SECS_DEFAULT decides whether a
# single supervised *worker* (a background teammate) has gone hung mid-turn and
# needs a nudge — a much tighter window, since a worker silent for minutes with
# no heartbeat is almost certainly stuck, not thinking. A run can be fresh while
# one of its workers is stale, and vice versa; never collapse the two knobs.
# All four are positive-int env overrides read through `_env_positive_int`.
WORKER_STALE_SECS_DEFAULT = 180     # WISE_WORKER_STALE_SECS  — nudge after this much silence
WORKER_POLL_SECS_DEFAULT = 30       # WISE_WORKER_POLL_SECS   — supervisor Monitor poll interval
WORKER_MAX_NUDGES_DEFAULT = 2       # WISE_WORKER_MAX_NUDGES  — nudges before TaskStop + respawn
WORKER_MAX_RESPAWNS_DEFAULT = 1     # WISE_WORKER_MAX_RESPAWNS — respawns before failing the slot
TRIGGER_RULES = {
    "all-success",
    "one-success",
    "all-done",
    "none-failed",
    "none-failed-min-one-success",
}

# ---- model / effort resolution (per-step model+effort knobs) ---------------
#
# wise's per-step `effort:` scale, low→high. Steps run in-conversation
# (subscription-covered), so this drives the agent-frontmatter baseline + the
# best-effort prompt directive. See docs/wise/workflows.md.
EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max"]

# Which effort levels each Claude model family accepts. A model not listed is
# treated as unrestricted (we don't down-map what we can't reason about — e.g.
# a future/unrecognised Claude id). An empty set means the family
# has no effort control at all, so a requested effort is dropped.
# Sources: Anthropic effort docs + GET /v1/models capabilities.effort.
MODEL_EFFORT_SUPPORT = {
    "opus":  {"low", "medium", "high", "xhigh", "max"},
    "fable": {"low", "medium", "high", "xhigh", "max"},
    # Sonnet 5 (the alias target) added xhigh — first Sonnet tier with it.
    # Family granularity means a pinned claude-sonnet-4-6 (no xhigh) slips
    # through unclamped; acceptable — effort is a best-effort prompt
    # directive, and aliases are the recommended way to author workflows.
    "sonnet": {"low", "medium", "high", "xhigh", "max"},
    "haiku": set(),                                 # no effort control
}

# Policy effort ceilings — the highest effort wise will REQUEST from a model.
# Distinct from MODEL_EFFORT_SUPPORT above on purpose: that one is *capability*
# (what the model accepts), this one is *policy* (what is worth asking for).
# Keyed per model, NOT per family, because the tiers differ by version: Opus 5
# reasons deeper at every level than Opus 4.8, so wise's planning steps get
# their signal at `high` and `xhigh`/`max` only buy latency and tokens, while
# Opus 4.8 still needs `xhigh` for the same steps. A model absent from the
# table has no ceiling (its capability set is the only limit).
#
# Keys match the RESOLVED model id/alias: exact first, then the longest
# `claude-…` key the model is a DATED SNAPSHOT of (base id + `-YYYYMMDD`),
# so claude-opus-5-20260401 inherits claude-opus-5's ceiling while a
# neighbouring id (claude-opus-50-20270101) or a version bump
# (claude-opus-5-1) does not. Override per run with
# WISE_EFFORT_CEILING="claude-opus-5=xhigh,opus=xhigh" (`<model>=off` drops one
# entry, a bare `off` disables every ceiling).
MODEL_EFFORT_CEILING = {
    "opus": "high",             # the alias resolves to the latest Opus = Opus 5
    "claude-opus-5": "high",
    "claude-opus-4-8": "xhigh",
}

# Low-profile Opus rule (MUST). Under the `low` token-budget profile wise
# NEVER dispatches Opus 5: every Opus-family pin — the `opus` alias, a
# `claude-opus-5*` id, a retired id that substitutes to `opus` — resolves
# to Opus 4.8 instead. Enforced in `_resolve_model_dict` whenever the
# caller passes `profile="low"` (get-profiles for the `low` level,
# resolve-model / resolve-team via `--profile low`). Not an env-tunable:
# the rule is part of what `low` means, not a ceiling to raise.
LOW_PROFILE_OPUS_MODEL = "claude-opus-4-8"

# One-hop fallback per alias family, used when a pinned model is unavailable.
# Prefer aliases in authored workflows — they auto-resolve and rarely retire.
MODEL_TIER_NEXT = {
    "fable": "opus",
    "opus": "sonnet",
    "sonnet": "haiku",
    "haiku": "sonnet",
}

# Known-retired / soon-retired full ids → the maintained alias that replaces
# them. Small and shippable; the durable check is GET /v1/models (needs an API
# key the subscription-auth conductor may lack — see docs). Keep alias-first so
# substitutions don't themselves go stale. Reasons are surfaced to the user.
RETIRED_MODELS = {
    "claude-3-opus-20240229":      ("opus",   "retired"),
    "claude-3-sonnet-20240229":    ("sonnet", "retired"),
    "claude-3-5-sonnet-20240620":  ("sonnet", "retired"),
    "claude-3-5-sonnet-20241022":  ("sonnet", "retired"),
    "claude-3-7-sonnet-20250219":  ("sonnet", "retired"),
    "claude-3-haiku-20240307":     ("haiku",  "retired"),
    "claude-3-5-haiku-20241022":   ("haiku",  "retired"),
    "claude-opus-4-20250514":      ("opus",   "deprecated"),
    "claude-sonnet-4-20250514":    ("sonnet", "deprecated"),
    "claude-opus-4-1-20250805":    ("opus",   "deprecated"),
}


# ---------- utilities -------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_positive_int(name: str, default: int) -> int:
    """A positive-int env override, falling back to `default` when the
    variable is unset, non-numeric, or not positive."""
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        return default
    return value if value > 0 else default


def _session_is_fresh(iso_ts: str | None, stale_after: int) -> bool:
    """Whether an ISO `%Y-%m-%dT%H:%M:%SZ` activity stamp is recent enough
    to count as a live run rather than one abandoned mid-flight.

    Missing or unparseable → False (treat as long-abandoned). A future
    stamp (clock skew) reads as fresh. See SESSION_STALE_SECS_DEFAULT for
    why recency distinguishes a live conductor from an orphaned run.
    """
    if not iso_ts:
        return False
    try:
        dt = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - dt).total_seconds() <= stale_after


def load_yaml(path: Path) -> dict:
    with path.open() as fh:
        data = yaml.safe_load(fh)
    return data or {}


def save_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        try:
            fh = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with fh:
            yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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

    def _cache_layout_name(p: Path) -> str:
        """Recover `<plugin>` from a `cache/<marketplace>/<plugin>[/<version>]/`
        path when `plugin.json` itself can't supply a usable `name` — used
        when the file is missing/unparseable/nameless, so we don't fall
        back to `p.name`, which is the VERSION string two levels below the
        plugin dir in that layout, not the plugin name. Layout is derived
        relative to `root` (`_walk`'s own traversal root), not by matching
        "cache" anywhere in the absolute path — HOME itself could contain
        a "cache" ancestor unrelated to this layout. Non-cache layouts
        just return `p.name` unchanged."""
        try:
            parts = p.relative_to(root).parts
        except ValueError:
            return p.name
        if len(parts) >= 3 and parts[0] == "cache":
            return parts[2]
        return p.name

    # Fallback: walk looking for `.claude-plugin/plugin.json`. Goes up to
    # four levels deep to cover cache/<marketplace>/<plugin>/<version>/.
    # Read the plugin's own `name` field rather than the directory it was
    # found in — in the cache layout that directory is the VERSION string
    # (cache/<marketplace>/<plugin>/<version>/.claude-plugin/plugin.json),
    # not the plugin name, so using `p.name` there falsely reports the
    # actual plugin as not installed. If `plugin.json` can't supply a
    # usable name (missing, unparseable, empty `name` field), recover the
    # plugin id from the cache path shape instead of guessing `p.name`.
    def _walk(p: Path, depth: int) -> None:
        if depth > 4 or not p.is_dir():
            return
        pj = p / ".claude-plugin" / "plugin.json"
        if pj.is_file():
            name = None
            try:
                data = json.loads(pj.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("name"), str) and data["name"]:
                    name = data["name"]
            except (OSError, ValueError):
                pass
            names.add(name or _cache_layout_name(p))
            return
        try:
            children = list(p.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_dir():
                _walk(child, depth + 1)

    try:
        top_children = list(root.iterdir())
    except OSError:
        return names
    for child in top_children:
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


# Step ids come from user-authored workflow.yaml and get interpolated
# straight into filesystem paths (write-log's <step-id>.<step-run-id>.log),
# so they're validated wherever a workflow definition is first read.
# Step ids are hyphen-case (unlike cmd_list_inputs's snake_case-only
# `^[a-z][a-z0-9_]*$` input-name pattern), so this allows `-` too.
STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*\Z")


def _validate_step_defs(steps: list, def_path: str) -> str | None:
    """Return an error message if any step is missing `id`/`type`, has an
    id that fails `STEP_ID_RE`, or repeats an id already seen; `None` if
    every step is well-formed. Duplicate ids are rejected here because
    `cmd_next_wave` keys `step_defs` by id (last-one-wins, silently
    dropping the earlier step) while `_step_by_id()` returns the first
    match — a duplicate would make the two disagree on which def a step
    actually runs."""
    seen: set[str] = set()
    for step in steps:
        if (
            not isinstance(step, dict)
            or "id" not in step
            or not isinstance(step.get("type"), str)
            or not step["type"]
        ):
            return f"INVALID:step-missing-id-or-type in {def_path}: {step!r}"
        sid = step["id"]
        if not isinstance(sid, str) or not STEP_ID_RE.match(sid):
            return (f"INVALID:step-id:{sid!r} in {def_path} "
                     f"(must match {STEP_ID_RE.pattern})")
        if sid in seen:
            return f"INVALID:duplicate-step-id:{sid!r} in {def_path}"
        seen.add(sid)
    return None


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
    raw_steps = definition.get("steps") or []
    err = _validate_step_defs(raw_steps, def_path)
    if err:
        print(err, file=sys.stderr)
        return 2
    steps = []
    for step in raw_steps:
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

        {"control_mode": "wave-sync" | "synchronous" | "auto-advance",
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

    Defense-in-depth: `step_id`/`step_run_id` are interpolated straight
    into the log path, so a value containing `/` or `..` could escape
    `<run-dir>/logs/`. Both are validated as safe single path components
    before the path is built, rather than trusting the caller (the
    conductor's blanket Bash grant means this runs unattended).
    """
    if not STEP_ID_RE.match(step_id):
        print(f"INVALID:step-id:{step_id!r} (must match {STEP_ID_RE.pattern})",
              file=sys.stderr)
        return 2
    if not re.match(r"^[A-Za-z0-9_-]+\Z", step_run_id):
        print(f"INVALID:step-run-id:{step_run_id!r} (must be a bare "
              f"alphanumeric/_/- token)", file=sys.stderr)
        return 2
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

# Valid values + default for each preflight key. For the three original
# keys `prompt` is the default — the conductor asks the user via
# AskUserQuestion; any other listed value pins the answer and skips the
# prompt entirely. The opt-in questionaries (`tuning`, `step-select`)
# default to `skip` instead, so workflows authored before this schema
# addition never gain a surprise prompt — a workflow enables them
# explicitly with `tuning: prompt` / `step-select: prompt`.
PREFLIGHT_KEYS = {
    "control-mode":   ({"prompt", "wave-sync", "synchronous", "auto-advance"}, "prompt"),
    "worktree":       ({"prompt", "current", "new"}, "prompt"),
    "rename_session": ({"prompt", "skip"}, "prompt"),
    "tuning":         ({"prompt", "skip"}, "skip"),
    "step-select":    ({"prompt", "skip"}, "skip"),
}


def cmd_get_preflight(def_path: str) -> int:
    """Emit the workflow's preflight pin map as KEY=VALUE lines.

    Always emits every known key so the conductor can `source` the
    output without branching on presence:

        CONTROL_MODE=<prompt|wave-sync|synchronous|auto-advance>
        WORKTREE=<prompt|current|new>
        RENAME_SESSION=<prompt|skip>
        TUNING=<prompt|skip>
        STEP_SELECT=<prompt|skip>

    Missing keys (or a missing `preflight:` block entirely) fall back to
    each key's default — `prompt` for the three original prompts,
    `skip` for the opt-in questionaries — so workflows authored before
    a schema addition keep their existing behaviour.

    Unknown values (typos, wrong enum) fall back to the key's default
    with a warning on stderr so the workflow still runs rather than
    failing.
    """
    data = load_yaml(Path(def_path))
    block = data.get("preflight") or {}
    if not isinstance(block, dict):
        print(f"INVALID:preflight-block:expected-mapping", file=sys.stderr)
        block = {}
    out = {}
    for key, (allowed, default) in PREFLIGHT_KEYS.items():
        value = block.get(key) or default
        if value not in allowed:
            print(
                f"WARN:preflight.{key}={value!r} not in {sorted(allowed)}; "
                f"falling back to {default!r}",
                file=sys.stderr,
            )
            value = default
        out[key] = value
    # Emit as KEY=VALUE (uppercase, hyphens → underscores) so the
    # conductor can eval/source the output.
    for key, value in out.items():
        var = key.upper().replace("-", "_")
        print(f"{var}={value}")
    return 0


# ---------- get-tuning / get-step-select ------------------------------------

# Like STEP_ID_RE but WITHOUT underscores — deliberate: tuning-group ids
# become `tuning_<id>` output names, where an underscore in the id would
# blur the prefix boundary. Keep the two regexes' divergence intentional.
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def _claim_slug(value: str, seen: set, bad_marker: str, dup_marker: str) -> bool:
    """Validate + claim a schema id: slug-shaped and unique within `seen`.

    On failure prints the `INVALID:` line (with the caller's marker) and
    returns False — the shared shape for every id check in the tuning /
    step-select schema parsers.
    """
    if not _SLUG_RE.match(value):
        print(f"INVALID:{bad_marker}:{value!r}", file=sys.stderr)
        return False
    if value in seen:
        print(f"INVALID:{dup_marker}:{value}", file=sys.stderr)
        return False
    seen.add(value)
    return True


def cmd_get_tuning(def_path: str) -> int:
    """Emit the workflow's `tuning:` block as JSON for the conductor.

    Schema (top-level in the definition):

        tuning:
          groups:
            - id: authoring                # slug, unique
              label: "Plan authoring"      # shown in the questionary
              steps: [gap-analysis, ...]   # prompt-step ids the override binds to
            - id: plan
              label: "Plan phase"
              default: "opus / xhigh"      # advisory group (no steps): display-only
                                           # default; the workflow consumes the
                                           # user's choice via {{tuning_<id>}} /
                                           # {{tuning_summary}} outputs instead

    Output: `{"groups": [{id, label, steps: [{id, model, effort}], default?}]}`
    where each bound step carries its declared `model:` (or `inherit`) and
    `effort:` (or null) so the conductor can render "Keep default (…)" options.
    Empty `{"groups": []}` when the workflow declares no tuning block.
    Structural errors (bad id, unknown step, non-prompt step, no steps AND no
    default, steps AND default together — the two are exclusive modes) exit 2
    with an `INVALID:` line on stderr — a half-parsed tuning questionary is
    worse than a loud authoring failure.
    """
    data = load_yaml(Path(def_path))
    block = data.get("tuning") or {}
    if not isinstance(block, dict):
        print("INVALID:tuning-block:expected-mapping", file=sys.stderr)
        return 2
    raw_steps = data.get("steps") or []
    err = _validate_step_defs(raw_steps, def_path)
    if err:
        print(err, file=sys.stderr)
        return 2
    groups = block.get("groups") or []
    step_defs = {s["id"]: s for s in raw_steps}
    out: list[dict] = []
    seen: set[str] = set()
    for g in groups:
        if not isinstance(g, dict):
            print("INVALID:tuning-group:expected-mapping", file=sys.stderr)
            return 2
        gid = str(g.get("id") or "")
        if not _claim_slug(gid, seen, "tuning-group-id", "duplicate-tuning-group"):
            return 2
        entry: dict = {"id": gid, "label": str(g.get("label") or gid)}
        step_ids = g.get("steps") or []
        if not isinstance(step_ids, list):
            # A scalar here would "work" by iterating characters — reject.
            print(f"INVALID:tuning-steps-not-list:{gid}", file=sys.stderr)
            return 2
        bound: list[dict] = []
        for sid in step_ids:
            sdef = step_defs.get(sid)
            if sdef is None:
                print(f"INVALID:tuning-unknown-step:{gid}:{sid}", file=sys.stderr)
                return 2
            if sdef.get("type") != "prompt":
                print(f"INVALID:tuning-non-prompt-step:{gid}:{sid}", file=sys.stderr)
                return 2
            eff = sdef.get("effort")
            bound.append({
                "id": sid,
                "model": str(sdef.get("model") or "inherit"),
                "effort": str(eff) if eff else None,
            })
        entry["steps"] = bound
        if g.get("default"):
            if bound:
                print(f"INVALID:tuning-group-steps-and-default:{gid}",
                      file=sys.stderr)
                return 2
            entry["default"] = str(g["default"])
        if not bound and "default" not in entry:
            print(f"INVALID:tuning-group-empty:{gid}", file=sys.stderr)
            return 2
        out.append(entry)
    print(json.dumps({"groups": out}, indent=2))
    return 0


def cmd_get_step_select(def_path: str) -> int:
    """Emit the workflow's `step-select:` block as JSON for the conductor.

    Schema (top-level in the definition):

        step-select:
          optional:
            - id: deep-dive                     # slug, unique
              label: "Deep-dive context sweep"  # shown in the questionary
              steps: [research-context]         # step ids skipped together;
                                                # defaults to [<id>] when the
                                                # entry id IS a step id
              ask-group: "Research"             # optional — one multiSelect
                                                # question per ask-group (keeps
                                                # each under the 4-option cap)
          presets:
            - id: standard
              label: "Standard"
              description: "Skip the deep-dive sweep"
              skip: [deep-dive]                 # ⊆ optional entry ids

    Output: `{"optional": [{id, label, steps, ask-group?}], "presets": [...]}`.
    The conductor renders presets first (plus an implicit "Full — run
    everything" and "Custom"), then — on Custom — one multiSelect per
    ask-group listing stages to SKIP. Structural errors exit 2 with an
    `INVALID:` line on stderr.
    """
    data = load_yaml(Path(def_path))
    block = data.get("step-select") or {}
    if not isinstance(block, dict):
        print("INVALID:step-select-block:expected-mapping", file=sys.stderr)
        return 2
    raw_steps = data.get("steps") or []
    err = _validate_step_defs(raw_steps, def_path)
    if err:
        print(err, file=sys.stderr)
        return 2
    step_defs = {s["id"] for s in raw_steps}
    optional_out: list[dict] = []
    seen: set[str] = set()
    for entry in block.get("optional") or []:
        if not isinstance(entry, dict):
            print("INVALID:step-select-optional:expected-mapping", file=sys.stderr)
            return 2
        oid = str(entry.get("id") or "")
        if not _claim_slug(oid, seen, "step-select-id", "duplicate-step-select-id"):
            return 2
        raw_entry_steps = entry.get("steps")
        if raw_entry_steps is not None and not isinstance(raw_entry_steps, list):
            print(f"INVALID:step-select-steps-not-list:{oid}", file=sys.stderr)
            return 2
        steps = raw_entry_steps or ([oid] if oid in step_defs else [])
        if not steps:
            print(f"INVALID:step-select-no-steps:{oid}", file=sys.stderr)
            return 2
        for sid in steps:
            if sid not in step_defs:
                print(f"INVALID:step-select-unknown-step:{oid}:{sid}", file=sys.stderr)
                return 2
        item = {"id": oid, "label": str(entry.get("label") or oid), "steps": steps}
        if entry.get("ask-group"):
            item["ask-group"] = str(entry["ask-group"])
        optional_out.append(item)
    presets_out: list[dict] = []
    seen_p: set[str] = set()
    for p in block.get("presets") or []:
        if not isinstance(p, dict):
            print("INVALID:step-select-preset:expected-mapping", file=sys.stderr)
            return 2
        pid = str(p.get("id") or "")
        if not _claim_slug(pid, seen_p, "step-select-preset-id",
                           "duplicate-step-select-preset"):
            return 2
        skip = p.get("skip") or []
        if not isinstance(skip, list):
            print(f"INVALID:preset-skip-not-list:{pid}", file=sys.stderr)
            return 2
        for oid in skip:
            if oid not in seen:
                print(f"INVALID:preset-unknown-optional:{pid}:{oid}", file=sys.stderr)
                return 2
        item = {"id": pid, "label": str(p.get("label") or pid), "skip": skip}
        if p.get("description"):
            item["description"] = str(p["description"])
        presets_out.append(item)
    print(json.dumps({"optional": optional_out, "presets": presets_out}, indent=2))
    return 0


# ---------- get-profiles ----------------------------------------------------

# Cap names become `cap_<name>` outputs consumed as `{{cap_<name>}}`
# template placeholders — underscores allowed (unlike _SLUG_RE) because
# the whole name sits after the `cap_` prefix.
_CAP_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def cmd_get_profiles(def_path: str) -> int:
    """Emit the workflow's `profiles:` block as JSON for the conductor.

    Schema (top-level in the definition; every field optional, and an
    empty level mapping means "the workflow's declared defaults" — the
    `medium` convention):

        profiles:
          low:
            tuning:                       # tuning-group id → "<model> [/ <effort>]" | "default"
              evidence: "sonnet / medium"
            step-preset: minimal          # a step-select preset id, or `full`
            # skip: [deep-dive]           # alternative to step-preset (exclusive)
            team-mode: solo               # solo | full
            caps:                         # advisory ints the workflow's prompts
              max_review_cycles: 2        # consume as {{cap_<name>}}
          medium: {}
          max: { ... }

    Output: `{"profiles": {"low": {"tuning": {gid: {model, effort, reason} |
    "default"}, "step-preset"?, "skip": [...], "team-mode"?, "caps": {...}},
    ...}}` — tuning values come back already resolved through
    `_resolve_model_dict` under their own level (retired-id substitution,
    the low-profile Opus rule for `low`, effort clamp).
    `{"profiles": {}}` when the workflow declares no block. Structural
    errors exit 2 with an `INVALID:` line on stderr, mirroring
    `get-tuning` — the conductor treats that as "fall back to the legacy
    questionary", never as a blocked run.
    """
    data = load_yaml(Path(def_path))
    block = data.get("profiles")
    if block is None:
        print(json.dumps({"profiles": {}}))
        return 0
    if not isinstance(block, dict):
        print("INVALID:profiles-block:expected-mapping", file=sys.stderr)
        return 2

    # A malformed sibling block (tuning:/step-select: not a mapping) is
    # get-tuning's / get-step-select's error to report — here it just
    # means "no known ids", so a profile referencing one comes back as
    # the matching INVALID instead of a traceback.
    tuning_block = data.get("tuning")
    tuning_block = tuning_block if isinstance(tuning_block, dict) else {}
    raw_groups = tuning_block.get("groups")
    tuning_groups = {
        str(g.get("id") or "")
        for g in (raw_groups if isinstance(raw_groups, list) else [])
        if isinstance(g, dict)
    }
    ss_block = data.get("step-select")
    ss_block = ss_block if isinstance(ss_block, dict) else {}
    raw_optional = ss_block.get("optional")
    optional_ids = {
        str(e.get("id") or "")
        for e in (raw_optional if isinstance(raw_optional, list) else [])
        if isinstance(e, dict)
    }
    raw_presets = ss_block.get("presets")
    preset_ids = {
        str(p.get("id") or "")
        for p in (raw_presets if isinstance(raw_presets, list) else [])
        if isinstance(p, dict)
    }

    out: dict[str, dict] = {}
    for level, entry in block.items():
        if level not in PROFILE_LEVELS:
            print(f"INVALID:profile-level:{level}", file=sys.stderr)
            return 2
        if entry is None:
            entry = {}
        if not isinstance(entry, dict):
            print(f"INVALID:profile-entry:expected-mapping:{level}",
                  file=sys.stderr)
            return 2
        res: dict = {"tuning": {}, "skip": [], "caps": {}}

        tuning = entry.get("tuning")
        if tuning is None:
            tuning = {}
        if not isinstance(tuning, dict):
            print(f"INVALID:profile-tuning:expected-mapping:{level}",
                  file=sys.stderr)
            return 2
        for gid, value in tuning.items():
            if gid not in tuning_groups:
                print(f"INVALID:profile-tuning-unknown-group:{level}:{gid}",
                      file=sys.stderr)
                return 2
            if not isinstance(value, str):
                print(f"INVALID:profile-tuning-bad-value:{level}:{gid}",
                      file=sys.stderr)
                return 2
            sval = value.strip()
            if sval == "default":
                res["tuning"][gid] = "default"
                continue
            parts = [x.strip() for x in sval.split("/")]
            model = parts[0] if parts else ""
            effort = parts[1] if len(parts) > 1 else ""
            if not model or len(parts) > 2:
                print(f"INVALID:profile-tuning-bad-value:{level}:{gid}",
                      file=sys.stderr)
                return 2
            # Resolved under the level itself, so `low` applies the
            # low-profile Opus rule (an authored `opus / high` comes back
            # as claude-opus-4-8, with the swap in `reason`).
            rm = _resolve_model_dict(model, effort, level)
            res["tuning"][gid] = {
                "model": rm["model"], "effort": rm["effort"],
                "reason": rm["reason"],
            }

        preset = entry.get("step-preset")
        skip = entry.get("skip")
        if preset is not None and skip is not None:
            print(f"INVALID:profile-step-preset-and-skip:{level}",
                  file=sys.stderr)
            return 2
        if preset is not None:
            preset = str(preset)
            if preset != "full" and preset not in preset_ids:
                print(f"INVALID:profile-step-preset-unknown:{level}:{preset}",
                      file=sys.stderr)
                return 2
            res["step-preset"] = preset
        if skip is not None:
            if not isinstance(skip, list):
                print(f"INVALID:profile-skip-not-list:{level}", file=sys.stderr)
                return 2
            for oid in skip:
                if oid not in optional_ids:
                    print(f"INVALID:profile-skip-unknown-optional:{level}:{oid}",
                          file=sys.stderr)
                    return 2
            res["skip"] = list(skip)

        team_mode = entry.get("team-mode")
        if team_mode is not None:
            if team_mode not in ("solo", "full"):
                print(f"INVALID:profile-team-mode:{level}:{team_mode}",
                      file=sys.stderr)
                return 2
            res["team-mode"] = team_mode

        caps = entry.get("caps")
        if caps is None:
            caps = {}
        if not isinstance(caps, dict):
            print(f"INVALID:profile-caps:expected-mapping:{level}",
                  file=sys.stderr)
            return 2
        for name, value in caps.items():
            if not _CAP_RE.match(str(name)):
                print(f"INVALID:profile-cap-name:{level}:{name}",
                      file=sys.stderr)
                return 2
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                print(f"INVALID:profile-cap-not-positive-int:{level}:{name}",
                      file=sys.stderr)
                return 2
            res["caps"][str(name)] = value

        out[level] = res

    print(json.dumps({"profiles": out}, indent=2))
    return 0


# ---------- list-inputs / validate-input -----------------------------------

def cmd_list_inputs(def_path: str) -> int:
    """Emit the workflow's declared `inputs:` as JSON.

    Each item is `{name, prompt, validate?, extract?, optional?,
    options?, default?}`. Empty list if the workflow declares none. Used
    by workflow-run's pre-flight to know which questions to ask before
    the DAG launches. `optional: true` lets the conductor skip the
    prompt when no value was supplied positionally (the input defaults
    to empty).

    `options:` turns the input into a CHOICE input: a list of
    `{value, label?, description?}` (a bare string is shorthand for
    `{value}`). The conductor renders these as AskUserQuestion options
    (batching consecutive choice inputs into one composite call)
    instead of a free-text prompt; `default:` names the option value to
    list first / apply when the run must proceed without an answer.
    When no explicit `validate:` is declared, one is DERIVED from the
    option values (`^(a|b|c)$`) so positionally-supplied answers are
    membership-checked by the same `validate-input` machinery as typed
    ones — authors never hand-maintain a regex that mirrors the list.
    An explicit `validate:` wins (an intentional loosening, e.g. to
    admit free-text Other values).
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
        if entry.get("optional") is True:
            item["optional"] = True
        raw_opts = entry.get("options")
        if raw_opts is not None and not isinstance(raw_opts, list):
            # A scalar would iterate characters into one option each — reject.
            print(f"INVALID:input-options-not-list:{name}", file=sys.stderr)
            return 2
        if raw_opts:
            opts = []
            for o in raw_opts:
                if isinstance(o, str):
                    opts.append({"value": o})
                elif isinstance(o, dict) and o.get("value"):
                    norm = {"value": str(o["value"])}
                    if o.get("label"):
                        norm["label"] = str(o["label"])
                    if o.get("description"):
                        norm["description"] = str(o["description"])
                    opts.append(norm)
                else:
                    print(f"INVALID:input-option:{name}:{o!r}", file=sys.stderr)
                    return 2
            item["options"] = opts
            default = entry.get("default")
            if default is not None:
                if str(default) not in {o["value"] for o in opts}:
                    print(f"INVALID:input-default:{name}:{default!r}",
                          file=sys.stderr)
                    return 2
                item["default"] = str(default)
            if "validate" not in item:
                item["validate"] = "^(" + "|".join(
                    re.escape(o["value"]) for o in opts) + ")$"
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
    if rule == "none-failed":
        # Like none-failed-min-one-success but tolerates ALL deps being
        # skipped (e.g. user-deselected stages) — the step still runs.
        # Any failed dep propagates the skip; otherwise wait for terminal.
        if failed:
            return False, True
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
    raw_steps = definition.get("steps") or []
    err = _validate_step_defs(raw_steps, def_path)
    if err:
        print(err, file=sys.stderr)
        return 2
    step_defs = {s["id"]: s for s in raw_steps}
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
        # or `name != 'literal'`. A LIST of such conditions is AND-ed (all
        # must hold) — the shape for a step gated on both a captured output
        # and a pre-flight mode choice, e.g.
        #   when:
        #     - "readiness == 'gaps'"
        #     - "gap_mode == 'ask'"
        # Anything unparseable is treated as truthy.
        when = sdef.get("when")
        when_ok = True
        conditions = when if isinstance(when, list) else ([when] if when else [])
        for cond in conditions:
            # fullmatch, not match — `x == 'a' && y == 'b'` must fall to the
            # unparseable path, not silently evaluate its parseable prefix.
            m = re.fullmatch(r"\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(==|!=)\s*'([^']*)'\s*",
                             str(cond))
            if m:
                var, op, lit = m.group(1), m.group(2), m.group(3)
                val = (state.get("outputs") or {}).get(var)
                if not ((val == lit) if op == "==" else (val != lit)):
                    when_ok = False
                    break
            else:
                # Unparseable condition stays truthy (never blocks a step
                # on a typo) but is surfaced — a silent drop-out of the
                # conjunction would mis-gate exactly the steps that decide
                # whether an autonomous run pauses.
                print(f"WARN:when-unparseable:{sid}:{cond!r}", file=sys.stderr)

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
# Claude Code also exports the active session UUID into every skill's shell
# as $CLAUDE_CODE_SESSION_ID — that is exact and per-process, so we read it
# first. The mtime sweep below (pick the most-recently-modified .jsonl in the
# cwd's project dir) is only a fallback for older Claude Code that predates
# the env var: it is UNRELIABLE when several sessions run in the same cwd,
# because they all share one project dir and the freshest transcript may
# belong to a sibling session, not this one. Preferring the env var fixes the
# false "this session already has another running workflow" conflicts that
# concurrent same-repo runs hit.

def _cwd_session_dir() -> Path:
    # Shares the slug shape with wise_runs_root_for_cwd so the two namespaces
    # stay in lockstep — useful when correlating a run with its originating
    # Claude Code session transcript.
    return HOME / ".claude/projects" / _cwd_slug()


def _synthetic_session_id() -> str:
    """A stable per-workspace session id for harnesses that expose no
    session concept (Codex / Cursor / Hermes). All runs in one working
    directory share it — mirroring Claude's per-cwd correlation closely
    enough for the conflict check and, crucially, requiring no transcript
    access so `/resume` still works off-Claude."""
    return "local-" + (_cwd_slug().strip("-") or "workspace")


def _current_session_id() -> str | None:
    # An exact, per-process id exported by the harness wins. Claude Code
    # sets CLAUDE_CODE_SESSION_ID; other harnesses may inject WISE_SESSION_ID.
    for var in ("CLAUDE_CODE_SESSION_ID", "WISE_SESSION_ID"):
        sid = os.environ.get(var, "").strip()
        if sid:
            return sid
    # Older Claude Code without the env var: sweep the cwd's transcript dir.
    session_dir = _cwd_session_dir()
    if session_dir.is_dir():
        newest: tuple[float, str] | None = None
        for entry in session_dir.iterdir():
            if entry.suffix != ".jsonl" or not entry.is_file():
                continue
            mtime = entry.stat().st_mtime
            if newest is None or mtime > newest[0]:
                newest = (mtime, entry.stem)
        if newest:
            return newest[1]
    # No harness id and no Claude transcript → a non-Claude harness. Fall
    # back to a synthetic per-workspace id so runs are still tagged and
    # resumable without any transcript.
    return _synthetic_session_id()


PROFILE_LEVELS = ("low", "medium", "max")
PROFILE_DEFAULT = "medium"
_PROFILE_GC_SECONDS = 30 * 24 * 3600  # prune sibling files older than 30 days


def _profile_dir() -> Path:
    """Session-profile store: `<wise_data_root>/profile/<session-id>`.

    One word per file (low|medium|max). Documented as state exception (e)
    in the plugin CLAUDE.md. Routed through `wise_data_root()` so a future
    relocation stays a one-function change.
    """
    return wise_data_root() / "profile"


# Session ids become filenames under the profile store; a hostile
# CLAUDE_CODE_SESSION_ID / WISE_SESSION_ID (settable from a repo-local
# settings env block) must not traverse out of it. Reject anything that
# is not a plain token — a non-matching id is treated as "no session".
_SESSION_ID_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _profile_safe_sid() -> str | None:
    sid = _current_session_id()
    if not sid or not _SESSION_ID_FILE_RE.match(sid) or sid in (".", ".."):
        return None
    return sid


def cmd_profile_set(level: str) -> int:
    level = level.strip().lower()
    if level not in PROFILE_LEVELS:
        print(f"INVALID:profile-level:{level}", file=sys.stderr)
        return 2
    sid = _profile_safe_sid()
    if not sid:
        print("INVALID:profile-no-session", file=sys.stderr)
        return 2
    pdir = _profile_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    # Atomic write (same mkstemp + replace pattern as init-registry.py) so
    # a concurrent reader never sees a partial file.
    fd, tmp = tempfile.mkstemp(dir=pdir, prefix=".tmp-profile-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(level + "\n")
        os.replace(tmp, pdir / sid)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    # Opportunistic GC: sessions are ephemeral, so files from long-dead
    # sessions accumulate. Best-effort — errors are swallowed; reads never
    # prune (profile-get stays pure and fast).
    cutoff = time.time() - _PROFILE_GC_SECONDS
    for entry in pdir.iterdir():
        with contextlib.suppress(OSError):
            if entry.name != sid and entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
    print(f"PROFILE: level={level} scope=session session={sid}")
    return 0


def cmd_profile_get() -> int:
    """Print the session's stored profile level, degrading to the default.

    NEVER exits non-zero for a missing/garbage store — consumers treat any
    failure as `medium`, so a user who never ran /wise-profile sees zero
    change and no error noise.
    """
    sid = _profile_safe_sid()
    if sid:
        try:
            level = (_profile_dir() / sid).read_text(encoding="utf-8").strip().lower()
            if level in PROFILE_LEVELS:
                print(level)
                return 0
        except (OSError, ValueError):
            # ValueError covers UnicodeDecodeError on a non-UTF-8 store —
            # degrade like any other unreadable store, never traceback.
            pass
    print(PROFILE_DEFAULT)
    return 0


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


def _parse_frontmatter(path: Path) -> dict:
    """Return the YAML frontmatter block of a markdown file as a dict.

    A frontmatter block is the content between the first two `---` fences
    at the very top of the file. Files without one yield `{}`.
    """
    try:
        text = path.read_text()
    except OSError:
        return {}
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)            # closing fence (line-anchored)
    if end == -1:
        return {}
    try:
        data = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _roster_agents() -> list:
    """The bundled SDLC roster as `[{name, description, tools, model, effort}]`,
    sorted by name. Single source for `list-agents` (the conductor's auto-router
    + the create wizard) and for `resolve-team`'s role validation.

    Roster files are real Claude Code plugin subagents — invoked as
    `subagent_type: wise:<name>`. `tools` is surfaced so the conductor can pick
    a tool-aware role (a step that writes files needs a role with Write/Edit;
    one that runs shell/git needs Bash) and fall back to `general-purpose`
    when no role covers the step's needs.
    """
    items: list[dict] = []
    if BUNDLED_AGENTS.is_dir():
        for child in sorted(BUNDLED_AGENTS.glob("*.md")):
            fm = _parse_frontmatter(child)
            tools = fm.get("tools")
            if isinstance(tools, str):
                tools = [t.strip() for t in tools.split(",") if t.strip()]
            items.append({
                "name": fm.get("name") or child.stem,
                "description": (fm.get("description") or "").strip() or None,
                "tools": tools or [],
                "model": fm.get("model") or "inherit",
                "effort": fm.get("effort"),
            })
    return items


def cmd_list_agents() -> int:
    """JSON of the bundled SDLC roster — consumed by the conductor (to resolve a
    `prompt` step's `agent: auto` to a concrete `wise:<name>` subagent) and the
    create wizard (the "force a role" picker). See `_roster_agents`."""
    print(json.dumps(_roster_agents(), indent=2))
    return 0


def _model_family(model: str) -> str:
    """Normalise a model id/alias to a Claude family key, or '' if unknown.

    `inherit` / empty → `inherit` (no constraints applied downstream).
    """
    m = (model or "").strip().lower()
    if not m or m == "inherit":
        return "inherit"
    if m in ("opus", "sonnet", "haiku", "fable"):
        return m
    for fam in ("opus", "sonnet", "haiku", "fable"):
        if f"claude-{fam}" in m or m.startswith(fam):
            return fam
    return ""


def _downmap_effort(family: str, effort: str):
    """Clamp `effort` to what `family` supports → (effort_or_None, changed).

    Unknown/`inherit` family → leave the effort untouched (we don't reason
    about models we don't know). A family with no effort control drops it.
    """
    eff = (effort or "").strip().lower()
    if not eff:
        return None, False
    supported = MODEL_EFFORT_SUPPORT.get(family)
    if supported is None:                 # unknown / inherit → don't touch
        return eff, False
    if not supported:                     # e.g. haiku → no effort control
        return None, True
    if eff in supported:
        return eff, False
    if eff not in EFFORT_ORDER:           # non-standard value → leave as-is
        return eff, False
    for j in range(EFFORT_ORDER.index(eff), -1, -1):   # highest supported ≤ req
        if EFFORT_ORDER[j] in supported:
            return EFFORT_ORDER[j], True
    return None, True


# Memo for `_effort_ceilings()`, keyed by the raw env value so a changed
# override still rebuilds. `(raw, table)` or None.
_EFFORT_CEILING_MEMO = None


def _effort_ceilings() -> dict:
    """MODEL_EFFORT_CEILING with the WISE_EFFORT_CEILING override applied.

    Value shape: `<model>=<level>` pairs, comma-separated; `<model>=off`
    removes one entry, a bare `off` disables every ceiling. Junk pairs are
    ignored rather than raised — a typo in an env var must not kill a run.

    The merged table is memoized per raw env value — every resolution in a
    run reads it, and re-parsing per lookup buys nothing. Callers must
    treat the result as read-only.
    """
    global _EFFORT_CEILING_MEMO
    raw = (os.environ.get("WISE_EFFORT_CEILING") or "").strip()
    if _EFFORT_CEILING_MEMO is not None and _EFFORT_CEILING_MEMO[0] == raw:
        return _EFFORT_CEILING_MEMO[1]
    table = dict(MODEL_EFFORT_CEILING)
    if not raw:
        _EFFORT_CEILING_MEMO = (raw, table)
        return table
    if raw.lower() == "off":
        _EFFORT_CEILING_MEMO = (raw, {})
        return _EFFORT_CEILING_MEMO[1]
    for pair in raw.split(","):
        key, sep, level = pair.partition("=")
        key, level = key.strip().lower(), level.strip().lower()
        if not sep or not key:
            continue
        if level in ("off", "none", ""):
            table.pop(key, None)
        elif level in EFFORT_ORDER:
            table[key] = level
    _EFFORT_CEILING_MEMO = (raw, table)
    return table


def _is_snapshot_of(model: str, key: str) -> bool:
    """Whether `model` is a dated snapshot of the base id `key`.

    A snapshot is the base id plus `-YYYYMMDD` and nothing else. The date
    suffix is what makes the match safe: a bare prefix test would hand
    `claude-opus-5`'s ceiling to `claude-opus-50-20270101` (a different
    model) and to `claude-opus-5-1` (a version bump whose ceiling is its
    own call, not an inherited one).
    """
    if not model.startswith(key + "-"):
        return False
    suffix = model[len(key) + 1:]
    return len(suffix) == 8 and suffix.isdigit()


def _effort_ceiling(model: str) -> str:
    """The policy ceiling for `model`, or '' when it has none.

    Exact id/alias match wins; otherwise the longest `claude-…` key this
    model is a DATED SNAPSHOT of, so `claude-opus-5-20260401` inherits
    `claude-opus-5`'s ceiling while a different model id does not.
    """
    m = (model or "").strip().lower()
    if not m or m == "inherit":
        return ""
    table = _effort_ceilings()
    if m in table:
        return table[m]
    best, ceiling = "", ""
    for key, level in table.items():
        if (key.startswith("claude-") and len(key) > len(best)
                and _is_snapshot_of(m, key)):
            best, ceiling = key, level
    return ceiling


def _cap_effort(model: str, effort: str):
    """Clamp `effort` to `model`'s policy ceiling → (effort, changed)."""
    eff = (effort or "").strip().lower()
    ceiling = _effort_ceiling(model)
    if not eff or eff not in EFFORT_ORDER or ceiling not in EFFORT_ORDER:
        return effort, False
    if EFFORT_ORDER.index(eff) <= EFFORT_ORDER.index(ceiling):
        return effort, False
    return ceiling, True


def _low_profile_model(model: str, family: str) -> str:
    """The model the low-profile Opus rule dispatches for `model`, or ''.

    Non-empty only for an Opus-family pin that is not already Opus 4.8
    (exact id or a dated snapshot of it) — `inherit`, sonnet, haiku,
    fable, and unknown families are untouched.
    """
    if family != "opus":
        return ""
    m = (model or "").strip().lower()
    if m == LOW_PROFILE_OPUS_MODEL or _is_snapshot_of(m, LOW_PROFILE_OPUS_MODEL):
        return ""
    return LOW_PROFILE_OPUS_MODEL


def _resolve_model_dict(pinned: str, effort: str = "",
                        profile: str = "") -> dict:
    """Resolve a pinned model+effort against availability + capability.

    Returns `{model, effort, fell_back, reason, next_fallback}`: substitutes a
    known-retired id for its maintained alias, applies the low-profile Opus
    rule when `profile` is `low` (LOW_PROFILE_OPUS_MODEL — Opus 5 is never
    dispatched under `low`), clamps the effort to what the resolved model
    supports and then to that model's policy ceiling (MODEL_EFFORT_CEILING —
    e.g. Opus 5 tops out at `high`), and hands back `next_fallback` (the
    alias to retry with if the *live* dispatch still reports the model
    unavailable). `reason` is the user-facing why, surfaced in chat + the
    step log.
    """
    pinned = (pinned or "").strip()
    effort = (effort or "").strip()
    profile = (profile or "").strip().lower()
    reasons: list[str] = []
    model = pinned or "inherit"
    fell_back = False

    if pinned in RETIRED_MODELS:
        repl, state = RETIRED_MODELS[pinned]
        reasons.append(f"{pinned} is {state}; using {repl}")
        model, fell_back = repl, True

    family = _model_family(model)
    if profile == "low":
        low_model = _low_profile_model(model, family)
        if low_model:
            reasons.append(
                f"low profile: {model}→{low_model} (Opus 5 is never used at low)")
            model = low_model
    eff_out, changed = _downmap_effort(family, effort)
    if changed:
        if eff_out is None:
            reasons.append(f"{model} has no effort control; effort '{effort}' dropped")
        else:
            reasons.append(
                f"effort {effort}→{eff_out} ({model} capability ceiling)")

    if eff_out:
        capped, lowered = _cap_effort(model, eff_out)
        if lowered:
            reasons.append(
                f"effort {eff_out}→{capped} ({model} policy ceiling)")
            eff_out = capped

    return {
        "model": model,
        "effort": eff_out,
        "fell_back": fell_back,
        "reason": "; ".join(reasons) or None,
        "next_fallback": MODEL_TIER_NEXT.get(family),
    }


def cmd_resolve_model(pinned: str, effort: str = "",
                      profile: str = "") -> int:
    """Resolve a single pinned model+effort; emit the JSON to stdout.

    The conductor calls this before dispatching a single-agent step. For a
    multi-agent step it calls `resolve-team` instead (which resolves every
    member's model through this same logic). `profile` is the run's
    budget level (`--profile`); `low` applies the low-profile Opus rule.
    """
    profile = (profile or "").strip().lower()
    if profile and profile not in PROFILE_LEVELS:
        print(f"INVALID:profile-level:{profile}", file=sys.stderr)
        return 2
    print(json.dumps(_resolve_model_dict(pinned, effort, profile), indent=2))
    return 0


def _roster_names() -> set:
    """Set of roster role names (for `resolve-team` validation)."""
    return {a["name"] for a in _roster_agents()}


def _normalize_member(item) -> dict:
    """One `agent:` list item → `{role, lead, model, effort}`.

    A bare string is a role name (knobs inherited from the step level); a dict
    carries optional per-member `lead` / `model` / `effort` overrides.
    """
    if isinstance(item, str):
        return {"role": item.strip(), "lead": False, "model": "", "effort": ""}
    if isinstance(item, dict):
        return {
            "role": str(item.get("role") or "").strip(),
            "lead": bool(item.get("lead")),
            "model": str(item.get("model") or "").strip(),
            "effort": str(item.get("effort") or "").strip(),
        }
    return {"role": "", "lead": False, "model": "", "effort": ""}


def cmd_resolve_team(def_path: str, step_id: str,
                     model_override: str = "", effort_override: str = "",
                     team_mode: str = "full", profile: str = "") -> int:
    """Resolve a prompt step's `agent:` into a normalized, model-resolved team.

    Reads the step's `agent:` / `model:` / `effort:` from the definition and
    emits JSON:
      `{mode, lead, members:[{role,lead,model,effort,reason,fell_back,next_fallback}], errors}`

    `model_override` / `effort_override` (the `--model` / `--effort` CLI
    flags) carry a run-level tuning choice made by the user at pre-flight.
    A non-empty override wins over BOTH the member-level and step-level
    pins — the user asked for the whole step (team included) on that
    tier — and still goes through `_resolve_model_dict`, so retired-id
    substitution and the effort clamp apply to the override too. The
    substitution is surfaced via each member's `reason`.

    `profile` (the `--profile` CLI flag) is the run's budget level; `low`
    applies the low-profile Opus rule to every member (Opus-family pins
    and overrides alike resolve to LOW_PROFILE_OPUS_MODEL, never Opus 5).

    `mode` ∈ {`unset`, `off`, `auto`, `single`, `team`}:
    - `unset`  — no `agent:`; the conductor applies the workflow `agents:` policy.
    - `off`    — plain `general-purpose` subagent.
    - `auto`   — returned as-is for the conductor to route (needs prompt-intent
      matching against the roster).
    - `single` — one concrete member (scalar role, or a one-item list).
    - `team`   — two or more members dispatched together, conductor-synthesized.

    Every member's model is run through `resolve-model`; per-member `model` /
    `effort` override the step-level ones, and a bare-string member inherits
    them. Validates each role against the roster and that at most one member is
    the lead. `errors` is non-empty when the conductor should surface a problem.
    """
    definition = load_yaml(Path(def_path))
    step = _step_by_id(definition.get("steps") or [], step_id) or {}
    raw = step.get("agent")
    step_model = str(step.get("model") or "").strip()
    step_effort = str(step.get("effort") or "").strip()
    errors: list[str] = []
    profile = (profile or "").strip().lower()
    if profile and profile not in PROFILE_LEVELS:
        errors.append(f"--profile: unknown value '{profile}' (low|medium|max)")
        profile = ""

    if raw is None or (isinstance(raw, str) and not raw.strip()):
        mode, items = "unset", []
    elif isinstance(raw, bool):
        # YAML 1.1 coerces unquoted off/no → False and on/yes → True. Only
        # `off` is a valid policy keyword, so False means the author wrote
        # `agent: off` (or `no`); True (`on`/`yes`) is a mistake.
        if raw is False:
            mode, items = "off", []
        else:
            mode, items = "unset", []
            errors.append("agent: `on`/`yes`/`true` is not valid (use a role, a list, `auto`, or `off`)")
    elif isinstance(raw, str):
        kw = raw.strip().lower()
        mode, items = (kw, []) if kw in ("auto", "off") else ("single", [raw])
    elif isinstance(raw, list):
        mode, items = "team", raw
    else:
        mode, items = "unset", []
        errors.append(f"agent: unexpected type {type(raw).__name__}")

    members = [_normalize_member(it) for it in items]
    if mode == "team" and len(members) == 1:   # one-item list → simple one-Task path
        mode = "single"

    roster = _roster_names()
    lead = None
    out_members: list[dict] = []
    for m in members:
        role = m["role"]
        if not role:
            errors.append("agent: list item missing a role")
            continue
        if role in ("auto", "off"):
            errors.append(f"'{role}' is a policy keyword; not valid as a team member")
        elif roster and role not in roster:
            errors.append(f"unknown role '{role}' (not in roster)")
        if m["lead"]:
            if lead:
                errors.append(f"multiple leads ({lead}, {role}); only one allowed")
            else:
                lead = role
        pin_model = model_override or m["model"] or step_model
        pin_effort = effort_override or m["effort"] or step_effort
        rm = _resolve_model_dict(pin_model, pin_effort, profile)
        reason = rm["reason"]
        if model_override or effort_override:
            reason = f"run tuning override; {reason}" if reason else "run tuning override"
        out_members.append({
            "role": role, "lead": m["lead"],
            "model": rm["model"], "effort": rm["effort"],
            "reason": reason, "fell_back": rm["fell_back"],
            "next_fallback": rm["next_fallback"],
        })

    result: dict = {"mode": mode, "lead": lead, "members": out_members,
                    "errors": errors}
    if team_mode not in ("full", "solo"):
        errors.append(f"--team-mode: unknown value '{team_mode}' (full|solo)")
    elif team_mode == "solo" and mode == "team" and out_members:
        # Budget-profile collapse: keep only the lead (declared lead, else
        # the first member) and demote to a single dispatch. Additive
        # `collapsed` key so full-mode consumers see an unchanged shape.
        keep = next((m for m in out_members if m["lead"]), out_members[0])
        dropped = [m["role"] for m in out_members if m is not keep]
        note = "team collapsed to lead (solo mode)"
        if not keep["lead"]:
            note += "; no declared lead — first member kept"
        keep["reason"] = f"{keep['reason']}; {note}" if keep["reason"] else note
        result["mode"] = "single"
        result["lead"] = keep["role"] if keep["lead"] else None
        result["members"] = [keep]
        result["collapsed"] = {"from": len(out_members), "dropped": dropped}
    print(json.dumps(result, indent=2))
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
    cap = _env_positive_int("WISE_RUN_HISTORY_CAP", RUN_HISTORY_CAP_DEFAULT)

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
    to_delete = term[max(0, cap - len(non_term)):]

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
    """Emit non-terminal runs in this workspace claiming `session_id`.

    One tab-separated line per match:
        <run-id>\t<workflow>\t<status>\t<last_activity_at>\t<fresh|stale>

    The freshness flag is the genuine-conflict signal. `fresh` means the
    run checked in within WISE_SESSION_STALE_SECS — the live conductor is
    interrupting an in-flight run, worth a prompt. `stale` means the run's
    activity froze long ago (abandoned mid-flight) — not a real conflict;
    callers must not block on it. A missing/unparseable timestamp counts
    as stale.
    """
    runs_root = wise_runs_root_for_cwd()
    if not runs_root.is_dir():
        return 0
    stale_after = _env_positive_int(
        "WISE_SESSION_STALE_SECS", SESSION_STALE_SECS_DEFAULT)
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
        last = state.get("last_activity_at")
        fresh = _session_is_fresh(last, stale_after)
        print(f"{state.get('run_id', child.name)}\t"
              f"{state.get('workflow_name', '?')}\t"
              f"{state.get('status', '?')}\t"
              f"{last or '?'}\t"
              f"{'fresh' if fresh else 'stale'}")
    return 0


# ---------- supervised execution (watchdog) ---------------------------------

def _read_heartbeat(path: Path) -> str | None:
    """The first whitespace-delimited token of a worker heartbeat file — its
    ISO `%Y-%m-%dT%H:%M:%SZ` activity stamp — or None when the file is
    unreadable or empty. Any trailing `phase=…`/`task=…` annotations are
    ignored here; only the stamp drives freshness.
    """
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    if not text:
        return None
    return text.split()[0]


def cmd_worker_heartbeat(run_dir: str, name: str, phase: str, task: str) -> int:
    """Refresh one supervised worker's heartbeat: write the current UTC stamp
    (plus optional phase/task annotations) to `<run_dir>/workers/<name>.hb`.

    A worker calls this as the first action of every turn and after each
    significant tool call, so the supervisor can tell a live worker from a
    hung one. Going through this subcommand (rather than each worker crafting
    its own `date`/redirect one-liner) keeps the format identical to
    `utc_now()` so `_session_is_fresh()` parses it unchanged.

    Defense-in-depth: `name` is interpolated straight into the heartbeat
    path, so a value containing `/` or `..` could escape
    `<run_dir>/workers/`. Validated as a safe single path component before
    the path is built, rather than trusting the caller (this runs
    unattended, same rationale as `cmd_write_log`'s `step_id` check).
    """
    if not re.match(r"^[A-Za-z0-9_-]+\Z", name):
        print(f"INVALID:worker-name:{name!r} (must be a bare "
              f"alphanumeric/_/- token)", file=sys.stderr)
        return 2
    workers_dir = Path(run_dir) / "workers"
    workers_dir.mkdir(parents=True, exist_ok=True)
    line = utc_now()
    if phase:
        line += f"\tphase={phase}"
    if task:
        line += f"\ttask={task}"
    hb_path = workers_dir / f"{name}.hb"
    # A PID-scoped name isn't enough — same-process concurrent heartbeat
    # calls for the same worker would share it and could unlink/replace
    # each other's tmp file. mkstemp gives every call its own file, same
    # as the other atomic writers in this module.
    fd, tmp_name = tempfile.mkstemp(prefix=f"{name}.hb.", suffix=".tmp", dir=workers_dir)
    tmp = Path(tmp_name)
    try:
        try:
            fh = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with fh:
            fh.write(line + "\n")
        os.replace(tmp, hb_path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return 0


def cmd_stale_workers(run_dir: str, expected: str) -> int:
    """Emit the supervised workers that look hung — SILENT when all are fresh.

    Reads heartbeat files under `<run_dir>/workers/<name>.hb` (see
    `cmd_worker_heartbeat`) and classifies each against WISE_WORKER_STALE_SECS.
    One tab-separated line per problem worker — and ONLY problem workers, so
    this is safe to run on a Monitor poll loop (the "emit only the lines you'd
    act on" rule); a fully-healthy wave prints nothing:

        <name>\t<last-hb-or-NONE>\t<stale|missing>\t<age-secs-or-?>

    `stale`   = has a heartbeat file but the stamp is older than the threshold
                (or unparseable/empty — treated as long-silent).
    `missing` = a name in `expected` with no heartbeat file at all (the worker
                never checked in once — spawned-but-dead, distinct from stale).

    `expected` is a comma-separated list of the worker names the conductor
    spawned this wave; pass "" to report purely on whatever .hb files exist.
    """
    stale_after = _env_positive_int(
        "WISE_WORKER_STALE_SECS", WORKER_STALE_SECS_DEFAULT)
    workers_dir = Path(run_dir) / "workers"
    now = datetime.now(timezone.utc)

    def _age(iso_ts: str | None) -> str:
        if not iso_ts:
            return "?"
        try:
            dt = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
        except ValueError:
            return "?"
        return str(int((now - dt).total_seconds()))

    seen: set[str] = set()
    if workers_dir.is_dir():
        for hb in sorted(workers_dir.glob("*.hb")):
            name = hb.stem
            seen.add(name)
            stamp = _read_heartbeat(hb)
            if _session_is_fresh(stamp, stale_after):
                continue
            print(f"{name}\t{stamp or 'NONE'}\tstale\t{_age(stamp)}")

    for raw in (expected or "").split(","):
        name = raw.strip()
        if name and name not in seen:
            print(f"{name}\tNONE\tmissing\t?")
    return 0


def cmd_supervise_config() -> int:
    """Emit the supervisor's resolved knobs as one JSON object so the conductor
    reads them in a single shell call instead of parsing env itself (mirrors
    `resolve-model`'s emit-JSON shape). See the WORKER_*_DEFAULT constants.
    """
    print(json.dumps({
        "stale_secs": _env_positive_int(
            "WISE_WORKER_STALE_SECS", WORKER_STALE_SECS_DEFAULT),
        "poll_secs": _env_positive_int(
            "WISE_WORKER_POLL_SECS", WORKER_POLL_SECS_DEFAULT),
        "max_nudges": _env_positive_int(
            "WISE_WORKER_MAX_NUDGES", WORKER_MAX_NUDGES_DEFAULT),
        "max_respawns": _env_positive_int(
            "WISE_WORKER_MAX_RESPAWNS", WORKER_MAX_RESPAWNS_DEFAULT),
    }))
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


# ---------- apply-worktree-include ------------------------------------------

def cmd_apply_worktree_include(repo_root: str, worktree_dir: str) -> int:
    """Copy `.worktreeinclude`-listed files from a base repo into a new worktree.

    `git worktree add` checks out only TRACKED files; the untracked / gitignored
    artifacts a working tree needs to actually run (`.env`, local config, build
    caches) do not come along. This reads the gitignore-syntax `.worktreeinclude`
    file at `repo_root` and copies the matching files into `worktree_dir`.

    Matching is delegated to git itself for full gitignore semantics:

        git -C <repo_root> ls-files -z --others --ignored \
            --directory --no-empty-directory \
            --exclude-from=<repo_root>/.worktreeinclude

    `--others` limits output to UNTRACKED files, so tracked files (already in the
    checkout) are never touched — the safety guarantee. With no `--exclude-standard`
    the only exclude source is `.worktreeinclude`, so output is exactly the paths
    its patterns match. `--directory` collapses a fully-ignored dir to `dir/` so we
    copy it once via copytree. Existing files in the worktree are OVERWRITTEN.

    Best-effort and graceful: a missing file → no-op; a non-git dir or git error →
    logged and skipped; a listed path that vanished → skipped. ALWAYS returns 0 so
    it never aborts a run (mirrors the conductor's graceful worktree fallback).
    """
    root = Path(repo_root)
    dest_root = Path(worktree_dir)
    inc = root / ".worktreeinclude"
    if not inc.is_file():
        print("worktree-include: no .worktreeinclude — nothing to copy",
              file=sys.stderr)
        return 0

    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--others", "--ignored",
             "--directory", "--no-empty-directory", f"--exclude-from={inc}"],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"worktree-include: git ls-files failed ({exc}); skipping",
              file=sys.stderr)
        return 0

    entries = [e for e in proc.stdout.split("\0") if e]
    try:
        root_res = root.resolve()
        dest_res = dest_root.resolve()
    except OSError as exc:
        print(f"worktree-include: cannot resolve paths ({exc}); skipping",
              file=sys.stderr)
        return 0

    copied = skipped = 0
    for rel in entries:
        src = root / rel
        dst = dest_root / rel
        try:
            # Stay inside repo_root / worktree_dir — never follow a `..` escape.
            src.resolve().relative_to(root_res)
            dst.resolve().relative_to(dest_res)
        except (ValueError, OSError):
            print(f"worktree-include: skip out-of-tree path {rel!r}",
                  file=sys.stderr)
            skipped += 1
            continue
        if not src.exists():
            skipped += 1
            continue
        try:
            if rel.endswith("/") or src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            copied += 1
        except OSError as exc:
            print(f"worktree-include: failed to copy {rel!r} ({exc})",
                  file=sys.stderr)
            skipped += 1

    print(f"worktree-include: copied {copied} (skipped {skipped})",
          file=sys.stderr)
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
    p = sub.add_parser("get-tuning"); p.add_argument("def_path")
    p = sub.add_parser("get-step-select"); p.add_argument("def_path")
    p = sub.add_parser("get-profiles"); p.add_argument("def_path")
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
    p = sub.add_parser("profile-set"); p.add_argument("level")
    sub.add_parser("profile-get")
    p = sub.add_parser("session-path"); p.add_argument("session_id")
    p = sub.add_parser("session-label")
    p.add_argument("run_id"); p.add_argument("workflow_name")
    p = sub.add_parser("find-runs-by-session"); p.add_argument("session_id")

    p = sub.add_parser("worker-heartbeat")
    p.add_argument("run_dir"); p.add_argument("name")
    p.add_argument("phase", nargs="?", default="")
    p.add_argument("task", nargs="?", default="")
    p = sub.add_parser("stale-workers")
    p.add_argument("run_dir"); p.add_argument("expected", nargs="?", default="")
    sub.add_parser("supervise-config")

    sub.add_parser("list-defs")
    sub.add_parser("list-agents")

    p = sub.add_parser("resolve-model")
    p.add_argument("pinned")
    p.add_argument("effort", nargs="?", default="")
    p.add_argument("--profile", default="", dest="profile",
                   help="run budget level (low|medium|max); low applies the low-profile Opus rule")

    p = sub.add_parser("resolve-team")
    p.add_argument("def_path"); p.add_argument("step_id")
    p.add_argument("--model", default="", dest="model_override")
    p.add_argument("--effort", default="", dest="effort_override")
    p.add_argument("--profile", default="", dest="profile",
                   help="run budget level (low|medium|max); low applies the low-profile Opus rule")
    p.add_argument("--team-mode", default="full", dest="team_mode",
                   choices=("full", "solo"))

    sub.add_parser("list-resumable-runs")
    sub.add_parser("prune-runs")

    p = sub.add_parser("apply-worktree-include")
    p.add_argument("repo_root"); p.add_argument("worktree_dir")

    args = parser.parse_args()

    dispatch = {
        "new-ulid": lambda: cmd_new_ulid(),
        "locate-def": lambda: cmd_locate_def(args.name),
        "probe-requires": lambda: cmd_probe_requires(args.def_path),
        "init-state": lambda: cmd_init_state(args.def_path, args.run_dir, args.run_id, args.ctx_json),
        "start-run": lambda: cmd_start_run(args.state_path, args.ctx_json),
        "runs-root": lambda: cmd_runs_root(),
        "get-preflight": lambda: cmd_get_preflight(args.def_path),
        "get-tuning": lambda: cmd_get_tuning(args.def_path),
        "get-step-select": lambda: cmd_get_step_select(args.def_path),
        "get-profiles": lambda: cmd_get_profiles(args.def_path),
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
        "profile-set": lambda: cmd_profile_set(args.level),
        "profile-get": lambda: cmd_profile_get(),
        "session-path": lambda: cmd_session_path(args.session_id),
        "session-label": lambda: cmd_session_label(args.run_id, args.workflow_name),
        "find-runs-by-session": lambda: cmd_find_runs_by_session(args.session_id),
        "worker-heartbeat": lambda: cmd_worker_heartbeat(args.run_dir, args.name, args.phase, args.task),
        "stale-workers": lambda: cmd_stale_workers(args.run_dir, args.expected),
        "supervise-config": lambda: cmd_supervise_config(),
        "list-defs": lambda: cmd_list_defs(),
        "list-agents": lambda: cmd_list_agents(),
        "resolve-model": lambda: cmd_resolve_model(
            args.pinned, args.effort, args.profile),
        "resolve-team": lambda: cmd_resolve_team(
            args.def_path, args.step_id,
            args.model_override, args.effort_override, args.team_mode,
            args.profile),
        "list-resumable-runs": lambda: cmd_list_resumable_runs(),
        "prune-runs": lambda: cmd_prune_runs(),
        "apply-worktree-include": lambda: cmd_apply_worktree_include(
            args.repo_root, args.worktree_dir),
    }
    return dispatch[args.cmd]()


if __name__ == "__main__":
    sys.exit(main() or 0)
