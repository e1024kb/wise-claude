"""Review verification for the watch loop.

Discovers what a review provider (CodeRabbit today; the table is generic)
did for the PR head from evidence tied to that head - reviews with the
head's `commit_id`, check runs on the head, status notices and trigger
comments newer than the head - and requests ONE incremental verification
review per head once the fix batch is done. State lives in the unit
ledger under `watch.verification[<provider>][<head>]`, so polls, resume
and concurrent watchers never post twice for the same head.

The prose twin for the interactive watcher is
`references/pr/review-verification.md`; keep the two in step.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from ..yaml_compat import js_string
from .common import Json, NETWORK_CMD_TIMEOUT_MS, err_text, gh, git, json_of, ok

# An automatic review shows its check run within a minute of the push;
# wait this long on a silent head before assuming nothing is coming.
VERIFY_GRACE_MS = 120_000
# Trigger comments per head, whatever the outcome of each.
VERIFY_MAX_ATTEMPTS = 3
# Backoff after a rejected or failed request when the provider gave no
# reset time; the remaining wall-clock budget bounds it in the loop.
VERIFY_BACKOFF_MS = (300_000, 900_000, 1_800_000)
# A request is answered by the provider within this window or it is not
# answered at all; the watch phase's stuck policy takes over after it.
REQUEST_GRACE_MS = 15 * 60_000
# How many heads a provider's history keeps in the ledger.
HISTORY_HEADS = 5
# States in which the loop waits instead of counting the pass as covered.
HOLD_STATES = ("requested", "pending")

PROVIDERS: dict[str, Json] = {
    "coderabbit": {
        "logins": ("coderabbitai", "coderabbitai[bot]"),
        "check_names": ("coderabbit",),
        "app_slugs": ("coderabbitai",),
        "handle": "@coderabbitai",
        "trigger": "@coderabbitai review",
        # Comment commands, matched as whole commands at the start of a body.
        "review_commands": ("review", "full review"),
        "pause_commands": ("pause",),
        "resume_commands": ("resume",),
        # Status-notice kinds, first match wins: the "auto reviews disabled"
        # skip notice invites a manual trigger and must beat the generic
        # skip pattern; a rate-limit refusal beats the generic failure.
        "notices": (
            (
                "manual-required",
                r"auto(?:matic)?(?: incremental)? reviews? (?:are|is) (?:disabled|off|turned off)",
            ),
            ("paused", r"reviews? (?:are |is )?(?:currently )?paused"),
            ("rate-limited", r"rate[ -]?limit"),
            ("skipped", r"review skipped|skipped the review|skipping (?:the )?review"),
            (
                "failed",
                r"out of credits|credit balance|usage limit|upgrade your plan|action not completed|"
                r"unable to review|could(?: not|n't) review|review failed",
            ),
            ("pending", r"review in progress|currently processing|processing new changes"),
        ),
    },
}


def _ms(stamp: Any) -> float | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return None


def _login(row: Any) -> str:
    user = row.get("user") if isinstance(row, dict) else None
    login = user.get("login") if isinstance(user, dict) else None
    return login.lower() if isinstance(login, str) else ""


def _documents(text: str) -> list[Any]:
    """Every JSON document in `text` (`gh api --paginate` concatenates pages)."""
    decoder = json.JSONDecoder()
    out: list[Any] = []
    index = 0
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            return out
        try:
            value, index = decoder.raw_decode(text, index)
        except ValueError:
            return out
        out.append(value)


def _rows(text: str) -> list[Json]:
    rows: list[Json] = []
    for doc in _documents(text):
        if isinstance(doc, list):
            rows.extend(row for row in doc if isinstance(row, dict))
    return rows


def notice_kind(spec: Json, text: str) -> str | None:
    for kind, pattern in spec["notices"]:
        if re.search(pattern, text, re.I):
            return kind
    return None


def retry_after_ms(text: str) -> float | None:
    """The wait a rate-limit notice asks for ("wait 12 minutes and 3 seconds")."""
    match = re.search(r"(?:wait|try again)(.{0,200})", text, re.I | re.S)
    if not match:
        return None
    total = 0.0
    for amount, unit in re.findall(r"(\d+)\s*(hour|minute|second)", match.group(1), re.I):
        total += int(amount) * {"hour": 3_600_000, "minute": 60_000, "second": 1000}[unit.lower()]
    return total or None


def _command(spec: Json, body: str) -> str | None:
    text = re.sub(r"\s+", " ", body.strip().lower())
    handle = spec["handle"].lower()
    for group in ("review_commands", "pause_commands", "resume_commands"):
        for command in spec[group]:
            if re.match(rf"{re.escape(handle)} {re.escape(command)}(?:\b|$)", text):
                return group
    return None


def pr_locator(pr: Json) -> tuple[str, int] | None:
    """`(owner/repo, number)` from the recorded PR url and number."""
    url = pr.get("url", "")
    match = re.search(r"/([^/]+/[^/]+)/pull/(\d+)/?$", url) if isinstance(url, str) else None
    number = pr.get("number")
    if match is None or not isinstance(number, (int, float)) or int(number) != int(match[2]):
        return None
    return match[1], int(number)


async def _paginated(ctx: Json, path: str) -> list[Json] | str:
    """The rows behind a paginated list endpoint, or the error text."""
    result = await gh(ctx, ["api", path, "--paginate"], {"timeout_ms": NETWORK_CMD_TIMEOUT_MS})
    return _rows(result["stdout"]) if ok(result) else err_text(result, 120)


async def gather_evidence(ctx: Json, repo: str, number: int, head: str) -> Json:
    """Head, PR state, review requests, reviews, issue comments and the head's
    check runs, or `{error}`."""
    view = await gh(
        ctx, ["pr", "view", js_string(number), "--json", "headRefOid,state,reviewRequests"]
    )
    parsed = json_of(view)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("headRefOid"), str):
        return {"error": f"pr view: {err_text(view, 120)}"}
    if parsed["headRefOid"] != head:
        return {"head": parsed["headRefOid"], "state": parsed.get("state"), "mismatch": True}
    reviews = await _paginated(ctx, f"repos/{repo}/pulls/{number}/reviews?per_page=100")
    if isinstance(reviews, str):
        return {"error": f"reviews: {reviews}"}
    comments = await _paginated(ctx, f"repos/{repo}/issues/{number}/comments?per_page=100")
    if isinstance(comments, str):
        return {"error": f"comments: {comments}"}
    runs = await gh(ctx, ["api", f"repos/{repo}/commits/{head}/check-runs?per_page=100"])
    docs = _documents(runs["stdout"]) if ok(runs) else []
    if not ok(runs) or not docs or not isinstance(docs[0], dict):
        return {"error": f"check runs: {err_text(runs, 120)}"}
    check_runs = [row for row in docs[0].get("check_runs", []) if isinstance(row, dict)]
    requests = parsed.get("reviewRequests")
    return {
        "head": head,
        "state": parsed.get("state"),
        "requested": [
            row["login"].lower()
            for row in (requests if isinstance(requests, list) else [])
            if isinstance(row, dict) and isinstance(row.get("login"), str)
        ],
        "reviews": reviews,
        "comments": comments,
        "check_runs": check_runs,
    }


def _provider_run(spec: Json, run: Json) -> bool:
    app = run.get("app")
    slug = app.get("slug") if isinstance(app, dict) else None
    name = run.get("name", "")
    return (isinstance(name, str) and name.lower() in spec["check_names"]) or (
        isinstance(slug, str) and slug.lower() in spec["app_slugs"]
    )


def classify(spec: Json, evidence: Json, head: str, head_since: float, record: Json) -> Json:
    """The provider's state for `head`: completed | pending | requested |
    rate-limited | skipped | failed | paused | manual-required | silent | absent."""
    logins = spec["logins"]
    reviews = [row for row in evidence["reviews"] if _login(row) in logins]
    comments = [row for row in evidence["comments"] if _login(row) in logins]
    runs = [row for row in evidence["check_runs"] if _provider_run(spec, row)]
    # A provider configured as a reviewer is on the PR even before it spoke.
    requested = any(login in logins for login in evidence.get("requested", ()))
    footprint = bool(reviews or comments or runs or requested)
    paused = False
    for row in sorted(evidence["comments"], key=lambda row: _ms(row.get("created_at")) or 0):
        command = _command(spec, row.get("body") or "")
        if command == "pause_commands":
            paused = True
        elif command == "resume_commands":
            paused = False
    if any(row.get("commit_id") == head for row in reviews):
        return {"state": "completed", "footprint": True}
    if paused:
        return {"state": "paused", "footprint": footprint, "detail": "paused by comment command"}
    run = max(runs, key=lambda row: _ms(row.get("started_at")) or 0, default=None)
    if run is not None:
        output = run.get("output") if isinstance(run.get("output"), dict) else {}
        title = " ".join(str(output.get(key) or "") for key in ("title", "summary")).strip() or str(
            run.get("status") or ""
        )
        kind = notice_kind(spec, title)
        if run.get("status") != "completed":
            return {"state": "pending", "footprint": True, "detail": title}
        if kind in ("manual-required", "rate-limited", "skipped", "failed", "paused"):
            out = {"state": kind, "footprint": True, "detail": title}
            if kind == "rate-limited":
                out["retry_after"] = retry_after_ms(title)
                out["at"] = _ms(run.get("completed_at"))
            return out
        conclusion = run.get("conclusion")
        if conclusion in ("success", "neutral"):
            return {"state": "completed", "footprint": True, "detail": title}
        if conclusion == "skipped":
            return {"state": "skipped", "footprint": True, "detail": title}
        return {"state": "failed", "footprint": True, "detail": title or str(conclusion)}
    # No head-bound evidence: read the conversation since the head appeared,
    # latest item first. A trigger newer than every provider notice is an
    # unanswered request; a notice newer than the trigger answers it.
    since = head_since
    if record.get("state") == "unknown" and record.get("attempted_at") is not None:
        since = min(since, record["attempted_at"] - 60_000)
    events: list[tuple[float, str, Json]] = []
    for row in evidence["comments"]:
        stamp = _ms(row.get("created_at"))
        if stamp is None or stamp < since:
            continue
        body = row.get("body") or ""
        if _login(row) in logins:
            events.append((stamp, notice_kind(spec, body) or "activity", row))
        elif _command(spec, body) == "review_commands":
            events.append((stamp, "trigger", row))
    events.sort(key=lambda event: event[0])
    for stamp, kind, row in reversed(events):
        if kind == "trigger":
            return {
                "state": "requested",
                "footprint": footprint,
                "at": stamp,
                "comment_id": row.get("id"),
            }
        if kind == "rate-limited":
            return {
                "state": "rate-limited",
                "footprint": True,
                "at": stamp,
                "retry_after": retry_after_ms(row.get("body") or ""),
                "detail": "rate limited",
            }
        if kind in ("skipped", "failed", "paused", "manual-required", "pending"):
            return {"state": kind, "footprint": True, "detail": kind}
        # "activity": the provider said something we do not classify; it
        # answers an older trigger, so keep looking at older items.
    return {"state": "silent" if footprint else "absent", "footprint": footprint}


def _backoff(attempts: int) -> float:
    return VERIFY_BACKOFF_MS[min(max(attempts, 1), len(VERIFY_BACKOFF_MS)) - 1]


async def commit_time_ms(ctx: Json) -> float | None:
    result = await git(
        ctx, ["show", "-s", "--format=%cI", "HEAD"], {"cwd": ctx["unit"]["worktree"]}
    )
    return _ms(result["stdout"].strip()) if ok(result) else None


async def verify_reviews(ctx: Json, watch: Json, head: str, output: Json) -> Json:
    """One call per watch pass after the pass's own fixes are settled.

    Returns `{"hold": bool, "states": {provider: state}}`; `hold` asks the
    loop to wait for a review that is requested or running instead of
    counting the pass as covered. Mutates `watch["verification"]`; the
    caller checkpoints."""
    pr = ctx["unit"].get("pr")
    locator = pr_locator(pr) if pr else None
    states: Json = {}
    if locator is None:
        return {"hold": False, "states": states}
    repo, number = locator
    now = ctx["now"]()
    since = watch.get("head_since", {})
    head_since = since["at"] if since.get("sha") == head else now
    history = watch.setdefault("verification", {})
    hold = False
    for name, spec in PROVIDERS.items():
        records = history.setdefault(name, {})
        record = records.setdefault(
            head, {"repo": repo, "pr": number, "attempts": 0, "state": "new"}
        )
        for stale in sorted(records, key=lambda sha: records[sha].get("updated", 0))[
            : max(0, len(records) - HISTORY_HEADS)
        ]:
            if stale != head:
                del records[stale]
        previous = record.get("state")
        result = await _verify_provider(
            ctx, spec, name, repo, number, head, head_since, record, watch, output, now
        )
        record["updated"] = now
        states[name] = record["state"]
        hold = hold or result
        if record["state"] != previous:
            detail = record.get("detail")
            ctx["log"](
                f"verify({name}): {record['state']} for {head[:12]}"
                + (f" ({detail})" if detail else "")
                + (f", attempts={js_string(record['attempts'])}" if record["attempts"] else "")
            )
    return {"hold": hold, "states": states}


async def _verify_provider(
    ctx: Json,
    spec: Json,
    name: str,
    repo: str,
    number: int,
    head: str,
    head_since: float,
    record: Json,
    watch: Json,
    output: Json,
    now: float,
) -> bool:
    evidence = await gather_evidence(ctx, repo, number, head)
    if "error" in evidence:
        # Access errors are not "no review": hold the merge and read again
        # next pass; the watch_minutes cap bounds the wait.
        record["detail"] = evidence["error"]
        record["state"] = "access-error"
        return True
    if evidence.get("mismatch"):
        record.update(state="head-mismatch", detail=f"PR head is {evidence['head'][:12]}")
        return False
    if evidence.get("state") != "OPEN":
        record.update(state="closed", detail=f"PR {evidence.get('state')}")
        return False
    found = classify(spec, evidence, head, head_since, record)
    state = found["state"]
    record["detail"] = found.get("detail")
    if state == "requested":
        record.update(state="requested", requested_at=found["at"])
        if found.get("comment_id") is not None:
            record["comment_id"] = found["comment_id"]
        return now - found["at"] < REQUEST_GRACE_MS
    if state == "pending":
        record["state"] = "pending"
        return True
    if state == "rate-limited":
        record["state"] = "rate-limited"
        record["retry_at"] = (found.get("at") or now) + (
            found.get("retry_after") or _backoff(record["attempts"])
        )
        record["detail"] = "rate limited; retry at " + _iso(record["retry_at"])
    elif state in ("completed", "skipped", "failed", "paused", "absent"):
        record["state"] = state
        return False
    elif state == "manual-required":
        record["state"] = "manual-required"
    elif state == "silent":
        record["state"] = "silent"
    if not spec["trigger"]:
        return False
    # Eligibility: the batch is done (nothing red or open to fix), the head
    # is not covered by the substitute review, the request budget holds and
    # any provider-announced reset time has passed.
    if output.get("ci") == "red" or output.get("bot_reviews") == "open":
        record["detail"] = "fix batch open"
        return False
    if watch.get("fallback_sha") == head:
        record["detail"] = "covered by the substitute review"
        return False
    if record["attempts"] >= VERIFY_MAX_ATTEMPTS:
        record["detail"] = "request budget exhausted"
        return False
    if record.get("retry_at") is not None and now < record["retry_at"]:
        return False
    if state == "silent" and now - head_since < VERIFY_GRACE_MS:
        record["detail"] = "waiting for an automatic review"
        return False
    result = await gh(
        ctx,
        ["pr", "comment", js_string(number), "--body", spec["trigger"]],
        {"timeout_ms": NETWORK_CMD_TIMEOUT_MS},
    )
    record["attempts"] += 1
    record.pop("retry_at", None)
    if ok(result):
        match = re.search(r"issuecomment-(\d+)", result["stdout"])
        record.update(state="requested", requested_at=now, detail="verification requested")
        if match:
            record["comment_id"] = int(match[1])
        ctx["log"](f"verify({name}): posted `{spec['trigger']}` for {head[:12]}")
        return True
    if result.get("timed_out") or "error" in result:
        # The comment may or may not exist: reconcile from the conversation
        # on the next pass before posting again.
        record.update(state="unknown", attempted_at=now, detail=err_text(result, 120))
        return True
    record.update(
        state="request-failed",
        retry_at=now + _backoff(record["attempts"]),
        detail=err_text(result, 120),
    )
    return False


def _iso(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def human_commenters(ctx: Json, since_ms: float) -> list[str] | None:
    """Logins of people who commented on or reviewed the PR since `since_ms`.

    Only a User-type account counts: a Bot account, a `[bot]` login, a known
    review bot, and this run's own login (the fix pass replies to threads as
    the operator) never do. None when GitHub cannot be read, so the caller
    falls back to the watch child's own classification."""
    from .model import BOT_LOGINS

    located = pr_locator(ctx["unit"].get("pr") or {})
    if located is None:
        return None
    repo, number = located
    me = await gh(ctx, ["api", "user", "--jq", ".login"])
    own = me["stdout"].strip().lower() if ok(me) else ""
    bots = {login.lower() for login in BOT_LOGINS}
    people: list[str] = []
    for path, stamp in (
        (f"repos/{repo}/issues/{number}/comments?per_page=100", "created_at"),
        (f"repos/{repo}/pulls/{number}/comments?per_page=100", "created_at"),
        (f"repos/{repo}/pulls/{number}/reviews?per_page=100", "submitted_at"),
    ):
        rows = await _paginated(ctx, path)
        if isinstance(rows, str):
            return None
        for row in rows:
            raw_user = row.get("user")
            user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
            login = _login(row)
            when = _ms(row.get(stamp))
            if (
                not login
                or user.get("type") != "User"
                or login.endswith("[bot]")
                or login in bots
                or login == own
                or when is None
                or when < since_ms
            ):
                continue
            if login not in people:
                people.append(login)
    return people
