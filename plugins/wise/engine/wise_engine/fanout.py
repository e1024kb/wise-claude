"""Epic fan-out for the `units` step: item specs, the dependency DAG and the
per-child repository.

An expansion step (an agent that reads the tracker) turns an epic or parent
work item into its children and hands the `units` step a JSON array of item
specs. This module turns that array into scheduler input. It never talks to
a tracker: the tracker state, the `blocked-by` edges and the target repo all
arrive in the specs, and this code only validates and orders them.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

Json = dict[str, Any]

# Tracker states that end a child before it runs. Compared lowercased with
# spaces, dashes and underscores removed, so "Won't do", "wont-do" and
# "WONT_DO" all match.
TERMINAL_STATES = frozenset(
    (
        "done",
        "completed",
        "complete",
        "closed",
        "canceled",
        "cancelled",
        "duplicate",
        "merged",
        "released",
        "resolved",
        "wontdo",
        "won'tdo",
        "wontfix",
        "obsolete",
    )
)

# A child whose pipeline reached one of these verdicts unblocks its
# dependents. Every other verdict (failed, exhausted, an open PR, no-pr,
# skipped) leaves them blocked.
SUCCESS_VERDICTS = {
    "ticket": frozenset(("merged",)),
    "plan": frozenset(("merged",)),
    "ticket-plan": frozenset(("plan-written",)),
    "pr": frozenset(("merged",)),
    "implement": frozenset(("all-green",)),
}

# Verdicts that count as a child failure for `on_child_failure: stop`. An
# open PR a human has to finish (all-green, human-intervention, blocked)
# is an outcome, not a failure.
FAILURE_VERDICTS = frozenset(("failed", "exhausted", "partial"))

MAX_CONCURRENCY = 4


class ExpansionError(ValueError):
    """The expansion output cannot be scheduled (bad JSON, a cycle)."""


def _norm_state(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value.strip().lower())


def is_terminal_state(state: Any) -> bool:
    return isinstance(state, str) and _norm_state(state) in TERMINAL_STATES


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(v.strip() for v in value if isinstance(v, str) and v.strip()))


def item_spec(value: Any) -> Json | None:
    """One item as the scheduler reads it: a plain string is a ref with no
    edges; an object keeps its ref, url, title, state, parent, depends_on,
    repo and serialize keys. Anything else is dropped."""
    if isinstance(value, str):
        ref = value.strip()
        return {"ref": ref} if ref else None
    if not isinstance(value, dict) or not isinstance(value.get("ref"), str):
        return None
    ref = value["ref"].strip()
    if not ref:
        return None
    spec: Json = {"ref": ref}
    for key in ("url", "title", "state", "parent", "repo"):
        if isinstance(value.get(key), str) and value[key].strip():
            spec[key] = value[key].strip()
    for key in ("depends_on", "serialize"):
        values = _strings(value.get(key))
        if values:
            spec[key] = values
    return spec


def parse_item_specs(text: str) -> list[Json]:
    """Parse the `items` of a units step into specs.

    A JSON array (strings or objects) is strict: a text that starts with `[`
    but is not a JSON array raises ExpansionError, so a garbled expansion
    never turns into comma-split refs. Any other text is the comma list the
    workflows have always accepted."""
    trimmed = text.strip()
    if trimmed.startswith("["):
        try:
            values = json.loads(trimmed)
        except ValueError as error:
            raise ExpansionError(f"items: not a JSON array ({error})") from error
        if not isinstance(values, list):
            raise ExpansionError("items: not a JSON array")
        raw = [spec for spec in (item_spec(v) for v in values) if spec is not None]
    else:
        raw = [{"ref": part.strip()} for part in re.split(r"[,;\n]", trimmed) if part.strip()]
    seen: dict[str, Json] = {}
    for spec in raw:
        seen.setdefault(spec["ref"], spec)
    return list(seen.values())


def normalize_items(items: list[Any]) -> list[Json]:
    """Specs from what the executor or a test passes: strings or objects."""
    out: dict[str, Json] = {}
    for value in items:
        spec = item_spec(value)
        if spec is not None:
            out.setdefault(spec["ref"], spec)
    return list(out.values())


def find_cycle(edges: dict[str, list[str]]) -> list[str] | None:
    """A dependency cycle as the path of keys that closes it, else None."""
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(key: str) -> list[str] | None:
        state[key] = 1
        stack.append(key)
        for dep in edges.get(key, []):
            if state.get(dep) == 1:
                return stack[stack.index(dep) :] + [dep]
            if state.get(dep) is None:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        state[key] = 2
        return None

    for key in edges:
        if state.get(key) is None:
            found = visit(key)
            if found:
                return found
    return None


def dependency_edges(nodes: list[Json], log: Callable[[str], None]) -> dict[str, list[str]]:
    """Map each node key to the keys it waits for.

    A node names its blockers by ref (or url); a blocker outside this run is
    not waited for and is logged, because nothing here can finish it. Raises
    ExpansionError on a cycle."""
    by_name: dict[str, str] = {}
    for node in nodes:
        for name in (node["spec"]["ref"], node["spec"].get("url"), node["unit"]["ref"]):
            if isinstance(name, str) and name:
                by_name.setdefault(name, node["key"])
                by_name.setdefault(name.removeprefix("#"), node["key"])
    edges: dict[str, list[str]] = {}
    for node in nodes:
        deps = []
        for name in node["spec"].get("depends_on", []):
            key = by_name.get(name, by_name.get(name.removeprefix("#")))
            if key is None:
                log(
                    f"[{node['unit']['ref']}] depends on {name}, which is not in this run; not waited for"
                )
            elif key != node["key"] and key not in deps:
                deps.append(key)
        edges[node["key"]] = deps
    cycle = find_cycle(edges)
    if cycle:
        refs = {node["key"]: node["unit"]["ref"] for node in nodes}
        raise ExpansionError(
            "dependency cycle: " + " -> ".join(refs.get(key, key) for key in cycle)
        )
    return edges


def concurrency_of(step: Json, inputs: Json) -> int:
    """Worker count: the `concurrency` input when it holds 1..MAX_CONCURRENCY,
    else the step's `parallel:`, else 1."""
    raw = str(inputs.get("concurrency", "") or "").strip()
    if raw.isdigit() and 1 <= int(raw) <= MAX_CONCURRENCY:
        return int(raw)
    value = step.get("parallel", 1)
    return int(value) if isinstance(value, (int, float)) and value >= 1 else 1


def repo_slug(url: str) -> str | None:
    """`owner/name` from a git remote URL (https, ssh or scp form)."""
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url.strip())
    return match.group(1).lower() if match else None


def parse_repo_paths(text: str) -> dict[str, str]:
    """The `repo_paths` input: `owner/name=/path` pairs, comma or newline separated."""
    out = {}
    for part in re.split(r"[,;\n]", text or ""):
        if "=" not in part:
            continue
        slug, path = (piece.strip() for piece in part.split("=", 1))
        if slug and path:
            out[slug.lower().removesuffix(".git")] = os.path.expanduser(path)
    return out


RepoProbe = Callable[[str], Awaitable[str | None]]


async def resolve_repo(
    wanted: str | None,
    project: str,
    project_slug: str | None,
    repo_paths: dict[str, str],
    origin_of: RepoProbe,
) -> str | None:
    """The local checkout a child targets.

    No repo, or the project's own: the project checkout. An absolute path
    that exists: that path. `owner/name`: the `repo_paths` input, else a
    sibling of the project checkout whose origin is that repo. None when no
    checkout is found; the child is skipped, never cloned."""
    if not wanted:
        return project
    value = wanted.strip()
    if value.startswith(("/", "~")):
        path = Path(os.path.expanduser(value))
        return str(path.resolve()) if (path / ".git").exists() else None
    slug = repo_slug(value) or value.lower().removesuffix(".git")
    if slug == project_slug:
        return project
    if slug in repo_paths:
        path = Path(repo_paths[slug])
        return str(path.resolve()) if (path / ".git").exists() else None
    parent = Path(project).resolve().parent
    try:
        siblings = sorted(p for p in parent.iterdir() if (p / ".git").exists())
    except OSError:
        return None
    for sibling in siblings:
        url = await origin_of(str(sibling))
        if url and repo_slug(url) == slug:
            return str(sibling.resolve())
    return None
