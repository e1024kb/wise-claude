"""Review verification: provider discovery per head and the one batched
CodeRabbit verification request the watch loop posts after a fix batch."""

import asyncio
import json
from pathlib import Path

import pytest

from test_model_phases import ModelFixture, answer, watch_output
from test_phases import command_result
from wise_engine.ledger import read_unit, write_unit
from wise_engine.phases.model import findings_path
from wise_engine.phases.verify import (
    PROVIDERS,
    REQUEST_GRACE_MS,
    VERIFY_GRACE_MS,
    VERIFY_MAX_ATTEMPTS,
    classify,
    pr_locator,
    retry_after_ms,
    verify_reviews,
)
from wise_engine.units import run_units_step

CR = PROVIDERS["coderabbit"]
TRIGGER = CR["trigger"]
T0 = 1_700_000_000_000  # 2023-11-14T22:13:20Z, an arbitrary anchor in ms


def iso(ms):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def review(login, commit, at=T0, state="COMMENTED"):
    return {"user": {"login": login}, "commit_id": commit, "state": state, "submitted_at": iso(at)}


def comment(login, body, at, cid=1):
    return {"id": cid, "user": {"login": login}, "body": body, "created_at": iso(at)}


def check_run(status="in_progress", conclusion=None, title="Review in progress", name="CodeRabbit"):
    return {
        "name": name,
        "app": {"slug": "coderabbitai"},
        "status": status,
        "conclusion": conclusion,
        "started_at": iso(T0),
        "completed_at": iso(T0 + 1000) if status == "completed" else None,
        "output": {"title": title, "summary": ""},
    }


def evidence(reviews=(), comments=(), runs=()):
    return {"reviews": list(reviews), "comments": list(comments), "check_runs": list(runs)}


def state_of(reviews=(), comments=(), runs=(), head="h2", since=T0, record=None):
    return classify(CR, evidence(reviews, comments, runs), head, since, record or {})


# --- classification -------------------------------------------------------


def test_absent_without_any_footprint_and_silent_with_one():
    assert state_of()["state"] == "absent"
    old = review("coderabbitai[bot]", "h1", T0 - 10_000)
    assert state_of([old])["state"] == "silent"
    # Copilot or a human reviewing the head is not CodeRabbit evidence.
    assert state_of([review("Copilot", "h2"), review("alice", "h2")])["state"] == "absent"


def test_completed_only_by_a_review_bound_to_the_head():
    assert state_of([review("coderabbitai[bot]", "h2")])["state"] == "completed"
    assert state_of([review("coderabbitai[bot]", "h1")])["state"] == "silent"


def test_check_run_states():
    assert state_of(runs=[check_run()])["state"] == "pending"
    done = check_run("completed", "success", "Review completed")
    assert state_of(runs=[done])["state"] == "completed"
    skipped = check_run("completed", "skipped", "Review skipped")
    assert state_of(runs=[skipped])["state"] == "skipped"
    limited = check_run("completed", "failure", "Review rate limited")
    assert state_of(runs=[limited])["state"] == "rate-limited"
    failed = check_run("completed", "failure", "Review failed")
    assert state_of(runs=[failed])["state"] == "failed"
    other = check_run(name="ci/test")
    other["app"] = {"slug": "github-actions"}
    assert state_of(runs=[other])["state"] == "absent"


def test_notices_after_the_head_drive_the_state():
    bot = "coderabbitai[bot]"
    older = comment(bot, "**Review skipped** Draft detected.", T0 - 5000)
    assert state_of([review(bot, "h1", T0 - 9000)], [older])["state"] == "silent"
    assert (
        state_of(comments=[comment(bot, "**Review skipped** Draft detected.", T0 + 1)])["state"]
        == "skipped"
    )
    disabled = comment(
        bot,
        "**Review skipped**\n\nAuto incremental reviews are disabled on this repository. "
        "To trigger a single review, invoke the `@coderabbitai review` command.",
        T0 + 1,
    )
    assert state_of(comments=[disabled])["state"] == "manual-required"
    credits = comment(bot, "You have run out of credits; upgrade your plan.", T0 + 1)
    assert state_of(comments=[credits])["state"] == "failed"
    limited = comment(
        bot,
        "**Rate limit exceeded**\n\n@bot has exceeded the limit. Please wait **12 minutes and 30 seconds** before requesting another review.",
        T0 + 1,
    )
    found = state_of(comments=[limited])
    assert found["state"] == "rate-limited" and found["retry_after"] == 12 * 60_000 + 30_000
    assert (
        state_of(comments=[comment(bot, "Currently processing new changes in this PR.", T0 + 1)])[
            "state"
        ]
        == "pending"
    )


def test_trigger_versus_answer_ordering():
    bot = "coderabbitai[bot]"
    trigger = comment("alice", "@coderabbitai review", T0 + 100, cid=7)
    found = state_of([review(bot, "h1", T0 - 9000)], [trigger])
    assert found["state"] == "requested" and found["comment_id"] == 7
    answered = comment(bot, "Rate limit exceeded. Please wait 5 minutes.", T0 + 200)
    assert state_of(comments=[trigger, answered])["state"] == "rate-limited"
    retried = comment("alice", "@coderabbitai full review", T0 + 300)
    assert state_of(comments=[trigger, answered, retried])["state"] == "requested"
    # Chatter mentioning the bot is not a command; the reply is not a trigger.
    chatter = comment("alice", "thanks @coderabbitai review was helpful", T0 + 400)
    assert state_of([review(bot, "h1", T0 - 9000)], [chatter])["state"] == "silent"


def test_pause_and_resume_commands():
    bot = "coderabbitai[bot]"
    footprint = [review(bot, "h1", T0 - 9000)]
    paused = comment("alice", "@coderabbitai pause", T0 - 100_000)
    assert state_of(footprint, [paused])["state"] == "paused"
    resumed = comment("alice", "@coderabbitai resume", T0 - 50_000)
    assert state_of(footprint, [paused, resumed])["state"] == "silent"
    # A completed head review beats the pause: nothing to request anyway.
    assert state_of([review(bot, "h2")], [paused])["state"] == "completed"


def test_retry_after_parsing_and_locator():
    assert retry_after_ms("Please wait 1 hour and 2 minutes and 3 seconds before") == 3_723_000
    assert retry_after_ms("try again in 45 seconds") == 45_000
    assert retry_after_ms("no numbers here") is None
    assert pr_locator({"url": "https://github.com/acme/app/pull/12", "number": 12}) == (
        "acme/app",
        12,
    )
    assert pr_locator({"url": "https://ghe.acme.com/acme/app/pull/12/", "number": 12}) == (
        "acme/app",
        12,
    )
    assert pr_locator({"url": "https://github.com/acme/app/pull/12", "number": 13}) is None
    assert pr_locator({"url": "", "number": 1}) is None


# --- the loop --------------------------------------------------------------


class VerifyFixture(ModelFixture):
    """A `pr` pipeline unit on branch feat/x with PR #7; CodeRabbit evidence
    is scripted per head through `self.reviews`, `self.issue_comments` and
    `self.runs`."""

    def __init__(self, root):
        super().__init__(root)
        self.branches.add("feat/x")
        self.trees[str(self.repo)] = "feat/x"
        self.pr = {"number": 7, "url": "https://github.invalid/a/r/pull/7", "state": "OPEN"}
        self.step.update({"pipeline": "pr", "items": "feat/x", "groups": {}, "caps": []})
        self.reviews = []
        self.issue_comments = []
        self.runs = {}
        self.api["repos/a/r/pulls/7/reviews"] = lambda args: command_result(
            json.dumps(self.reviews)
        )
        self.api["repos/a/r/issues/7/comments"] = lambda args: command_result(
            json.dumps(self.issue_comments)
        )
        self.api["repos/a/r/commits/"] = lambda args: command_result(
            json.dumps({"total_count": 0, "check_runs": self.runs.get(args[1].split("/")[4], [])})
        )
        self.time = T0

    async def execute(self, cmd, args, opts):
        if cmd == "git" and args[:2] == ["symbolic-ref", "--quiet"]:
            return command_result("feat/x\n")
        if cmd == "git" and args[0] == "rev-list":
            # The branch is always ahead of its base, so the substitute
            # review runs a child instead of short-circuiting on an empty range.
            return command_result(str(max(1, self.commits)))
        if cmd == "gh" and args[:2] == ["pr", "comment"]:
            result = await super().execute(cmd, args, opts)
            self.issue_comments.append(
                comment("wise-bot", args[-1], self.time, cid=900 + len(self.comments))
            )
            return result
        return await super().execute(cmd, args, opts)

    def run(self, **over):
        return asyncio.run(run_units_step(self.input(items=["feat/x"], **over)))

    def findings(self):
        return Path(findings_path({"run_dir": str(self.run_dir), "unit": {"branch": "feat/x"}}))

    def ledger(self):
        return read_unit(self.run_dir, "feat/x")

    def triggers(self):
        return [body for body in self.comments if body == TRIGGER]


def cr_footprint(fixture, head="head-0", at=T0 - 60_000):
    fixture.reviews.append(review("coderabbitai[bot]", head, at))


def with_findings(fixture, first_passes, **kw):
    """Watch script: bot findings open for the first N passes, then the default."""

    def watch(req, nth):
        if nth <= first_passes:
            fixture.findings().write_text("1. a.py:1 - thread T1 - coderabbit - fix it\n")
            return answer(watch_output(bot_reviews="open", verdict="fix", **kw))
        return answer(watch_output(**kw))

    fixture.scripts["watch"] = watch


@pytest.mark.parametrize(
    "setup",
    ["no-bot", "human-only", "copilot-only", "other-tool"],
)
def test_no_request_without_a_coderabbit_footprint(tmp_path, setup):
    fixture = VerifyFixture(tmp_path)
    if setup == "human-only":
        fixture.reviews.append(review("alice", "head-0", state="APPROVED"))
    elif setup == "copilot-only":
        fixture.reviews.append(review("Copilot", "head-0"))
    elif setup == "other-tool":
        fixture.reviews.append(review("sonarqubecloud[bot]", "head-0"))
    result = fixture.run()
    assert "merged=1" in result["verdict"]
    assert fixture.triggers() == []
    assert fixture.ledger()["watch"]["verification"]["coderabbit"]["head-0"]["state"] == "absent"


def test_completed_head_is_not_re_requested(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    result = fixture.run()
    assert "merged=1" in result["verdict"] and fixture.triggers() == []
    assert fixture.ledger()["watch"]["verification"]["coderabbit"]["head-0"]["state"] == "completed"


def test_bulk_fix_then_one_verification_request_then_merge(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    # CodeRabbit reviews the head only after the trigger (incremental reviews off).
    with_findings(fixture, 1)

    def watch(req, nth):
        if nth == 1:
            fixture.findings().write_text("1. a.py:1 - T1 - coderabbit - fix\n")
            return answer(watch_output(bot_reviews="open", verdict="fix"))
        if fixture.triggers() and not any(r["commit_id"] == fixture.head for r in fixture.reviews):
            fixture.reviews.append(review("coderabbitai[bot]", fixture.head, fixture.time))
            return answer(watch_output(bot_reviews="pending", verdict="wait"))
        return answer(watch_output(bot_reviews="pending" if not fixture.triggers() else "resolved"))

    fixture.scripts["watch"] = watch
    result = fixture.run()
    assert "merged=1" in result["verdict"]
    assert fixture.triggers() == [TRIGGER]
    records = fixture.ledger()["watch"]["verification"]["coderabbit"]
    assert records["head-1"]["state"] == "completed" and records["head-1"]["attempts"] == 1
    assert fixture.counts["fix"] == 1 and fixture.counts["review"] == 0


def test_manual_required_notice_requests_without_waiting_for_grace(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    with_findings(fixture, 1)
    posted_at = {}

    original = fixture.execute

    async def execute(cmd, args, opts):
        if cmd == "git" and args[0] == "push":
            fixture.issue_comments.append(
                comment(
                    "coderabbitai[bot]",
                    "**Review skipped** Auto incremental reviews are disabled on this repository.",
                    fixture.time + 1,
                    cid=50,
                )
            )
        if cmd == "gh" and args[:2] == ["pr", "comment"]:
            posted_at["at"] = fixture.time
            fixture.reviews.append(review("coderabbitai[bot]", fixture.head, fixture.time + 2))
        return await original(cmd, args, opts)

    fixture.execute = execute
    result = fixture.run()
    assert "merged=1" in result["verdict"] and fixture.triggers() == [TRIGGER]
    pushed = fixture.ledger()["watch"]["head_since"]
    assert pushed["sha"] == "head-1" and posted_at["at"] - pushed["at"] < VERIFY_GRACE_MS


def test_automatic_incremental_review_in_progress_is_not_duplicated(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    with_findings(fixture, 1)
    original = fixture.execute

    async def execute(cmd, args, opts):
        if cmd == "git" and args[0] == "push":
            fixture.runs["head-1"] = [check_run()]
        return await original(cmd, args, opts)

    def watch(req, nth):
        if nth == 1:
            fixture.findings().write_text("1. a.py:1 - T1 - coderabbit - fix\n")
            return answer(watch_output(bot_reviews="open", verdict="fix"))
        if nth == 4:
            fixture.runs["head-1"] = [check_run("completed", "success", "Review completed")]
            fixture.reviews.append(review("coderabbitai[bot]", "head-1", fixture.time))
        return answer(watch_output(bot_reviews="pending" if nth < 4 else "resolved"))

    fixture.execute = execute
    fixture.scripts["watch"] = watch
    result = fixture.run()
    assert "merged=1" in result["verdict"] and fixture.triggers() == []
    records = fixture.ledger()["watch"]["verification"]["coderabbit"]["head-1"]
    assert records["state"] == "completed" and records["attempts"] == 0


def test_repeated_findings_get_a_new_round_per_head(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")

    answered = []

    def watch(req, nth):
        head = fixture.head
        reviewed = any(r["commit_id"] == head for r in fixture.reviews)
        if len(fixture.triggers()) > len(answered) and not reviewed:
            # CodeRabbit answers each trigger once: findings on head-1, none on head-2.
            answered.append(head)
            fixture.reviews.append(review("coderabbitai[bot]", head, fixture.time))
            reviewed = True
        if reviewed and head in ("head-0", "head-1") and nth <= 6:
            fixture.findings().write_text(f"1. a.py:1 - T{nth} - coderabbit - fix\n")
            return answer(watch_output(bot_reviews="open", verdict="fix"))
        return answer(watch_output(bot_reviews="resolved" if reviewed else "pending"))

    fixture.scripts["watch"] = watch
    result = fixture.run()
    assert "merged=1" in result["verdict"]
    assert fixture.triggers() == [TRIGGER, TRIGGER]
    records = fixture.ledger()["watch"]["verification"]["coderabbit"]
    # head-1's request was answered with findings and superseded by the fix push.
    assert [records[h]["state"] for h in ("head-1", "head-2")] == ["requested", "completed"]
    assert fixture.counts["fix"] == 2


def test_open_findings_or_red_ci_never_trigger(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    fixture.step["caps"] = ["max_fix_attempts"]
    fixture.state["caps"]["max_fix_attempts"] = 1
    with_findings(fixture, 99, ci="red")
    result = fixture.run()
    assert result["outputs"]["units"][0]["verdict"] == "exhausted"
    assert fixture.triggers() == []


def test_request_holds_the_merge_until_answered_then_stuck_policy_applies(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    fixture.head = "head-5"
    fixture.step["caps"] = ["watch_minutes"]
    fixture.state["caps"]["watch_minutes"] = 60
    stuck_after = []

    def watch(req, nth):
        # The child sees silence: pending inside its grace, stuck afterwards.
        if fixture.triggers():
            stuck_after.append(nth)
            return answer(watch_output(bot_reviews="stuck", verdict="wait"))
        return answer(watch_output(bot_reviews="pending", verdict="wait"))

    fixture.scripts["watch"] = watch
    result = fixture.run()
    assert "merged=1" in result["verdict"]
    assert fixture.triggers() == [TRIGGER]
    # Substitute review ran once, only after the request grace elapsed.
    assert fixture.counts["review"] == 1
    ledger = fixture.ledger()
    assert ledger["watch"]["fallback_sha"] == "head-5"
    requested_at = ledger["watch"]["verification"]["coderabbit"]["head-5"]["requested_at"]
    assert fixture.time - requested_at >= REQUEST_GRACE_MS


def test_rate_limit_retries_after_reset_but_not_every_poll(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    fixture.head = "head-5"
    fixture.step["caps"] = ["watch_minutes"]
    fixture.state["caps"]["watch_minutes"] = 90
    original = fixture.execute

    async def execute(cmd, args, opts):
        result = await original(cmd, args, opts)
        if cmd == "gh" and args[:2] == ["pr", "comment"] and len(fixture.triggers()) == 1:
            fixture.issue_comments.append(
                comment(
                    "coderabbitai[bot]",
                    "Rate limit exceeded. Please wait **10 minutes and 0 seconds** before requesting another review.",
                    fixture.time + 1,
                    cid=60,
                )
            )
        if cmd == "gh" and args[:2] == ["pr", "comment"] and len(fixture.triggers()) == 2:
            fixture.reviews.append(review("coderabbitai[bot]", "head-5", fixture.time + 2))
        return result

    fixture.execute = execute
    posts = []

    def watch(req, nth):
        posts.append((nth, len(fixture.triggers())))
        reviewed = any(r["commit_id"] == "head-5" for r in fixture.reviews)
        return answer(
            watch_output(bot_reviews="resolved" if reviewed else "pending", verdict="wait")
        )

    fixture.scripts["watch"] = watch
    result = fixture.run()
    assert "merged=1" in result["verdict"]
    assert fixture.triggers() == [TRIGGER, TRIGGER]
    record = fixture.ledger()["watch"]["verification"]["coderabbit"]["head-5"]
    assert record["state"] == "completed" and record["attempts"] == 2
    # Ten passes of one minute passed between the two requests: no per-poll spam.
    first = next(n for n, k in posts if k == 1)
    second = next(n for n, k in posts if k == 2)
    assert second - first >= 10


def test_explicit_skip_and_pause_are_respected(tmp_path):
    for body in ("**Review skipped** Draft detected.", None):
        root = tmp_path / ("skip" if body else "pause")
        root.mkdir()
        fixture = VerifyFixture(root)
        cr_footprint(fixture, "head-0")
        fixture.head = "head-5"
        if body:
            fixture.issue_comments.append(comment("coderabbitai[bot]", body, T0 + 1, cid=70))
        else:
            fixture.issue_comments.append(comment("alice", "@coderabbitai pause", T0 - 10, cid=71))
        fixture.scripts["watch"] = lambda req, nth: answer(watch_output())
        result = fixture.run()
        assert "merged=1" in result["verdict"] and fixture.triggers() == []
        record = fixture.ledger()["watch"]["verification"]["coderabbit"]["head-5"]
        assert record["state"] == ("skipped" if body else "paused")


def test_access_error_neither_requests_nor_reports_completion(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    fixture.head = "head-5"
    fixture.api["repos/a/r/pulls/7/reviews"] = lambda args: command_result(
        code=1, stderr="HTTP 403"
    )
    fixture.scripts["watch"] = lambda req, nth: answer(watch_output())
    result = fixture.run()
    assert "merged=1" in result["verdict"] and fixture.triggers() == []
    record = fixture.ledger()["watch"]["verification"]["coderabbit"]["head-5"]
    assert record["state"] == "access-error" and "403" in record["detail"]


def test_head_mismatch_and_closed_pr_never_request(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    fixture.head = "head-5"
    original = fixture.execute

    async def execute(cmd, args, opts):
        if cmd == "gh" and args[:2] == ["pr", "view"] and "headRefOid" in args[-1]:
            return command_result(json.dumps({**fixture.pr, "headRefOid": "remote-head"}))
        return await original(cmd, args, opts)

    fixture.execute = execute
    fixture.scripts["watch"] = lambda req, nth: answer(watch_output())
    fixture.run()
    assert fixture.triggers() == []
    assert (
        fixture.ledger()["watch"]["verification"]["coderabbit"]["head-5"]["state"]
        == "head-mismatch"
    )


def test_ambiguous_post_is_reconciled_before_a_second_request(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    fixture.head = "head-5"
    original = fixture.execute
    calls = []

    async def execute(cmd, args, opts):
        if cmd == "gh" and args[:2] == ["pr", "comment"]:
            calls.append(fixture.time)
            if len(calls) == 1:
                # The comment landed, but the CLI timed out before saying so.
                fixture.issue_comments.append(comment("wise-bot", TRIGGER, fixture.time, cid=80))
                return {"code": 1, "stdout": "", "stderr": "", "timed_out": True}
        return await original(cmd, args, opts)

    fixture.execute = execute

    def watch(req, nth):
        if any(c["body"] == TRIGGER for c in fixture.issue_comments) and nth >= 5:
            fixture.reviews.append(review("coderabbitai[bot]", "head-5", fixture.time))
            return answer(watch_output())
        return answer(watch_output(bot_reviews="pending", verdict="wait"))

    fixture.scripts["watch"] = watch
    result = fixture.run()
    assert "merged=1" in result["verdict"]
    assert len(calls) == 1
    record = fixture.ledger()["watch"]["verification"]["coderabbit"]["head-5"]
    assert record["state"] == "completed" and record["attempts"] == 1 and record["comment_id"] == 80


def test_resume_and_concurrent_trigger_do_not_duplicate(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    fixture.head = "head-5"
    # Another watcher (or the previous incarnation of this run) already asked.
    fixture.issue_comments.append(comment("someone-else", TRIGGER, T0 - 30_000, cid=90))
    unit = {
        "ref": "feat/x",
        "branch": "feat/x",
        "worktree": str(fixture.repo),
        "base": "main",
        "pr": fixture.pr,
    }
    write_unit(
        fixture.run_dir,
        "feat/x",
        {
            "unit": unit,
            "last_phase": "claim",
            "cleaned": False,
            "cursors": {},
            "usage": {
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
                "pool": "subscription",
            },
            "caps": {},
            "watch": {
                "passes": 3,
                "fix_attempts": 1,
                "stable": 0,
                "started": T0 - 120_000,
                "head_since": {"sha": "head-5", "at": T0 - 100_000},
            },
        },
    )

    def watch(req, nth):
        if nth >= 2:
            fixture.reviews.append(review("coderabbitai[bot]", "head-5", fixture.time))
            return answer(watch_output())
        return answer(watch_output(bot_reviews="pending", verdict="wait"))

    fixture.scripts["watch"] = watch
    result = fixture.run()
    assert "merged=1" in result["verdict"] and fixture.triggers() == []
    ledger = fixture.ledger()
    assert ledger["watch"]["passes"] >= 5
    record = ledger["watch"]["verification"]["coderabbit"]["head-5"]
    assert record["state"] == "completed" and record["attempts"] == 0 and record["comment_id"] == 90


def test_request_budget_is_per_head(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    fixture.head = "head-5"
    fixture.step["caps"] = ["watch_minutes"]
    fixture.state["caps"]["watch_minutes"] = 200
    original = fixture.execute

    async def execute(cmd, args, opts):
        result = await original(cmd, args, opts)
        if cmd == "gh" and args[:2] == ["pr", "comment"]:
            fixture.issue_comments.append(
                comment(
                    "coderabbitai[bot]",
                    "Rate limit exceeded. Please wait 5 minutes.",
                    fixture.time + 1,
                )
            )
        return result

    fixture.execute = execute
    fixture.scripts["watch"] = lambda req, nth: answer(
        watch_output(bot_reviews="pending", verdict="wait")
    )
    result = fixture.run()
    assert result["outputs"]["units"][0]["verdict"] == "all-green"
    assert len(fixture.triggers()) == VERIFY_MAX_ATTEMPTS
    record = fixture.ledger()["watch"]["verification"]["coderabbit"]["head-5"]
    assert record["attempts"] == VERIFY_MAX_ATTEMPTS and "budget exhausted" in record["detail"]


def test_human_comment_still_stands_the_run_down_before_any_request(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    fixture.head = "head-5"
    fixture.scripts["watch"] = lambda req, nth: answer(watch_output(human_comment=True))
    result = fixture.run()
    assert result["outputs"]["units"][0]["verdict"] == "human-intervention"
    assert fixture.triggers() == [] and "verification" not in fixture.ledger()["watch"]


def test_verify_reviews_without_a_pr_is_a_no_op():
    async def scenario():
        ctx = {"unit": {"pr": None}, "now": lambda: 0, "log": lambda line: None}
        assert await verify_reviews(ctx, {}, "h", watch_output()) == {"hold": False, "states": {}}

    asyncio.run(scenario())
