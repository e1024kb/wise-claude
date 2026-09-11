import math
import re
from pathlib import Path
from urllib.parse import quote

from ..yaml_compat import js_string
from .common import (
    Json,
    err_text,
    fail,
    gh,
    git,
    is_protected_branch,
    json_of,
    ok,
    pass_,
    ticket_context,
)

TITLE_MAX = 90
COMMITS_MAX = 20


def find_pr_template(repo: str) -> str | None:
    root = Path(repo)
    for rel in (
        ".github/pull_request_template.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
        "docs/pull_request_template.md",
    ):
        path = root / rel
        if path.exists():
            return str(path)
    directory = root / ".github/PULL_REQUEST_TEMPLATE"
    if directory.exists():
        default = directory / "default.md"
        if default.exists():
            return str(default)
        names = sorted(path for path in directory.iterdir() if path.name.endswith(".md"))
        if names:
            return str(names[0])
    return None


def _summary(facts: Json) -> str:
    return "- " + facts["title"]


def _changes(facts: Json) -> str:
    return (
        "\n".join("- " + item for item in facts["commits"])
        if facts["commits"]
        else "- no commits on the branch yet"
    )


def _context(facts: Json) -> str:
    return (
        "- ticket: "
        + facts["ticket_link"]
        + (f"\n- plan: `{facts['plan_path']}`" if "plan_path" in facts else "")
    )


def default_pr_body(facts: Json) -> str:
    return "\n".join(
        [
            "## Summary",
            _summary(facts),
            "",
            "## Changes",
            _changes(facts),
            "",
            "## Context",
            _context(facts),
            "",
            "## Testing",
            "- [ ] Unit tests pass",
            "- [ ] Manual verification",
            "",
        ]
    )


def fill_pr_template(template: str, facts: Json) -> str:
    out = []
    skipping = matched = False
    for line in template.split("\n"):
        match = re.fullmatch(r"##\s+(.+?)\s*", line)
        if match:
            skipping = False
            out.append(line)
            head = match[1].lower()
            filled = (
                _summary(facts)
                if head.startswith("summary")
                else _changes(facts)
                if head.startswith("change")
                else _context(facts)
                if re.match(r"context|ticket|reference", head)
                else None
            )
            if filled is not None:
                out.extend([filled, ""])
                skipping = matched = True
            continue
        if not skipping:
            out.append(line)
    return (
        re.sub(r"\n{3,}", "\n\n", "\n".join(out))
        if matched
        else default_pr_body(facts) + "\n" + template
    )


def _clip_title(value: str) -> str:
    flat = re.sub(r"[.:;,]+$", "", re.sub(r"\s+", " ", value).strip())
    return flat[: TITLE_MAX - 1] + "…" if len(flat) > TITLE_MAX else flat


def _plan_heading(path: str) -> str | None:
    try:
        return next(
            (
                re.sub(r"^#\s+", "", line).strip()
                for line in Path(path).read_text().split("\n")
                if re.match(r"#\s+\S", line)
            ),
            None,
        )
    except (OSError, UnicodeError):
        return None


async def _collect_facts(ctx: Json) -> Json:
    unit = ctx["unit"]
    ticket: Json = ticket_context(ctx["config"]["tickets"], unit) or {}
    native_ref = ticket.get("ref", unit.get("ticket_ref", unit["ref"]))
    result = await git(
        ctx,
        [
            "log",
            "--pretty=%s",
            f"--max-count={COMMITS_MAX}",
            f"origin/{unit['base'] or 'main'}..{unit['branch']}",
        ],
    )
    commits = (
        [line.strip() for line in result["stdout"].split("\n") if line.strip()]
        if ok(result)
        else []
    )
    if ctx["config"]["pipeline"] == "plan":
        heading = _plan_heading(unit["plan_path"]) if "plan_path" in unit else None
        title = f"{unit['ref']}: {heading}" if heading is not None else unit["ref"]
    elif ticket.get("title"):
        title = f"{native_ref}: {ticket['title']}"
    else:
        title = f"{native_ref}: {commits[0]}" if commits else native_ref
    facts = {
        "ref": native_ref,
        "title": _clip_title(title),
        "ticket_link": f"[{native_ref}]({ticket['url']})"
        if ticket.get("url")
        else f"`{native_ref}`",
        "commits": commits,
    }
    if "plan_path" in unit:
        facts["plan_path"] = unit["plan_path"]
    return facts


async def view_pr(ctx: Json, branch: str) -> Json | None:
    value = json_of(await gh(ctx, ["pr", "view", branch, "--json", "number,url,state,baseRefName"]))
    if (
        not isinstance(value, dict)
        or type(value.get("number")) not in (int, float)
        or not isinstance(value.get("url"), str)
    ):
        return None
    out = {
        "number": value["number"],
        "url": value["url"],
        "state": value["state"] if isinstance(value.get("state"), str) else "OPEN",
    }
    if isinstance(value.get("baseRefName"), str):
        out["base"] = value["baseRefName"]
    return out


async def pr_phase(ctx: Json) -> Json:
    unit = ctx["unit"]
    if is_protected_branch(unit["branch"]):
        return fail(f"pr: refused, {unit['branch']} is a protected branch")
    existing = await view_pr(ctx, unit["branch"])
    if existing and existing["state"] in ("MERGED", "CLOSED"):
        existing_pr = {"number": existing["number"], "url": existing["url"]}
        return fail(
            f"pr-merged: #{js_string(existing_pr['number'])}"
            if existing["state"] == "MERGED"
            else "pr-closed: closed without merge",
            "merged" if existing["state"] == "MERGED" else "human-intervention",
            {"unit": {**unit, "pr": existing_pr}},
        )
    facts = await _collect_facts(ctx)
    template = find_pr_template(ctx["cwd"])
    body = (
        fill_pr_template(Path(template).read_text(), facts)
        if template is not None
        else default_pr_body(facts)
    )
    ctx["log"](f"pr: body from {template or 'the compact default'}")
    directory = Path(ctx["run_dir"]) / "units"
    directory.mkdir(parents=True, exist_ok=True)
    body_path = directory / (quote(unit["branch"], safe="~!*'()") + ".pr-body.md")
    body_path.write_text(body)
    if existing:
        edit = await gh(
            ctx, ["pr", "edit", js_string(existing["number"]), "--body-file", str(body_path)]
        )
        if not ok(edit):
            return fail(f"pr: refresh of #{js_string(existing['number'])} failed: {err_text(edit)}")
        ctx["log"](f"pr: refreshed #{js_string(existing['number'])}")
        return pass_(
            {"unit": {**unit, "pr": {"number": existing["number"], "url": existing["url"]}}}
        )
    created = await gh(
        ctx,
        [
            "pr",
            "create",
            "--base",
            unit["base"] or "main",
            "--head",
            unit["branch"],
            "--title",
            facts["title"],
            "--body-file",
            str(body_path),
        ],
    )
    if not ok(created):
        return fail(f"pr: create failed: {err_text(created)}")
    url = next(
        (
            line.strip()
            for line in created["stdout"].split("\n")
            if re.match(r"https?://", line.strip())
        ),
        None,
    )
    pr = None
    if url is not None:
        try:
            number = float(url.split("/")[-1] or "0")
            if math.isfinite(number) and number.is_integer():
                pr = {"number": int(number), "url": url}
        except ValueError:
            pass
    if pr is None:
        viewed = await view_pr(ctx, unit["branch"])
        if viewed:
            pr = {"number": viewed["number"], "url": viewed["url"]}
    if pr is None:
        return fail("pr: created but could not read its number and url")
    ctx["log"](f"pr: created #{js_string(pr['number'])} {pr['url']}")
    return pass_({"unit": {**unit, "pr": pr}})
