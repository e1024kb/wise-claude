import json
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
    assert state["expected_checks"]["main"] == ["job-0", "job-1", "job-2"]
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
    assert "| PROJ-1 | ticket | proj-1 | main | 5 | starting |" in table
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
