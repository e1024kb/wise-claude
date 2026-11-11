import io
import json
import os
from pathlib import Path

import pytest

from wise_engine import conductor as c
from wise_engine import preflight as p
from wise_engine.defs import load_and_validate
from wise_engine.standalone import main as helpers_main

ROOT = Path(__file__).parents[2]
TUNING = {
    "tuning-scope": "single",
    "harness.all": "claude",
    "model.all": "opus",
    "effort.all": "high",
    "permissions.claude": "auto",
}


@pytest.fixture
def env(tmp_path):
    return {"XDG_DATA_HOME": str(tmp_path / "data"), "HOME": str(tmp_path)}


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "shop"
    path.mkdir()
    return path


def make(repo, env, **setup):
    state = c.init("local_conductor", repo, setup, TUNING, "desktop", "epic PROJ-100", env)
    return state


def ticket(ref="PROJ-1", **over):
    return {
        "ref": ref,
        "title": "Trap focus in the drawer",
        "body": "The drawer must trap focus.",
        "url": f"https://jira.example/browse/{ref}",
        "comments": ["QA: Esc must close it"],
        **over,
    }


# -- setup persistence -------------------------------------------------------


def test_setup_defaults_follow_the_spec(repo, env):
    state = make(repo, env)
    assert state["setup"] == {
        "base_rule": "default",
        "base_fixed": "",
        "release_field": "fixVersion",
        "release_pattern": "release-{version}",
        "release_missing": "default",
        "merge_policy": "never",
        "pr_state": "draft-then-ready",
        "questions": "none",
        "overlap": "follow-base",
        "auth_fallback": "stop",
        "tracker_writeback": "no",
        "poll_seconds": "60",
        "stall_minutes": "15",
    }


def test_setup_survives_a_restart_and_reinit_keeps_the_ledger(repo, env):
    state = make(repo, env, merge_policy="green")
    c.add_child(state, "ticket", "PROJ-1", base="main")
    c.save(state, env)
    loaded = c.load("local_conductor", repo, env)
    assert loaded["setup"]["merge_policy"] == "green"
    assert loaded["answers"] == TUNING
    again = c.init("local_conductor", repo, {"merge_policy": "never"}, None, "desktop", env=env)
    assert again["setup"]["merge_policy"] == "never"
    assert [child["ref"] for child in again["children"]] == ["PROJ-1"]
    assert again["answers"] == TUNING and again["prompt"] == "epic PROJ-100"
    assert c.latest(repo, env)["id"] == "local_conductor"


def test_project_rules_are_written_for_later_conductors(repo, env):
    make(repo, env, base_rule="release", release_pattern="rel/{major}.{minor}")
    rules = json.loads(c.project_rules_path(repo, env).read_text())
    assert rules["base_rule"] == "release" and rules["release_pattern"] == "rel/{major}.{minor}"
    assert rules["merge_policy"] == "never"
    assert "../<repo>.<KEY>" in rules["worktree_pattern"]


@pytest.mark.parametrize(
    "setup,needle",
    [
        ({"merge_policy": "sometimes"}, "merge_policy"),
        ({"base_rule": "fixed"}, "base_fixed: required"),
        ({"base_rule": "fixed", "base_fixed": "bad..name"}, "not a valid branch"),
        ({"base_rule": "release", "release_pattern": "release"}, "needs a placeholder"),
        ({"base_rule": "release", "release_pattern": "r-{build}"}, "unknown placeholder"),
        ({"colour": "red"}, "unknown setup field"),
    ],
)
def test_invalid_setup_is_rejected(repo, env, setup, needle):
    with pytest.raises(c.ConductorError, match=needle):
        make(repo, env, **setup)


@pytest.mark.parametrize("bad", ["../escape", "", "a/b", "x" * 81])
def test_conductor_id_cannot_leave_the_state_root(repo, env, bad):
    with pytest.raises(c.ConductorError, match="invalid conductor id"):
        c.state_path(bad, repo, env)


def test_answers_keep_only_tuning_and_provider_ids(repo, env):
    state = make(repo, env)
    stored = c.set_answers(
        state, {**TUNING, "worktree": "current", "input.tickets": "X-1", "step-select": ["a"]}
    )
    assert stored == TUNING
    with pytest.raises(c.ConductorError, match="no harness"):
        c.set_answers(state, {"input.tickets": "X-1"})


def test_setup_from_bash_step_environment(repo, env):
    setup = c.setup_from_env(
        {"WISE_BASE_RULE": "fixed", "WISE_BASE_FIXED": "develop", "WISE_X": "1"}
    )
    assert setup == {"base_rule": "fixed", "base_fixed": "develop"}


# -- base-branch rule --------------------------------------------------------


@pytest.mark.parametrize(
    "version,pattern,branch",
    [
        ("26.11.0", "release-{version}", "release-26-11-0"),
        ("26.11.0", "release/{raw}", "release/26.11.0"),
        ("4.2", "rel-{major}-{minor}-{patch}", "rel-4-2-0"),
    ],
)
def test_release_mapping(version, pattern, branch):
    assert c.map_release(version, pattern) == branch


def test_base_rule_per_ticket(repo, env):
    default = make(repo, env)["setup"]
    assert c.resolve_base(default, "main", ["26.11.0"]) == {"base": "main", "source": "default"}
    fixed = make(repo, env, base_rule="fixed", base_fixed="develop")["setup"]
    assert c.resolve_base(fixed, "main")["base"] == "develop"
    release = make(repo, env, base_rule="release")["setup"]
    # L4: two tickets in one effort get different bases.
    assert c.resolve_base(release, "main", ["26.11.0"])["base"] == "release-26-11-0"
    assert c.resolve_base(release, "main", ["26.12.1"])["base"] == "release-26-12-1"
    assert c.resolve_base(release, "main", [])["base"] == "main"
    assert "ambiguous" in c.resolve_base(release, "main", ["26.11.0", "26.12.0"])["error"]
    held = make(repo, env, base_rule="release", release_missing="stop")["setup"]
    assert "no fixVersion" in c.resolve_base(held, "main", [])["error"]


def test_release_value_that_breaks_a_branch_name_is_rejected():
    with pytest.raises(c.ConductorError, match="invalid branch"):
        c.map_release("26..1", "release-{raw}")


# -- ledger ------------------------------------------------------------------


def test_ledger_rows_names_and_updates(repo, env):
    state = make(repo, env)
    first = c.add_child(state, "ticket", "PROJ-12", "Fix it", base="main")
    assert first["name"] == "proj-12" and first["state"] == "starting"
    url = c.add_child(state, "ticket", "https://linear.app/acme/issue/PROJ-12x")
    assert url["name"] == "proj-12x"
    topic = c.add_child(state, "topic", "Investigate flaky e2e")
    assert topic["kind"] == "topic" and topic["name"] == "investigate-flaky-e2e"
    c.update_child(
        state, "PROJ-12", session_id="local_123", pr="77", state="running", branch="PROJ-12"
    )
    row = c.find_child(state, "PROJ-12")
    assert row["pr"] == 77 and row["session_id"] == "local_123"
    c.save(state, env)
    assert c.find_child(c.load("local_conductor", repo, env), "PROJ-12")["pr"] == 77


@pytest.mark.parametrize(
    "fields,needle",
    [
        ({"state": "done"}, "state"),
        ({"session_id": "PROJ 12 child"}, "local_"),
        ({"pr": "abc"}, "PR number"),
        ({"colour": 1}, "unknown ledger fields"),
    ],
)
def test_ledger_rejects_bad_updates(repo, env, fields, needle):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1")
    with pytest.raises(c.ConductorError, match=needle):
        c.update_child(state, "PROJ-1", **fields)


def test_duplicate_child_and_same_name(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1")
    with pytest.raises(c.ConductorError, match="already has a child"):
        c.add_child(state, "ticket", "PROJ-1")
    assert c.add_child(state, "ticket", "proj_1")["name"] == "proj-1-2"


# -- brief generation --------------------------------------------------------


def test_payload_answers_every_ticket_auto_question(repo, env):
    state = make(repo, env)
    child = c.add_child(state, "ticket", "PROJ-1", base="release-26-11-0")
    payload = c.ticket_auto_payload(state, child, ticket())
    definition = load_and_validate({"path": str(ROOT / "workflows/ticket-auto/workflow.yaml")})[
        "def"
    ]
    done = p.complete_answers(definition, {"harnesses": ["claude"]}, payload["answers"])
    # L8: nothing left for a child to ask.
    assert done["missing"] == []
    assert payload["inputs"]["base_branch"] == "release-26-11-0"
    assert payload["context"]["decisions"]["merge_policy"] == "never"
    assert payload["context"]["decisions"]["pr_draft"] == "yes"
    # L1: the full ticket text rides in the run context, comments included.
    body = payload["context"]["ticket"][0]["body"]
    assert "trap focus" in body and "Esc must close it" in body


def test_payload_needs_stored_answers(repo, env):
    state = c.init("bare", repo, {}, None, env=env)
    child = c.add_child(state, "ticket", "PROJ-1", base="main")
    with pytest.raises(c.ConductorError, match="set-answers"):
        c.ticket_auto_payload(state, child, ticket())


def test_green_merge_policy_keeps_ticket_auto_merging(repo, env):
    state = make(repo, env, merge_policy="green", pr_state="ready")
    child = c.add_child(state, "ticket", "PROJ-1", base="main")
    decisions = c.ticket_auto_payload(state, child, ticket())["context"]["decisions"]
    assert decisions["merge_policy"] == "merge" and decisions["pr_draft"] == "no"


def test_ticket_brief_states_every_fixed_answer(repo, env):
    state = make(repo, env)
    child = c.add_child(
        state, "ticket", "PROJ-1", base="release-26-11-0", base_source="fixVersion 26.11.0"
    )
    siblings = [{"ref": "PROJ-2", "pr": 41, "base": "release-26-11-0", "notes": "drawer focus"}]
    brief = c.build_ticket_brief(state, child, ticket(), siblings, "local_conductor")
    for needle in (
        "Key: PROJ-1",  # 1 ticket
        "QA: Esc must close it",
        "git fetch origin release-26-11-0",  # 2 worktree (L3)
        "git worktree add ../shop.PROJ-1 -b PROJ-1 origin/release-26-11-0",
        "Never edit, stash, reset or check out the main checkout",
        '"workflow": "ticket-auto"',  # 3 setup
        '"model.all": "opus"',
        "never merge",
        "draft during the run",
        "Questions: none",
        "Post nothing to the tracker",
        "AUTH_REQUIRED",
        "Ambiguous scope",
        "`Overlap` section",  # 4 overlap (L9)
        "PROJ-2: PR 41",
        "every expected job ran",  # 5 done (L5, L6, L11, L13)
        "Do not end your turn while CI runs",
        "fails on the old code",
        "did not verify by hand",
        "`local_conductor`",  # 6 conductor
    ):
        assert needle in brief, needle


def test_brief_follows_the_policy_choices(repo, env):
    state = make(
        repo, env, questions="allowed", auth_fallback="direct", overlap="stop", merge_policy="green"
    )
    child = c.add_child(state, "ticket", "PROJ-1", base="main")
    brief = c.build_ticket_brief(state, child, ticket())
    assert "Send them to the conductor" in brief
    assert "Implement the ticket directly" in brief
    assert "stop and report it to the conductor" in brief
    assert "merged after green CI" in brief
    assert "Your final reply is your report" in brief


def test_topic_brief_has_no_workflow(repo, env):
    state = make(repo, env)
    child = c.add_child(state, "topic", "Investigate X")
    brief = c.build_topic_brief(state, child, "Find why X is slow.", "local_conductor")
    assert "No workflow applies" in brief and "Find why X is slow." in brief
    assert "ticket-auto" not in brief and "`local_conductor`" in brief


# -- verification ------------------------------------------------------------


def checks(*buckets):
    return [{"name": f"job-{i}", "bucket": b} for i, b in enumerate(buckets)]


@pytest.mark.parametrize(
    "rows,expected,verdict",
    [
        (checks("pass", "pass"), None, "green"),
        (checks("pass", "fail"), None, "red"),
        (checks("pass", "pending"), None, "pending"),
        (checks("skipping"), None, "incomplete"),
        ([], None, "incomplete"),
        (checks("pass"), ["job-0", "job-1"], "incomplete"),
    ],
)
def test_check_classification(rows, expected, verdict):
    assert c.classify_checks(rows, expected)["verdict"] == verdict


def test_verify_learns_expected_jobs_and_catches_dropped_ones(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main")
    c.add_child(state, "ticket", "PROJ-2", base="main")
    pr = {"number": 5, "state": "OPEN", "isDraft": False, "baseRefName": "main"}
    first = c.verify_child(state, "PROJ-1", pr, checks("pass", "pass", "skipping"), [])
    assert first["done"] and first["state"] == "ready"
    # Only passed jobs form the baseline: a skipped job is not expected.
    assert state["expected_checks"]["main"] == ["job-0", "job-1"]
    # L5: a later run drops a job; "no failures" is not green.
    second = c.verify_child(state, "PROJ-2", {**pr, "number": 6}, checks("pass"), [])
    assert second["ci"]["verdict"] == "incomplete" and not second["done"]
    assert "Rerun the missing jobs" in " ".join(second["nudges"])


def test_verify_nudges_a_draft_with_pending_ci_and_a_wrong_base(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="release-26-11-0")
    pr = {"number": 5, "state": "OPEN", "isDraft": True, "baseRefName": "main"}
    result = c.verify_child(state, "PROJ-1", pr, checks("pending"), [{"thread_id": "T1"}])
    text = " ".join(result["nudges"])
    assert "draft and CI is pending" in text and "Retarget" in text and "1 review thread" in text
    row = c.find_child(state, "PROJ-1")
    assert row["state"] == "running" and row["open_threads"] == 1 and row["pr"] == 5


def test_verify_flags_a_merge_under_never_policy(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main")
    pr = {"number": 5, "state": "MERGED", "isDraft": False, "baseRefName": "main"}
    result = c.verify_child(state, "PROJ-1", pr, checks("pass"), [])
    assert result["state"] == "merged" and any(
        "merge policy is never" in n for n in result["nudges"]
    )


# -- review-comment fan-out --------------------------------------------------


def graphql(*threads):
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": list(threads)}}}}}


def thread(tid, resolved=False, body="Announce empty input"):
    return {
        "id": tid,
        "isResolved": resolved,
        "path": "src/Search.tsx",
        "line": 12,
        "comments": {"nodes": [{"databaseId": 900, "body": body, "author": {"login": "copilot"}}]},
    }


def test_unresolved_threads_skip_resolved_ones():
    rows = c.unresolved_threads(graphql(thread("T1"), thread("T2", resolved=True)))
    assert [r["thread_id"] for r in rows] == ["T1"]
    assert rows[0]["comment_ids"] == [900] and rows[0]["author"] == "copilot"


def test_review_fanout_routes_threads_to_the_owning_child(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main", pr=5, session_id="local_a")
    c.add_child(state, "ticket", "PROJ-2", base="main", pr=6, session_id="local_b")
    by_pr = {
        "5": c.unresolved_threads(
            graphql(thread("T1"), thread("T2", body="Drawer does not trap focus"))
        ),
        "6": [],
        "9": c.unresolved_threads(graphql(thread("T9"))),
    }
    messages = c.review_fanout(state, by_pr)
    assert [m["pr"] for m in messages] == [5, 9]
    first = messages[0]
    assert first["owner"] == "PROJ-1" and first["session_id"] == "local_a"
    assert "`T1`" in first["message"] and "`T2`" in first["message"] and "900" in first["message"]
    # L11: the instruction demands a test that fails without the fix.
    assert "fails without the fix" in first["message"] and "resolve it" in first["message"]
    assert messages[1]["owner"] is None and "no child" in messages[1]["error"]
    assert c.find_child(state, "PROJ-1")["open_threads"] == 2


# -- runtime mode and status -------------------------------------------------


def test_desktop_detection_needs_start_and_messaging_tools():
    desktop = c.detect_mode(["mcp__ccd_session__start_session", "SendMessage", "Bash"])
    assert desktop == {"mode": "desktop", "notice": ""}
    for tools in (["SendMessage"], ["mcp__ccd_session__start_session"], []):
        fallback = c.detect_mode(tools)
        assert fallback["mode"] == "fallback" and "sidebar tree is not shown" in fallback["notice"]


def test_status_table_carries_manual_qa_items(repo, env):
    state = make(repo, env)
    c.add_child(
        state, "ticket", "PROJ-1", base="main", pr=5, manual_qa=["screen reader on the drawer"]
    )
    c.add_child(state, "topic", "Investigate X", notes="decision: keep the old API")
    table = c.status_table(state)
    assert "| open | PROJ-1 | ticket | proj-1 | main | 5 | starting |" in table
    assert "PROJ-1: manual QA: screen reader on the drawer" in table
    assert "Investigate X: decision: keep the old API" in table


# -- CLI (the path the skill and the fallback use) ---------------------------


def test_cli_round_trip(repo, env, tmp_path, monkeypatch, capsys):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("WISE_BASE_RULE", "release")
    run = lambda *args: helpers_main(["conductor", *args, "--cwd", str(repo)])  # noqa: E731
    assert run("init", "--id", "c1", "--mode", "fallback", "--from-env") == 0
    capsys.readouterr()
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({**TUNING, "input.tickets": "PROJ-1"}))
    assert run("set-answers", "--id", "c1", "--answers-json", str(answers)) == 0
    assert json.loads(capsys.readouterr().out) == TUNING
    assert run("base", "--id", "c1", "--default-branch", "main", "--release", "26.11.0") == 0
    base = json.loads(capsys.readouterr().out)
    assert base["base"] == "release-26-11-0"
    fields = tmp_path / "fields.json"
    fields.write_text(json.dumps({"base": base["base"]}))
    assert (
        run(
            "add-child",
            "--id",
            "c1",
            "--kind",
            "ticket",
            "--ref",
            "PROJ-1",
            "--fields-json",
            str(fields),
        )
        == 0
    )
    body = tmp_path / "ticket.json"
    body.write_text(json.dumps(ticket()))
    capsys.readouterr()
    assert run("brief", "--id", "c1", "--ref", "PROJ-1", "--ticket-json", str(body)) == 0
    assert "# Ticket child: PROJ-1" in capsys.readouterr().out
    threads = tmp_path / "threads.json"
    threads.write_text(json.dumps({"5": graphql(thread("T1"))}))
    fields.write_text(json.dumps({"pr": 5}))
    assert run("update-child", "--id", "c1", "--ref", "PROJ-1", "--fields-json", str(fields)) == 0
    capsys.readouterr()
    assert run("fanout", "--id", "c1", "--threads-json", str(threads)) == 0
    assert json.loads(capsys.readouterr().out)[0]["owner"] == "PROJ-1"
    assert run("status", "--id", "c1") == 0
    assert "| PROJ-1 |" in capsys.readouterr().out
    assert run("show", "--id", "missing") == 2
    assert "no conductor state" in capsys.readouterr().err
    assert helpers_main(["conductor", "mode", "--tools", "Bash"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "fallback"


def test_conductor_workflow_validates_and_records_setup(repo, env):
    result = load_and_validate({"path": str(ROOT / "workflows/conductor/workflow.yaml")})
    assert "def" in result, result
    names = [item["name"] for item in result["def"]["inputs"]]
    assert set(c.SETUP_FIELDS) <= set(names)
    for item in result["def"]["inputs"]:
        if item["name"] in c.SETUP_FIELDS and item.get("default") is not None:
            assert item["default"] == c.SETUP_FIELDS[item["name"]][1], item["name"]


# -- tracker -----------------------------------------------------------------


def open_pr(number=5, **over):
    pr = {
        "number": number,
        "url": f"https://github.com/o/r/pull/{number}",
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "main",
        "headRefOid": "a1",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
    }
    pr.update(over)
    return pr


def fact(pr, *buckets, threads=()):
    return {"pr": pr, "checks": checks(*buckets), "threads": graphql(*threads)}


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_poll_and_stall_setup_is_validated(repo, env):
    assert make(repo, env)["setup"]["poll_seconds"] == "60"
    with pytest.raises(c.ConductorError, match="poll_seconds"):
        make(repo, env, poll_seconds="5")
    with pytest.raises(c.ConductorError, match="stall_minutes"):
        make(repo, env, stall_minutes="soon")


def test_status_is_in_work_order(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main", pr=1, state="merged")
    c.add_child(state, "ticket", "PROJ-2", base="main", pr=2, ci={"verdict": "green"})
    c.add_child(state, "ticket", "PROJ-3", base="main")
    c.add_child(state, "ticket", "PROJ-4", base="main", pr=4, conflict=True)
    c.add_child(state, "ticket", "PROJ-5", base="main", pr=5, ci={"verdict": "red"})
    order = [(c.pr_group(r), r["ref"]) for r in c.ordered_children(state)]
    assert order == [
        ("blocked", "PROJ-4"),
        ("blocked", "PROJ-5"),
        ("open", "PROJ-2"),
        ("working", "PROJ-3"),
        ("merged", "PROJ-1"),
    ]


def test_conflict_blocks_the_row_and_nudges(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main")
    pr = open_pr(mergeable="CONFLICTING")
    result = c.verify_child(state, "PROJ-1", pr, checks("pass"), [])
    assert result["conflict"] and result["group"] == "blocked" and not result["done"]
    assert any("merge conflicts" in n for n in result["nudges"])


def test_apply_facts_reports_each_change_once(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main", branch="PROJ-1")

    def kinds(events):
        return [e["kind"] for e in events]

    draft = open_pr(isDraft=True)
    assert kinds(c.apply_facts(state, {"PROJ-1": fact(draft, "pending")}, now=100)) == [
        "pr-opened",
        "ci",
    ]
    assert c.apply_facts(state, {"PROJ-1": fact(draft, "pending")}, now=160) == []
    pushed = open_pr(isDraft=True, headRefOid="b2")
    red = c.apply_facts(state, {"PROJ-1": fact(pushed, "fail")}, now=220)
    assert kinds(red) == ["ci"] and red[0]["group"] == "blocked" and "red" in red[0]["text"]
    conflicted = fact(open_pr(mergeable="CONFLICTING"), "pass", threads=[thread("T1")])
    ready = c.apply_facts(state, {"PROJ-1": conflicted}, now=280)
    assert kinds(ready) == ["ready", "ci", "conflict", "threads"]
    merged = c.apply_facts(state, {"PROJ-1": fact(open_pr(state="MERGED"), "pass")}, now=340)
    assert "merged" in kinds(merged) and c.find_child(state, "PROJ-1")["state"] == "merged"
    assert c.tracked(state) == []


def test_stalled_child_is_reported_once_per_silence(repo, env):
    state = make(repo, env, stall_minutes="10")
    c.add_child(state, "ticket", "PROJ-1", base="main", session_id="local_abc")
    seen = c.find_child(state, "PROJ-1")["last_seen"]
    assert c.stall_events(state, seen + 599) == []
    events = c.stall_events(state, seen + 600)
    assert [e["kind"] for e in events] == ["stalled"]
    assert events[0]["session_id"] == "local_abc"
    assert c.stall_events(state, seen + 1200) == []
    c.mark_seen(state, "PROJ-1", now=seen + 1300)
    assert len(c.stall_events(state, seen + 1900)) == 1


def test_a_new_push_counts_as_child_activity(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main", pr=5)
    c.apply_facts(state, {"PROJ-1": fact(open_pr(headRefOid="b2"), "pending")}, now=10_000_000)
    assert c.find_child(state, "PROJ-1")["last_seen"] == 10_000_000


def test_watch_prints_events_and_writes_a_heartbeat(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main", branch="PROJ-1")
    c.save(state, env)
    out, clock = io.StringIO(), Clock()
    code = c.watch(
        "local_conductor",
        repo,
        env,
        fetcher=lambda row: fact(open_pr(), "pass"),
        out=out,
        once=True,
        clock=clock,
        sleep=clock.sleep,
    )
    assert code == 0
    assert "EVENT pr-opened C1 PROJ-1 PR 5 [open]: opened PR 5" in out.getvalue()
    assert c.load("local_conductor", repo, env)["children"][0]["pr"] == 5
    health = c.watch_health("local_conductor", repo, env, int(clock.now))
    assert health["status"] == "stopped" and health["action"] == "start"


def test_watch_stops_after_repeated_failures(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main", pr=5)
    c.save(state, env)

    def broken(row):
        raise c.ConductorError("gh pr view: no answer in 30s")

    out, clock = io.StringIO(), Clock()
    code = c.watch(
        "local_conductor", repo, env, fetcher=broken, out=out, clock=clock, sleep=clock.sleep
    )
    assert code == 3 and "stopped after 5 failed passes" in out.getvalue()
    health = c.watch_health("local_conductor", repo, env, int(clock.now))
    assert health["status"] == "failed" and "no answer" in health["reason"]


def test_watch_ends_at_max_runtime(repo, env):
    make(repo, env)
    out, clock = io.StringIO(), Clock()
    code = c.watch(
        "local_conductor",
        repo,
        env,
        fetcher=lambda row: {},
        out=out,
        max_minutes=3,
        clock=clock,
        sleep=clock.sleep,
    )
    assert code == 0 and "max runtime reached" in out.getvalue()


def test_watch_health_finds_a_stuck_watcher_and_a_second_watcher_exits(repo, env):
    make(repo, env)
    beat = c.heartbeat_path("local_conductor", repo, env)
    live = {"pid": os.getpid(), "status": "running", "interval": 60, "last_pass": 1000}
    c._write_json(beat, live)
    assert c.watch_health("local_conductor", repo, env, now=1100)["status"] == "running"
    stuck = c.watch_health("local_conductor", repo, env, now=1000 + 3 * 60 + 4 * 30 + 1)
    assert stuck["status"] == "stuck" and stuck["action"] == "reap"
    # A live, fresh heartbeat of another process: a second watcher exits.
    c._write_json(beat, {**live, "pid": os.getppid()})
    out = io.StringIO()
    code = c.watch(
        "local_conductor", repo, env, fetcher=lambda row: {}, out=out, clock=lambda: 1010.0
    )
    assert code == 0 and "already running" in out.getvalue()


def test_watch_health_reports_a_dead_or_absent_watcher(repo, env):
    make(repo, env)
    assert c.watch_health("local_conductor", repo, env, now=0)["status"] == "absent"
    beat = c.heartbeat_path("local_conductor", repo, env)
    dead = {"pid": 999_999_999, "status": "running", "interval": 60, "last_pass": 1000}
    c._write_json(beat, dead)
    health = c.watch_health("local_conductor", repo, env, now=1010)
    assert health["status"] == "dead" and health["action"] == "start"


def test_an_expected_job_that_is_skipped_now_is_not_green():
    rows = [{"name": "lint", "bucket": "pass"}, {"name": "e2e", "bucket": "skipping"}]
    result = c.classify_checks(rows, ["lint", "e2e"])
    assert result["verdict"] == "incomplete" and result["missing"] == ["e2e"]


def test_two_release_values_are_ambiguous_even_on_one_branch(repo, env):
    state = make(repo, env, base_rule="release", release_pattern="release-{major}")
    result = c.resolve_base(state["setup"], "main", ["26.11.0", "26.12.0"])
    assert "ambiguous" in result["error"]
    assert c.resolve_base(state["setup"], "main", ["26.11.0"])["base"] == "release-26"


def test_a_pr_on_the_wrong_base_is_not_ready(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="release-26-11-0")
    result = c.verify_child(state, "PROJ-1", open_pr(), checks("pass"), [])
    assert result["state"] != "ready" and not result["done"] and result["group"] == "blocked"


def test_a_merged_pr_with_open_threads_is_not_done(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main")
    pr = open_pr(state="MERGED")
    open_thread = [{"thread_id": "T1"}]
    assert not c.verify_child(state, "PROJ-1", pr, checks("pass"), open_thread)["done"]
    assert c.verify_child(state, "PROJ-1", pr, checks("pass"), [])["done"]


def test_verify_refuses_a_pr_that_is_not_the_rows(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main", branch="PROJ-1", pr=5)
    c.add_child(state, "ticket", "PROJ-2", base="main", branch="PROJ-2")
    with pytest.raises(c.ConductorError, match="not its PR"):
        c.verify_child(state, "PROJ-2", open_pr(6, headRefName="PROJ-1"), checks("pass"), [])
    with pytest.raises(c.ConductorError, match="already belongs to PROJ-1"):
        c.verify_child(state, "PROJ-2", open_pr(5), checks("pass"), [])
    assert c.find_child(state, "PROJ-2")["pr"] is None
    fact_for = {"PROJ-2": fact(open_pr(6, headRefName="PROJ-1"), "pass")}
    assert [e["kind"] for e in c.apply_facts(state, fact_for, now=1)] == ["pr-mismatch"]
    assert c.apply_facts(state, fact_for, now=2) == []


def test_threads_from_every_page_are_counted():
    pages = [graphql(thread("T1")), graphql(thread("T2"), thread("T3", resolved=True))]
    assert [t["thread_id"] for t in c.unresolved_threads(pages)] == ["T1", "T2"]


def test_direct_worktree_command_quotes_a_url_ref(repo, env):
    state = make(repo, env, auth_fallback="direct")
    url = "https://tracker.example/browse/PROJ-9?focus=a&x=1"
    child = c.add_child(state, "ticket", url, base="main")
    brief = c.build_ticket_brief(state, child, ticket(url))
    line = next(x for x in brief.splitlines() if x.startswith("git worktree add"))
    assert line == "git worktree add ../shop.PROJ-9 -b PROJ-9 origin/main"
    assert c.branch_key(url) == "PROJ-9"


def test_learned_checks_reach_a_new_conductor(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main")
    c.verify_child(state, "PROJ-1", open_pr(), checks("pass", "pass"), [])
    c.save(state, env)
    later = c.init("local_later", repo, {}, TUNING, "desktop", "", env)
    assert later["expected_checks"] == {"main": ["job-0", "job-1"]}


def test_child_codes_are_stable_and_never_reused(repo, env):
    state = make(repo, env)
    for ref in ("PROJ-1", "PROJ-2", "PROJ-3"):
        c.add_child(state, "ticket", ref, base="main")
    assert [r["code"] for r in state["children"]] == ["C1", "C2", "C3"]
    state["children"].pop(1)
    c.add_child(state, "ticket", "PROJ-4", base="main")
    assert [r["code"] for r in state["children"]] == ["C1", "C3", "C4"]
    c.update_child(state, "PROJ-1", state="merged")
    assert c.find_child(state, "PROJ-1")["code"] == "C1"


def test_rows_from_before_codes_get_them_on_load(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main")
    c.add_child(state, "ticket", "PROJ-2", base="main")
    for row in state["children"]:
        del row["code"]
    del state["next_code"]
    c.save(state, env)
    loaded = c.load("local_conductor", repo, env)
    assert [r["code"] for r in loaded["children"]] == ["C1", "C2"]


def test_status_and_human_items_carry_codes(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main", pr=5, manual_qa=["zoom", "keyboard"])
    c.add_child(state, "topic", "Investigate X", notes="decision: keep the old API")
    table = c.status_table(state)
    assert "| C1 | open | PROJ-1 |" in table and "| C2 | working | Investigate X |" in table
    assert "N1 C1 PROJ-1: manual QA: zoom" in table
    assert "N2 C1 PROJ-1: manual QA: keyboard" in table
    assert "N3 C2 Investigate X: decision: keep the old API" in table


def test_human_item_codes_stay_after_an_earlier_item_closes(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main", pr=5, manual_qa=["zoom", "keyboard"])
    c.add_child(state, "topic", "Investigate X", notes="decision: keep the old API")
    c.status_table(state)
    c.update_child(state, "PROJ-1", manual_qa=["keyboard"])
    table = c.status_table(state)
    assert "N2 C1 PROJ-1: manual QA: keyboard" in table
    assert "N3 C2 Investigate X: decision" in table and "N1 " not in table


def test_a_removed_last_row_keeps_its_code_retired(repo, env):
    state = make(repo, env)
    for ref in ("PROJ-1", "PROJ-2", "PROJ-3"):
        c.add_child(state, "ticket", ref, base="main")
    state["children"].pop()
    c.add_child(state, "ticket", "PROJ-4", base="main")
    assert c.find_child(state, "PROJ-4")["code"] == "C4"


def test_reinit_keeps_a_retired_code_retired(repo, env):
    state = make(repo, env)
    for ref in ("PROJ-1", "PROJ-2", "PROJ-3"):
        c.add_child(state, "ticket", ref, base="main")
    state["children"].pop()
    c.save(state, env)
    again = c.init("local_conductor", repo, {}, None, "desktop", env=env)
    c.add_child(again, "ticket", "PROJ-4", base="main")
    assert c.find_child(again, "PROJ-4")["code"] == "C4"


def test_a_duplicate_code_gets_a_new_one(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main")
    c.add_child(state, "ticket", "PROJ-2", base="main")
    state["children"][1]["code"] = "C1"
    c.assign_codes(state)
    assert [r["code"] for r in state["children"]] == ["C1", "C3"]


def test_find_child_accepts_the_code(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main")
    assert c.find_child(state, "C1")["ref"] == "PROJ-1"
    assert c.find_child(state, "C9") is None


def test_latest_gives_legacy_rows_codes(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main")
    del state["children"][0]["code"], state["next_code"]
    c.save(state, env)
    assert c.latest(repo, env)["children"][0]["code"] == "C1"


def test_thread_codes_stay_the_same_in_later_rounds(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main", pr=5, session_id="local_a")
    first = {"5": c.unresolved_threads(graphql(thread("T-a"), thread("T-b")))}
    c.review_fanout(state, first)
    second = {"5": c.unresolved_threads(graphql(thread("T-b"), thread("T-c")))}
    message = c.review_fanout(state, second)[0]["message"]
    assert "\nT2 thread `T-b`" in message and "\nT3 thread `T-c`" in message


def test_briefs_give_the_child_its_code_and_the_contract(repo, env):
    state = make(repo, env)
    child = c.add_child(state, "ticket", "PROJ-1", base="main")
    topic = c.add_child(state, "topic", "Investigate X")
    ticket_brief = c.build_ticket_brief(state, child, ticket())
    topic_brief = c.build_topic_brief(state, topic, "Look at X.")
    assert "Your conductor code is `C1`" in ticket_brief and c.REF_CODES in ticket_brief
    assert "Your conductor code is `C2`" in topic_brief and c.REF_CODES in topic_brief


def test_review_round_numbers_threads_by_t_code(repo, env):
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main", pr=5, session_id="local_a")
    raw = {"5": c.unresolved_threads(graphql(thread("T-a"), thread("T-b")))}
    message = c.review_fanout(state, raw)[0]
    assert message["code"] == "C1"
    assert message["message"].startswith("C1 review-comment round for PR 5")
    assert "\nT1 thread `T-a`" in message["message"] and "\nT2 thread `T-b`" in message["message"]
    assert "by its T code" in message["message"]


def test_seen_cli_resets_the_stall_timer(repo, env, monkeypatch):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    state = make(repo, env)
    c.add_child(state, "ticket", "PROJ-1", base="main", last_seen=1)
    c.save(state, env)
    args = ["conductor", "seen", "--id", "local_conductor", "--ref", "PROJ-1", "--cwd", str(repo)]
    assert helpers_main(args) == 0
    assert c.load("local_conductor", repo, env)["children"][0]["last_seen"] > 1
