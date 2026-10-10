"""Review-bot observation per head, and the watch loop's local review:
wise reads what a configured bot did and never posts a trigger."""

import asyncio
import json
from pathlib import Path

import pytest

from test_model_phases import ModelFixture, answer, watch_output
from test_phases import command_result
from wise_engine.ledger import read_unit, write_unit
from wise_engine.phases.request_review import request_review_phase
from wise_engine.phases.model import findings_path
from wise_engine.phases.verify import (
    PROVIDERS,
    REQUEST_GRACE_MS,
    classify,
    pr_locator,
    retry_after_ms,
    verify_reviews,
)
from wise_engine.units import run_units_step

CR = PROVIDERS["coderabbit"]
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


def state_of(reviews=(), comments=(), runs=(), head="h2", since=T0):
    return classify(CR, evidence(reviews, comments, runs), head, since)


# --- classification -------------------------------------------------------


def test_absent_without_any_footprint_and_silent_with_one():
    assert state_of()["state"] == "absent"
    old = review("coderabbitai[bot]", "h1", T0 - 10_000)
    assert state_of([old])["state"] == "silent"
    # Copilot or a human reviewing the head is not CodeRabbit evidence.
    assert state_of([review("Copilot", "h2"), review("alice", "h2")])["state"] == "absent"
    # A configured-but-silent reviewer is on the PR: silent, not absent.
    configured = {**evidence(), "requested": ["coderabbitai"]}
    assert classify(CR, configured, "h2", T0)["state"] == "silent"


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
    # A completed head review beats the pause.
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
            # The branch is always ahead of its base, so the local review
            # runs a child instead of short-circuiting on an empty range.
            return command_result(str(max(1, self.commits)))
        return await super().execute(cmd, args, opts)

    def run(self, **over):
        return asyncio.run(run_units_step(self.input(items=["feat/x"], **over)))

    def findings(self):
        return Path(findings_path({"run_dir": str(self.run_dir), "unit": {"branch": "feat/x"}}))

    def ledger(self):
        return read_unit(self.run_dir, "feat/x")

    def posted(self):
        """Every comment or reviewer request wise sent to the PR."""
        return [
            args
            for cmd, args, _ in self.calls
            if cmd == "gh"
            and (
                args[:2] == ["pr", "comment"]
                or (args[:2] == ["pr", "edit"] and "--add-reviewer" in args)
            )
        ]

    def records(self):
        return self.ledger()["watch"]["verification"]["coderabbit"]


def cr_footprint(fixture, head="head-0", at=T0 - 60_000):
    fixture.reviews.append(review("coderabbitai[bot]", head, at))


@pytest.mark.parametrize(
    "setup",
    [
        "no-bot",
        "human-only",
        "copilot-only",
        "other-tool",
        "silent",
        "manual-required",
        "rate-limited",
    ],
)
def test_never_requests_a_bot_review_and_reviews_locally(tmp_path, setup):
    fixture = VerifyFixture(tmp_path)
    fixture.head = "head-5"
    if setup == "human-only":
        fixture.reviews.append(review("alice", "head-5", state="APPROVED"))
    elif setup == "copilot-only":
        fixture.reviews.append(review("Copilot", "head-5"))
    elif setup == "other-tool":
        fixture.reviews.append(review("sonarqubecloud[bot]", "head-5"))
    elif setup != "no-bot":
        cr_footprint(fixture)
    if setup == "manual-required":
        fixture.issue_comments.append(
            comment(
                "coderabbitai[bot]",
                "**Review skipped** Auto incremental reviews are disabled on this repository.",
                T0 + 1,
            )
        )
    elif setup == "rate-limited":
        fixture.issue_comments.append(
            comment("coderabbitai[bot]", "Rate limit exceeded. Please wait 5 minutes.", T0 + 1)
        )
    result = fixture.run()
    assert "merged=1" in result["verdict"]
    assert fixture.posted() == []
    assert fixture.counts["review"] == 1
    assert fixture.ledger()["watch"]["reviewed_sha"] == "head-5"
    expected = {
        "silent": "silent",
        "manual-required": "manual-required",
        "rate-limited": "rate-limited",
    }
    assert fixture.records()["head-5"]["state"] == expected.get(setup, "absent")


def test_configured_bot_findings_are_fixed_then_the_new_head_is_reviewed_locally(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")

    def watch(req, nth):
        if nth == 1:
            fixture.findings().write_text("1. a.py:1 - T1 - coderabbit - fix\n")
            return answer(watch_output(bot_reviews="open", verdict="fix"))
        return answer(watch_output())

    fixture.scripts["watch"] = watch
    result = fixture.run()
    assert "merged=1" in result["verdict"] and fixture.posted() == []
    assert fixture.counts["fix"] == 1
    # Only the pushed head is reviewed: head-0 never reached a settled pass.
    assert fixture.counts["review"] == 1
    assert fixture.ledger()["watch"]["reviewed_sha"] == "head-1"
    assert fixture.records()["head-1"]["state"] == "silent"


def test_local_review_findings_are_fixed_and_the_new_head_re_reviewed(tmp_path):
    fixture = VerifyFixture(tmp_path)

    def local(req, nth):
        verdict = "changes-requested" if nth == 1 else "approve"
        return answer({"findings": 2 - nth, "blocking": 2 - nth, "verdict": verdict})

    fixture.scripts["review"] = local
    result = fixture.run()
    assert "merged=1" in result["verdict"] and fixture.posted() == []
    assert fixture.counts["review"] == 2 and fixture.counts["fix"] == 1
    assert fixture.ledger()["watch"]["reviewed_sha"] == "head-1"


def test_local_review_waits_for_green_ci(tmp_path):
    fixture = VerifyFixture(tmp_path)
    fixture.scripts["watch"] = lambda req, nth: answer(
        watch_output(ci="pending", verdict="wait") if nth < 3 else watch_output()
    )
    result = fixture.run()
    assert "merged=1" in result["verdict"]
    assert fixture.counts["review"] == 1
    first_review = next(i for i, call in enumerate(fixture.child_calls) if call[0] == "review")
    assert [call[0] for call in fixture.child_calls[:first_review]] == ["watch"] * 3


def test_a_head_the_pre_push_review_approved_is_not_reviewed_again(tmp_path):
    fixture = VerifyFixture(tmp_path)
    fixture.head = "head-5"
    write_unit(
        fixture.run_dir,
        "feat/x",
        {
            "unit": {
                "ref": "feat/x",
                "branch": "feat/x",
                "worktree": str(fixture.repo),
                "base": "main",
                "pr": fixture.pr,
            },
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
            "review": {"converged": True, "cycles": 1, "sha": "head-5"},
        },
    )
    result = fixture.run()
    assert "merged=1" in result["verdict"]
    assert fixture.counts["review"] == 0


def test_stuck_bot_does_not_block_the_merge(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    fixture.head = "head-5"
    fixture.scripts["watch"] = lambda req, nth: answer(
        watch_output(bot_reviews="stuck", verdict="wait")
    )
    result = fixture.run()
    assert "merged=1" in result["verdict"] and fixture.posted() == []
    assert fixture.counts["review"] == 1


def test_pending_bot_review_holds_the_merge_until_it_completes(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    fixture.head = "head-5"
    fixture.runs["head-5"] = [check_run()]
    merged_at = []

    def watch(req, nth):
        if nth == 4:
            fixture.runs["head-5"] = [check_run("completed", "success", "Review completed")]
            fixture.reviews.append(review("coderabbitai[bot]", "head-5", fixture.time))
        return answer(watch_output())

    original = fixture.execute

    async def execute(cmd, args, opts):
        if cmd == "gh" and args[:2] == ["pr", "merge"]:
            merged_at.append(fixture.counts["watch"])
        return await original(cmd, args, opts)

    fixture.execute = execute
    fixture.scripts["watch"] = watch
    result = fixture.run()
    assert "merged=1" in result["verdict"] and fixture.posted() == []
    assert merged_at and merged_at[0] >= 5
    assert fixture.records()["head-5"]["state"] == "completed"


def test_someone_elses_trigger_holds_the_merge_only_for_the_grace(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    fixture.head = "head-5"
    fixture.step["caps"] = ["watch_minutes"]
    fixture.state["caps"]["watch_minutes"] = 60
    fixture.issue_comments.append(comment("alice", "@coderabbitai review", T0 + 1, cid=90))
    result = fixture.run()
    assert "merged=1" in result["verdict"] and fixture.posted() == []
    record = fixture.records()["head-5"]
    assert record["state"] == "requested" and record["comment_id"] == 90
    assert fixture.time - record["requested_at"] >= REQUEST_GRACE_MS


def test_access_error_holds_the_merge(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    fixture.head = "head-5"
    fixture.api["repos/a/r/pulls/7/reviews"] = lambda args: command_result(
        code=1, stderr="HTTP 403"
    )
    fixture.scripts["watch"] = lambda req, nth: answer(watch_output())
    result = fixture.run()
    assert "merged=0" in result["verdict"] and fixture.posted() == []
    assert not any(args[:2] == ["pr", "merge"] for cmd, args, _ in fixture.calls if cmd == "gh")
    record = fixture.records()["head-5"]
    assert record["state"] == "access-error" and "403" in record["detail"]


def test_head_mismatch_is_recorded(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    fixture.head = "head-5"
    original = fixture.execute

    async def execute(cmd, args, opts):
        if cmd == "gh" and args[:2] == ["pr", "view"] and "headRefOid" in args[-1]:
            return command_result(json.dumps({**fixture.pr, "headRefOid": "remote-head"}))
        return await original(cmd, args, opts)

    fixture.execute = execute
    fixture.run()
    assert fixture.posted() == []
    assert fixture.records()["head-5"]["state"] == "head-mismatch"


def test_human_comment_still_stands_the_run_down(tmp_path):
    fixture = VerifyFixture(tmp_path)
    cr_footprint(fixture, "head-0")
    fixture.head = "head-5"
    fixture.issue_comments.append(
        {
            **comment("alice", "please rename", T0 + 10**12),
            "user": {"login": "alice", "type": "User"},
        }
    )
    fixture.scripts["watch"] = lambda req, nth: answer(watch_output(human_comment=True))
    result = fixture.run()
    assert result["outputs"]["units"][0]["verdict"] == "human-intervention"
    assert fixture.posted() == [] and "verification" not in fixture.ledger()["watch"]


def test_verify_reviews_without_a_pr_is_a_no_op():
    async def scenario():
        ctx = {"unit": {"pr": None}, "now": lambda: 0, "log": lambda line: None}
        assert await verify_reviews(ctx, {}, "h") == {"hold": False, "states": {}}

    asyncio.run(scenario())


def test_request_review_never_attaches_a_review_bot():
    calls, lines = [], []

    async def scenario():
        async def execute(cmd, args, opts):
            calls.append(args)
            return command_result(json.dumps({"reviewRequests": []}))

        ctx = {
            "unit": {"pr": {"number": 7}},
            "config": {"reviewers": ["copilot-pull-request-reviewer", "CodeRabbitAI", "alice"]},
            "log": lines.append,
            "exec": execute,
            "env": {},
            "cwd": ".",
        }
        return await request_review_phase(ctx)

    asyncio.run(scenario())
    added = [args[-1] for args in calls if "--add-reviewer" in args]
    assert added == ["alice"]
    assert sum("review bot, never requested" in line for line in lines) == 2
