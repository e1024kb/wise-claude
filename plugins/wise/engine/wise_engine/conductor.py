"""Conductor state: setup, ledger, base-branch rule, child briefs, verification.

The `/wise-conductor` skill runs in the main conversation and starts one child
session per ticket or topic. This module holds everything that must outlive the
context window (L12) and the pure rules the skill applies, so they are tested
code instead of prose. Stdlib only: `wise-helpers.py conductor ...` runs it
before `/wise-init`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from .paths import cwd_slug, wise_data_root

Json = dict[str, Any]

STATE_VERSION = 1
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9#:/._?=&%+-]{0,199}$")
GIT_REF_RE = re.compile(r"^(?!.*\.\.)(?!/)(?!.*//)[A-Za-z0-9._/-]{1,200}(?<![./])$")
PLACEHOLDER_RE = re.compile(r"\{([a-z]+)\}")
RELEASE_PLACEHOLDERS = {"version", "raw", "major", "minor", "patch"}

# One entry per setup question: allowed values (None = free text) and default.
SETUP_FIELDS: dict[str, tuple[tuple[str, ...] | None, str]] = {
    "base_rule": (("default", "fixed", "release"), "default"),
    "base_fixed": (None, ""),
    "release_field": (None, "fixVersion"),
    "release_pattern": (None, "release-{version}"),
    "release_missing": (("default", "stop"), "default"),
    "merge_policy": (("never", "green"), "never"),
    "pr_state": (("draft-then-ready", "ready"), "draft-then-ready"),
    "questions": (("none", "allowed"), "none"),
    "overlap": (("follow-base", "stop"), "follow-base"),
    "auth_fallback": (("stop", "direct"), "stop"),
    "tracker_writeback": (("no", "yes"), "no"),
}
CHILD_KINDS = ("ticket", "topic")
CHILD_STATES = ("starting", "running", "waiting", "ready", "merged", "closed", "failed")
CHILD_FIELDS = {
    "session_id",
    "name",
    "title",
    "worktree",
    "branch",
    "base",
    "base_source",
    "pr",
    "state",
    "ci",
    "open_threads",
    "manual_qa",
    "notes",
    "run_id",
}
# The ticket-auto pre-flight answers every child reuses. `worktree`, `input.*`
# and `step-select` are per child: the payload sets them, a stored copy is dropped.
ANSWER_ID_RE = re.compile(
    r"^(tuning-scope|harness\.[a-z0-9_-]+|permissions\.[a-z0-9_-]+|model\.[a-z0-9_-]+|effort\.[a-z0-9_-]+)$"
)
DESKTOP_START = "mcp__ccd_session__start_session"
DESKTOP_MESSAGE = ("SendMessage", "mcp__ccd_session_mgmt__send_message")
FALLBACK_NOTICE = (
    "Claude Desktop session tools are not available: children run as background "
    "agents or wise engine runs, and the sidebar tree is not shown."
)


class ConductorError(ValueError):
    """Invalid input; the message is safe to show the user."""


# -- paths and persistence ---------------------------------------------------


def conductor_root(cwd: str | Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    return wise_data_root(env) / "conductor" / cwd_slug(cwd)


def check_id(conductor_id: str) -> str:
    if not ID_RE.match(conductor_id or ""):
        raise ConductorError(f"invalid conductor id {conductor_id!r}: use [A-Za-z0-9._-], max 80")
    return conductor_id


def state_path(
    conductor_id: str, cwd: str | Path | None = None, env: Mapping[str, str] | None = None
) -> Path:
    return conductor_root(cwd, env) / check_id(conductor_id) / "state.json"


def project_rules_path(cwd: str | Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    return conductor_root(cwd, env) / "project.json"


def _write_json(path: Path, value: Json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load(
    conductor_id: str, cwd: str | Path | None = None, env: Mapping[str, str] | None = None
) -> Json:
    path = state_path(conductor_id, cwd, env)
    if not path.is_file():
        raise ConductorError(f"no conductor state for {conductor_id!r} in this workspace")
    return json.loads(path.read_text())


def save(state: Json, env: Mapping[str, str] | None = None) -> Path:
    state["updated"] = int(time.time())
    path = state_path(state["id"], state["cwd"], env)
    _write_json(path, state)
    return path


def latest(cwd: str | Path | None = None, env: Mapping[str, str] | None = None) -> Json | None:
    """The most recently updated conductor of this workspace, for resume."""
    root = conductor_root(cwd, env)
    best: Json | None = None
    for path in root.glob("*/state.json"):
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if best is None or state.get("updated", 0) > best.get("updated", 0):
            best = state
    return best


# -- setup -------------------------------------------------------------------


def validate_setup(raw: Mapping[str, Any]) -> Json:
    """Fill defaults and validate; raises ConductorError naming every bad field."""
    unknown = sorted(set(raw) - set(SETUP_FIELDS))
    errors = [f"unknown setup field {name!r}" for name in unknown]
    setup: Json = {}
    for name, (allowed, default) in SETUP_FIELDS.items():
        value = str(raw.get(name, "") or "").strip() or default
        if allowed is not None and value not in allowed:
            errors.append(f"{name}: {value!r} is not one of {', '.join(allowed)}")
        setup[name] = value
    if setup["base_rule"] == "fixed":
        if not setup["base_fixed"]:
            errors.append("base_fixed: required when base_rule is fixed")
        elif not GIT_REF_RE.match(setup["base_fixed"]):
            errors.append(f"base_fixed: {setup['base_fixed']!r} is not a valid branch name")
    if setup["base_rule"] == "release":
        errors.extend(_pattern_errors(setup["release_pattern"]))
        if not re.match(r"^[A-Za-z0-9_.-]{1,64}$", setup["release_field"]):
            errors.append(f"release_field: {setup['release_field']!r} is not a field name")
    if errors:
        raise ConductorError("; ".join(errors))
    return setup


def _pattern_errors(pattern: str) -> list[str]:
    names = set(PLACEHOLDER_RE.findall(pattern))
    errors = [
        f"release_pattern: unknown placeholder {{{n}}}"
        for n in sorted(names - RELEASE_PLACEHOLDERS)
    ]
    if not names:
        errors.append("release_pattern: needs a placeholder, for example release-{version}")
    literal = PLACEHOLDER_RE.sub("x", pattern)
    if not GIT_REF_RE.match(literal):
        errors.append(f"release_pattern: {pattern!r} does not give a valid branch name")
    return errors


def validate_answers(answers: Mapping[str, Any]) -> Json:
    """Keep the ticket-auto tuning and provider answers; drop per-child ids."""
    out: Json = {}
    for key, value in answers.items():
        if not ANSWER_ID_RE.match(key):
            continue
        if isinstance(value, list):
            value = ",".join(str(v) for v in value)
        if not isinstance(value, str) or not value.strip():
            raise ConductorError(f"answers: {key} needs a non-empty string")
        out[key] = value.strip()
    return out


def set_answers(state: Json, answers: Mapping[str, Any]) -> Json:
    tuning = validate_answers(answers)
    if not tuning:
        raise ConductorError("answers: no harness, model, effort or permission answer to store")
    state["answers"] = tuning
    return tuning


def init(
    conductor_id: str,
    cwd: str | Path,
    setup: Mapping[str, Any],
    answers: Mapping[str, Any] | None = None,
    mode: str = "fallback",
    prompt: str = "",
    env: Mapping[str, str] | None = None,
) -> Json:
    """Create or replace the setup of a conductor; the ledger is kept on re-init."""
    if mode not in ("desktop", "fallback"):
        raise ConductorError(f"mode: {mode!r} is not desktop or fallback")
    clean = validate_setup(setup)
    tuning = validate_answers(answers or {})
    path = state_path(conductor_id, cwd, env)
    previous = json.loads(path.read_text()) if path.is_file() else {}
    now = int(time.time())
    state: Json = {
        "version": STATE_VERSION,
        "id": conductor_id,
        "cwd": str(Path(cwd).resolve()),
        "created": previous.get("created", now),
        "mode": mode,
        "prompt": prompt or previous.get("prompt", ""),
        "setup": clean,
        "answers": tuning or previous.get("answers", {}),
        "children": previous.get("children", []),
        "expected_checks": previous.get("expected_checks", {}),
    }
    save(state, env)
    _write_json(project_rules_path(cwd, env), project_rules(state))
    return state


def project_rules(state: Json) -> Json:
    """Per-project rules a later conductor of this workspace starts from (L12)."""
    setup = state["setup"]
    return {
        "conductor": state["id"],
        "base_rule": setup["base_rule"],
        "base_fixed": setup["base_fixed"],
        "release_field": setup["release_field"],
        "release_pattern": setup["release_pattern"],
        "merge_policy": setup["merge_policy"],
        "pr_state": setup["pr_state"],
        "worktree_pattern": "../<repo>.<KEY> on branch <KEY> from origin/<base>",
        "expected_checks": state.get("expected_checks", {}),
    }


def setup_from_env(env: Mapping[str, str]) -> Json:
    """Setup answers from a bash step's `WISE_<NAME>` variables."""
    return {
        name: env[f"WISE_{name.upper()}"] for name in SETUP_FIELDS if f"WISE_{name.upper()}" in env
    }


# -- base branch rule (L4) ---------------------------------------------------


def map_release(version: str, pattern: str) -> str:
    """`26.11.0` with `release-{version}` gives `release-26-11-0`."""
    raw = version.strip()
    if not raw:
        raise ConductorError("release: empty version")
    parts = re.split(r"[.]", raw)
    values = {
        "raw": raw,
        "version": "-".join(parts),
        "major": parts[0],
        "minor": parts[1] if len(parts) > 1 else "0",
        "patch": parts[2] if len(parts) > 2 else "0",
    }
    branch = PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), pattern)
    if not GIT_REF_RE.match(branch):
        raise ConductorError(
            f"release: {raw!r} with {pattern!r} gives an invalid branch {branch!r}"
        )
    return branch


def resolve_base(setup: Json, default_branch: str, releases: list[str] | None = None) -> Json:
    """The base branch for one ticket: {base, source} or {error}."""
    rule = setup["base_rule"]
    if rule == "fixed":
        return {"base": setup["base_fixed"], "source": "fixed"}
    if rule == "default":
        return {"base": default_branch, "source": "default"}
    values = sorted({v.strip() for v in releases or [] if v and v.strip()})
    if not values:
        if setup["release_missing"] == "stop":
            return {"error": f"no {setup['release_field']} on the ticket"}
        return {
            "base": default_branch,
            "source": f"default (no {setup['release_field']} on the ticket)",
        }
    branches = sorted({map_release(v, setup["release_pattern"]) for v in values})
    if len(branches) > 1:
        return {"error": f"ambiguous {setup['release_field']}: {', '.join(values)}"}
    return {"base": branches[0], "source": f"{setup['release_field']} {', '.join(values)}"}


# -- ledger ------------------------------------------------------------------


def child_name(ref: str) -> str:
    """A short, stable session handle: `PROJ-12` -> `proj-12` ([a-z0-9-], max 24)."""
    key = ref.rstrip("/").split("/")[-1]
    slug = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")
    return (slug or "child")[:24].rstrip("-")


def find_child(state: Json, ref: str) -> Json | None:
    return next((c for c in state["children"] if c["ref"] == ref), None)


def add_child(state: Json, kind: str, ref: str, title: str = "", /, **fields: Any) -> Json:
    if kind not in CHILD_KINDS:
        raise ConductorError(f"kind: {kind!r} is not ticket or topic")
    if kind == "ticket" and not REF_RE.match(ref):
        raise ConductorError(f"ref: {ref!r} has characters outside [A-Za-z0-9#:/._?=&%+-]")
    if kind == "topic" and not ref.strip():
        raise ConductorError("ref: a topic needs a short label")
    if find_child(state, ref) is not None:
        raise ConductorError(f"{ref} already has a child in this conductor")
    names = {c["name"] for c in state["children"]}
    name, n = child_name(ref), 2
    while name in names:
        name = f"{child_name(ref)[:21]}-{n}"
        n += 1
    child: Json = {
        "kind": kind,
        "ref": ref,
        "name": name,
        "title": (title or ref)[:59],
        "state": "starting",
        "session_id": "",
        "worktree": "",
        "branch": "",
        "base": "",
        "pr": None,
        "ci": None,
        "open_threads": None,
        "manual_qa": [],
        "notes": "",
        "added": int(time.time()),
    }
    state["children"].append(child)
    update_child(state, ref, **fields)
    return child


def update_child(state: Json, ref: str, /, **fields: Any) -> Json:
    child = find_child(state, ref)
    if child is None:
        raise ConductorError(f"{ref} has no child in this conductor")
    unknown = sorted(set(fields) - CHILD_FIELDS)
    if unknown:
        raise ConductorError(f"unknown ledger fields: {', '.join(unknown)}")
    if "state" in fields and fields["state"] not in CHILD_STATES:
        raise ConductorError(f"state: {fields['state']!r} is not one of {', '.join(CHILD_STATES)}")
    if (
        "session_id" in fields
        and fields["session_id"]
        and not re.match(r"^[A-Za-z0-9_-]{1,120}$", fields["session_id"])
    ):
        raise ConductorError("session_id: use the local_... id, never a display name")
    if "pr" in fields and fields["pr"] is not None:
        pr = fields["pr"]
        if isinstance(pr, (int, str)) and str(pr).isdigit():
            fields["pr"] = int(pr)
        else:
            raise ConductorError(f"pr: {pr!r} is not a PR number")
    if "manual_qa" in fields:
        items = fields["manual_qa"]
        fields["manual_qa"] = [str(i) for i in items] if isinstance(items, list) else [str(items)]
    child.update(fields)
    child["changed"] = int(time.time())
    return child


# -- run payload and briefs (L1, L7, L8) -------------------------------------


def ticket_auto_payload(state: Json, child: Json, ticket: Json) -> Json:
    """The exact `wise_run` arguments of one ticket child: every question answered."""
    setup = state["setup"]
    ref = child["ref"]
    if not any(k.startswith(("harness.", "model.")) for k in state.get("answers", {})):
        raise ConductorError(
            "no ticket-auto answers stored: run the ticket-auto pre-flight and set-answers first"
        )
    answers = dict(state.get("answers", {}))
    answers.update(
        {
            "worktree": "new",
            "input.worktree_mode": "new",
            "input.tickets": ref,
            "input.base_branch": child["base"],
            "input.guidance": "",
            # Asked only when the ref fans out (an epic): one child at a time.
            "input.concurrency": "1",
            "input.on_child_failure": "continue",
            "input.repo_paths": "",
        }
    )
    decisions = {
        "merge_policy": "never" if setup["merge_policy"] == "never" else "merge",
        "pr_draft": "yes" if setup["pr_state"] == "draft-then-ready" else "no",
        "overlap": (
            "Follow the convention already on the base branch. Never undo another ticket's fix. "
            "Record each conflict in an Overlap section of the PR body."
            if setup["overlap"] == "follow-base"
            else "Stop and report when another ticket touched the same code."
        ),
        "ambiguous_scope": "Take the smallest reading that meets the acceptance criteria and record it in the PR body.",
        "tracker_writeback": "allowed"
        if setup["tracker_writeback"] == "yes"
        else "forbidden: post nothing to the tracker",
    }
    context_ticket = {
        k: ticket[k]
        for k in ("ref", "title", "body", "url", "state", "parent", "repo")
        if ticket.get(k)
    }
    context_ticket["ref"] = ref
    comments = [str(c) for c in ticket.get("comments") or [] if str(c).strip()]
    if comments:
        # L1: the run gets the full ticket text, comments included.
        body = context_ticket.get("body", "")
        context_ticket["body"] = (
            body + "\n\nComments:\n" + "\n".join(f"- {c}" for c in comments)
        ).strip()
    return {
        "workflow": "ticket-auto",
        "cwd": state["cwd"],
        "answers": answers,
        "inputs": {"tickets": ref, "base_branch": child["base"], "worktree_mode": "new"},
        "context": {"ticket": [context_ticket], "decisions": decisions},
    }


def _ticket_text(ticket: Json) -> str:
    lines = [f"Key: {ticket.get('ref', '')}", f"Title: {ticket.get('title', '')}"]
    if ticket.get("url"):
        lines.append(f"URL: {ticket['url']}")
    lines += ["", "Description:", ticket.get("body") or "(empty)"]
    comments = ticket.get("comments") or []
    lines += ["", "Comments:"] + ([f"- {c}" for c in comments] if comments else ["(none)"])
    return "\n".join(lines)


def build_ticket_brief(
    state: Json,
    child: Json,
    ticket: Json,
    siblings: list[Json] | None = None,
    conductor_session: str = "",
) -> str:
    """The self-contained brief of one ticket child (sections 1-6 of the spec)."""
    setup = state["setup"]
    ref, base = child["ref"], child["base"]
    payload = ticket_auto_payload(state, child, ticket)
    repo = Path(state["cwd"]).name
    unattended = setup["questions"] == "none"
    merge_never = setup["merge_policy"] == "never"
    draft = setup["pr_state"] == "draft-then-ready"
    sibling_lines = [
        f"- {s['ref']}: PR {s.get('pr') or '(none yet)'} on {s.get('base') or '?'} - {s.get('notes') or s.get('title', '')}"
        for s in siblings or []
    ] or ["- None known to the conductor. Search the tracker and the base history yourself."]
    auth = (
        "Stop. Report the AUTH_REQUIRED error and its login command to the conductor once. Do not implement."
        if setup["auth_fallback"] == "stop"
        else "Implement the ticket directly in your own worktree with the same quality bar: plan, tests, 3-lens review, PR, CI."
    )
    parts = [
        f"# Ticket child: {ref}",
        "",
        "You are a ticket child of a wise conductor session. Work on this one ticket only.",
        "",
        "## 1. Ticket (fetched by the conductor; do not refetch through a subagent)",
        "",
        _ticket_text(ticket),
        "",
        "## 2. Worktree",
        "",
        f"Base branch: `{base}` ({child.get('base_source') or setup['base_rule']}).",
        "WARNING: Never edit, stash, reset or check out the main checkout. It can hold uncommitted work.",
        "`ticket-auto` (worktree_mode `new`) fetches the base and creates the worktree. Do not create one for it.",
        "When you implement directly, create your own worktree first:",
        "",
        "```bash",
        f"git fetch origin {base}",
        f"git worktree add ../{repo}.{ref} -b {ref} origin/{base}",
        "```",
        "",
        "## 3. Setup (fixed answers; ask nothing about them)",
        "",
        "Start `ticket-auto` with `wise_run` and exactly these arguments, then follow it with `wise_wait`:",
        "",
        "```json",
        json.dumps(payload, indent=2, sort_keys=True),
        "```",
        "",
        f"- Merge policy: {'never merge. Keep the PR for manual review.' if merge_never else 'merge when green.'}",
        f"- PR state: {'draft during the run, ready for review at the end.' if draft else 'ready for review when opened.'}",
        f"- Questions: {'none. The run is unattended. Use the documented defaults below and record each one in the PR body.' if unattended else 'allowed. Send them to the conductor.'}",
        f"- Tracker write-back: {'allowed.' if setup['tracker_writeback'] == 'yes' else 'forbidden. Post nothing to the tracker.'}",
        f"- If `wise_run` fails with AUTH_REQUIRED: {auth}",
        "- Ambiguous scope: take the smallest reading that meets the acceptance criteria. Record it in the PR body.",
        "",
        "## 4. Overlap",
        "",
        "Before you implement, search the tracker and `git log origin/"
        + base
        + "` for sibling work in the same area.",
        (
            "Follow what is already merged. Never undo another ticket's fix. Record each conflict in an `Overlap` section of the PR body."
            if setup["overlap"] == "follow-base"
            else "When another ticket touched the same code, stop and report it to the conductor."
        ),
        "Sibling PRs the conductor knows in this area:",
        *sibling_lines,
        "",
        "## 5. Definition of done",
        "",
        f"1. One PR on base `{base}`, {'ready for review and unmerged' if merge_never else 'merged after green CI'}.",
        "2. CI green: every expected job ran and passed. A run with missing jobs is not green. Rerun it.",
        "3. WARNING: Do not end your turn while CI runs or while the PR is still a draft.",
        "4. Every fix has a test that fails on the old code. Say so in each reply.",
        "5. End with a short report: what changed, the PR URL, the CI result, and what you did not verify by hand"
        " (keyboard, zoom, screen reader, visual checks). Write `Manual QA: none` only when nothing applies.",
        "",
        "## 6. Conductor",
        "",
        (
            f"Conductor session id: `{conductor_session}`. Your turn-end report reaches it automatically."
            f" Send a message to `{conductor_session}` only when you are blocked."
            if conductor_session
            else "Your final reply is your report to the conductor."
        ),
    ]
    return "\n".join(parts) + "\n"


def build_topic_brief(state: Json, child: Json, brief: str, conductor_session: str = "") -> str:
    parts = [
        f"# Topic child: {child['title']}",
        "",
        "You are a topic child of a wise conductor session. No workflow applies.",
        "",
        brief.strip(),
        "",
        "WARNING: Never edit, stash, reset or check out the main checkout. Use your own worktree for code changes.",
        "End with a short report: what you found or changed, and what you did not verify.",
    ]
    if conductor_session:
        parts += [
            "",
            f"Conductor session id: `{conductor_session}`. Message it only when you are blocked.",
        ]
    return "\n".join(parts) + "\n"


# -- verification (L5, L6) ---------------------------------------------------

BUCKETS = {
    "pass": "pass",
    "fail": "fail",
    "cancel": "fail",
    "pending": "pending",
    "skipping": "skipped",
}


def classify_checks(checks: list[Json], expected: list[str] | None = None) -> Json:
    """Count `gh pr checks --json name,bucket` rows and decide green (L5)."""
    counts = {"pass": 0, "fail": 0, "pending": 0, "skipped": 0}
    names = set()
    for row in checks:
        bucket = BUCKETS.get(str(row.get("bucket", "")).lower(), "pending")
        counts[bucket] += 1
        names.add(str(row.get("name", "")))
    missing = sorted(set(expected or []) - names)
    if counts["fail"]:
        verdict = "red"
    elif counts["pending"]:
        verdict = "pending"
    elif missing or counts["pass"] == 0:
        verdict = "incomplete"
    else:
        verdict = "green"
    return {**counts, "missing": missing, "verdict": verdict, "names": sorted(names)}


def unresolved_threads(graphql: Json) -> list[Json]:
    """Unresolved review threads from the `reviewThreads` GraphQL response."""
    node = graphql.get("data", graphql) or {}
    node = node.get("repository", node) or {}
    node = node.get("pullRequest", node) or {}
    node = node.get("reviewThreads", node) or {}
    rows = []
    for thread in node.get("nodes", []):
        if thread.get("isResolved"):
            continue
        comments = thread.get("comments", {}).get("nodes", [])
        rows.append(
            {
                "thread_id": thread.get("id", ""),
                "comment_ids": [c.get("databaseId") or c.get("id") for c in comments],
                "path": thread.get("path", ""),
                "line": thread.get("line"),
                "author": (comments[0].get("author") or {}).get("login", "") if comments else "",
                "body": comments[0].get("body", "") if comments else "",
            }
        )
    return rows


def verify_child(state: Json, ref: str, pr: Json, checks: list[Json], threads: list[Json]) -> Json:
    """Fold live PR facts into the ledger row; returns the verified view and nudges."""
    child = find_child(state, ref)
    if child is None:
        raise ConductorError(f"{ref} has no child in this conductor")
    base = pr.get("baseRefName", "")
    expected = state.get("expected_checks", {}).get(base)
    ci = classify_checks(checks, expected)
    if ci["verdict"] == "green" and not expected:
        state.setdefault("expected_checks", {})[base] = ci["names"]
    nudges = []
    draft = bool(pr.get("isDraft"))
    pr_state = str(pr.get("state", "")).upper()
    if draft and ci["verdict"] == "pending":
        nudges.append(
            "The PR is a draft and CI is pending. Do not end your turn: wait for CI, then mark it ready."
        )
    if ci["verdict"] == "incomplete":
        nudges.append(
            f"CI is incomplete (missing: {', '.join(ci['missing']) or 'all jobs'}). Rerun the missing jobs."
        )
    if ci["verdict"] == "red":
        nudges.append("CI is red. Fix the failing checks.")
    if child["base"] and base and base != child["base"]:
        nudges.append(f"The PR targets {base}, the ledger expects {child['base']}. Retarget it.")
    if threads:
        nudges.append(f"{len(threads)} review thread(s) are unresolved.")
    if pr_state == "MERGED":
        row_state = "merged"
    elif pr_state == "CLOSED":
        row_state = "closed"
    elif not draft and ci["verdict"] == "green" and not threads:
        row_state = "ready"
    else:
        row_state = "waiting" if not nudges else "running"
    update_child(
        state,
        ref,
        pr=pr.get("number", child.get("pr")),
        state=row_state,
        ci={k: ci[k] for k in ("pass", "fail", "pending", "skipped", "missing", "verdict")},
        open_threads=len(threads),
    )
    merge_state = pr.get("mergeStateStatus", "")
    if state["setup"]["merge_policy"] == "never" and pr_state == "MERGED":
        nudges.append(
            "WARNING: the PR is merged although the merge policy is never. Tell the user."
        )
    return {
        "ref": ref,
        "pr": pr.get("number"),
        "url": pr.get("url", ""),
        "state": row_state,
        "draft": draft,
        "base": base,
        "merge_state": merge_state,
        "ci": ci,
        "open_threads": len(threads),
        "nudges": nudges,
        "done": row_state in ("ready", "merged"),
    }


# -- review-comment round (L10, L11) -----------------------------------------

REVIEW_INSTRUCTIONS = (
    "Check every finding against the code. For a valid finding, fix it with a test that fails "
    "without the fix, and say so in the reply. For an invalid finding, reply with evidence "
    "(file, line, behaviour). Then reply on the thread, resolve it, and wait for green CI. "
    "Do not end your turn while CI runs. Report the thread ids you resolved."
)


def review_fanout(state: Json, threads_by_pr: Mapping[str, list[Json]]) -> list[Json]:
    """One message per owning child for every PR that has unresolved threads."""
    owners = {str(c["pr"]): c for c in state["children"] if c.get("pr") is not None}
    out = []
    for pr, threads in sorted(
        threads_by_pr.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0
    ):
        if not threads:
            continue
        child = owners.get(str(pr))
        if child is None:
            out.append(
                {
                    "pr": int(pr) if str(pr).isdigit() else pr,
                    "owner": None,
                    "threads": threads,
                    "message": "",
                    "error": "no child in the ledger owns this PR",
                }
            )
            continue
        lines = [
            f"Review-comment round for PR {pr} ({child['ref']}): {len(threads)} unresolved thread(s).",
            "",
        ]
        for t in threads:
            where = f"{t['path']}:{t['line']}" if t.get("line") else t.get("path", "")
            ids = ", ".join(str(i) for i in t.get("comment_ids", []))
            body = " ".join(str(t.get("body", "")).split())[:400]
            lines.append(
                f"- thread `{t['thread_id']}` (comments {ids}) at `{where}` by {t.get('author') or '?'}: {body}"
            )
        lines += ["", REVIEW_INSTRUCTIONS]
        update_child(state, child["ref"], open_threads=len(threads), state="running")
        out.append(
            {
                "pr": int(pr) if str(pr).isdigit() else pr,
                "owner": child["ref"],
                "session_id": child.get("session_id", ""),
                "name": child["name"],
                "threads": threads,
                "message": "\n".join(lines),
            }
        )
    return out


# -- runtime mode and status -------------------------------------------------


def detect_mode(tools: list[str]) -> Json:
    """Desktop when the session-start tool and a session messaging tool exist."""
    names = {t.strip() for t in tools if t.strip()}
    desktop = DESKTOP_START in names and any(m in names for m in DESKTOP_MESSAGE)
    if desktop:
        return {"mode": "desktop", "notice": ""}
    return {"mode": "fallback", "notice": FALLBACK_NOTICE}


def status_table(state: Json) -> str:
    rows = [
        "| Child | Kind | Session | Base | PR | State | CI | Threads |",
        "|---|---|---|---|---|---|---|---|",
    ]
    human: list[str] = []
    for c in state["children"]:
        ci = c.get("ci") or {}
        ci_text = (
            f"{ci['verdict']} {ci['pass']}/{ci['fail']}/{ci['pending']}/{ci['skipped']}"
            if ci
            else "-"
        )
        threads = "-" if c.get("open_threads") is None else str(c["open_threads"])
        rows.append(
            f"| {c['ref']} | {c['kind']} | {c['name']} | {c.get('base') or '-'} | "
            f"{c.get('pr') or '-'} | {c['state']} | {ci_text} | {threads} |"
        )
        for item in c.get("manual_qa", []):
            human.append(f"- {c['ref']}: manual QA: {item}")
        if c.get("notes"):
            human.append(f"- {c['ref']}: {c['notes']}")
    text = "\n".join(rows) + "\n\nCI = verdict pass/fail/pending/skipped.\n"
    if human:
        text += "\nNeeds a human:\n" + "\n".join(human) + "\n"
    return text


# -- CLI: `wise-helpers.py conductor <action> ...` ---------------------------


def _read_json(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    text = sys.stdin.read() if value == "-" else Path(value).read_text()
    return json.loads(text) if text.strip() else default


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wise-helpers.py conductor")
    actions = parser.add_subparsers(dest="action", required=True)

    def action(name: str, with_id: bool = True) -> argparse.ArgumentParser:
        sub = actions.add_parser(name)
        if with_id:
            sub.add_argument("--id", required=True)
        sub.add_argument("--cwd", default=".")
        return sub

    actions.add_parser("mode").add_argument("--tools", required=True)
    sub = action("init")
    sub.add_argument("--mode", default="fallback")
    sub.add_argument("--setup-json")
    sub.add_argument("--answers-json")
    sub.add_argument("--from-env", action="store_true")
    sub.add_argument("--prompt", default="")
    action("show")
    action("status")
    action("set-answers").add_argument("--answers-json", required=True)
    action("latest", with_id=False)
    action("rules", with_id=False)
    sub = action("base")
    sub.add_argument("--default-branch", required=True)
    sub.add_argument("--release", action="append", default=[])
    sub = action("add-child")
    sub.add_argument("--kind", required=True)
    sub.add_argument("--ref", required=True)
    sub.add_argument("--title", default="")
    sub.add_argument("--fields-json")
    sub = action("update-child")
    sub.add_argument("--ref", required=True)
    sub.add_argument("--fields-json", required=True)
    for name in ("payload", "brief"):
        sub = action(name)
        sub.add_argument("--ref", required=True)
        sub.add_argument("--ticket-json")
        sub.add_argument("--brief-file")
        sub.add_argument("--siblings-json")
        sub.add_argument("--conductor-session", default="")
    sub = action("verify")
    sub.add_argument("--ref", required=True)
    sub.add_argument("--pr-json", required=True)
    sub.add_argument("--checks-json", required=True)
    sub.add_argument("--threads-json")
    sub = action("fanout")
    sub.add_argument("--threads-json", required=True)
    return parser


def _brief(args: argparse.Namespace, state: Json) -> Any:
    child = find_child(state, args.ref)
    if child is None:
        raise ConductorError(f"{args.ref} has no child in this conductor")
    if child["kind"] == "topic":
        if args.action == "payload":
            raise ConductorError("a topic child runs no workflow")
        text = Path(args.brief_file).read_text() if args.brief_file else child["title"]
        return build_topic_brief(state, child, text, args.conductor_session)
    ticket = _read_json(args.ticket_json, {}) or {}
    if ticket.get("ref") and ticket["ref"] != child["ref"]:
        raise ConductorError(f"ticket {ticket['ref']} does not match child {child['ref']}")
    if not child.get("base"):
        raise ConductorError(f"{child['ref']} has no base branch: resolve it first")
    if args.action == "payload":
        return ticket_auto_payload(state, child, ticket)
    siblings = _read_json(args.siblings_json, []) or []
    return build_ticket_brief(state, child, ticket, siblings, args.conductor_session)


def _run(args: argparse.Namespace, env: Mapping[str, str] | None) -> Any:
    if args.action == "mode":
        return detect_mode(args.tools.split(","))
    if args.action == "latest":
        return latest(args.cwd, env)
    if args.action == "rules":
        path = project_rules_path(args.cwd, env)
        return json.loads(path.read_text()) if path.is_file() else None
    if args.action == "init":
        setup = setup_from_env(os.environ if env is None else env) if args.from_env else {}
        setup.update(_read_json(args.setup_json, {}) or {})
        answers = _read_json(args.answers_json, {}) or {}
        state = init(args.id, args.cwd, setup, answers, args.mode, args.prompt, env)
        return {
            "id": state["id"],
            "path": str(state_path(state["id"], args.cwd, env)),
            "setup": state["setup"],
        }
    state = load(args.id, args.cwd, env)
    if args.action == "show":
        return state
    if args.action == "status":
        return status_table(state)
    if args.action == "base":
        return resolve_base(state["setup"], args.default_branch, args.release)
    if args.action in ("payload", "brief"):
        return _brief(args, state)
    if args.action == "set-answers":
        result: Any = set_answers(state, _read_json(args.answers_json, {}) or {})
    elif args.action == "add-child":
        fields = _read_json(args.fields_json, {}) or {}
        result = add_child(state, args.kind, args.ref, args.title, **fields)
    elif args.action == "update-child":
        result = update_child(state, args.ref, **(_read_json(args.fields_json, {}) or {}))
    elif args.action == "verify":
        threads = unresolved_threads(_read_json(args.threads_json, {}) or {})
        pr, checks = _read_json(args.pr_json, {}), _read_json(args.checks_json, [])
        result = verify_child(state, args.ref, pr, checks, threads)
    else:
        raw = _read_json(args.threads_json, {}) or {}
        result = review_fanout(state, {str(pr): unresolved_threads(v) for pr, v in raw.items()})
    save(state, env)
    return result


def cli(argv: list[str], env: Mapping[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _run(args, env)
    except (ConductorError, OSError, ValueError) as error:
        print(f"conductor: {error}", file=sys.stderr)
        return 2
    if result is None:
        return 1
    print(result if isinstance(result, str) else json.dumps(result, indent=2, sort_keys=True))
    return 0
