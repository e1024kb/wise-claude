"""Epic fan-out: expansion parsing, the dependency DAG scheduler in the
`units` step, the fan-out pre-flight inputs and the per-child safeguards
(duplicate PR, merged verdict, bot-only comments, cursor trailers,
sequence collisions, base rebase). The daemon restart guard is in
test_daemon.py, the executor-level parallel+current rejection in
test_executor.py."""

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from test_model_phases import ModelFixture, answer, watch_output
from test_phases import PhaseFixture, command_result
from wise_engine import preflight as p
from wise_engine.defs import validate_def
from wise_engine.fanout import (
    ExpansionError,
    concurrency_of,
    dependency_edges,
    find_cycle,
    is_terminal_state,
    parse_item_specs,
    parse_repo_paths,
    repo_slug,
    resolve_repo,
)
from wise_engine.ledger import read_unit
from wise_engine.phases.claim import claim_phase
from wise_engine.phases.push import rebase_onto_base, sequence_collisions, sequence_number
from wise_engine.phases.trailers import strip_cursor_trailers, strip_trailer
from wise_engine.phases.common import spawn_runner
from wise_engine.spawn import clean_env
from wise_engine.units import run_units_step

# --- expansion parsing -------------------------------------------------------


def test_parse_item_specs_comma_list_and_json():
    assert parse_item_specs("A-1, B-2;A-1\nC-3") == [
        {"ref": "A-1"},
        {"ref": "B-2"},
        {"ref": "C-3"},
    ]
    specs = parse_item_specs(
        json.dumps(
            [
                "A-1",
                {
                    "ref": "B-2",
                    "depends_on": "A-1",
                    "serialize": ["migrations", ""],
                    "state": "Todo",
                },
                {"ref": " "},
                {"title": "no ref"},
                False,
                {"ref": "A-1", "title": "duplicate"},
            ]
        )
    )
    assert specs == [
        {"ref": "A-1"},
        {"ref": "B-2", "state": "Todo", "depends_on": ["A-1"], "serialize": ["migrations"]},
    ]


def test_parse_item_specs_is_strict_on_a_json_array():
    with pytest.raises(ExpansionError, match="not a JSON array"):
        parse_item_specs('["A-1", ')
    assert parse_item_specs("[]") == []


@pytest.mark.parametrize(
    "state,terminal",
    [
        ("Done", True),
        ("CANCELED", True),
        ("Cancelled", True),
        ("Duplicate", True),
        ("Won't do", True),
        ("wont_do", True),
        ("Todo", False),
        ("Backlog", False),
        ("In Progress", False),
        (None, False),
    ],
)
def test_terminal_states(state, terminal):
    assert is_terminal_state(state) is terminal


def node(ref, depends_on=()):
    spec = {"ref": ref, "depends_on": list(depends_on)} if depends_on else {"ref": ref}
    return {"spec": spec, "unit": {"ref": ref}, "key": ref.lower()}


def test_dependency_edges_and_unknown_blocker():
    logs = []
    edges = dependency_edges(
        [node("A-1"), node("B-2", ["A-1", "EXT-9"]), node("C-3", ["#B-2", "C-3"])], logs.append
    )
    assert edges == {"a-1": [], "b-2": ["a-1"], "c-3": ["b-2"]}
    assert logs == ["[B-2] depends on EXT-9, which is not in this run; not waited for"]


def test_dependency_cycle_is_rejected():
    assert find_cycle({"a": ["b"], "b": ["c"], "c": ["a"]}) == ["a", "b", "c", "a"]
    with pytest.raises(ExpansionError, match="dependency cycle: A-1 -> B-2 -> A-1"):
        dependency_edges([node("A-1", ["B-2"]), node("B-2", ["A-1"])], lambda line: None)


def test_concurrency_input_wins_over_the_step():
    assert concurrency_of({"parallel": 2}, {"concurrency": "3"}) == 3
    assert concurrency_of({"parallel": 2}, {"concurrency": "9"}) == 2
    assert concurrency_of({}, {}) == 1


# --- target repository -------------------------------------------------------


def test_repo_slug_and_paths():
    assert repo_slug("git@github.com:Acme/API.git") == "acme/api"
    assert repo_slug("https://github.com/acme/web/") == "acme/web"
    assert parse_repo_paths("acme/api=/x/api, acme/web.git = ~/web,junk") == {
        "acme/api": "/x/api",
        "acme/web": str(Path("~/web").expanduser()),
    }


def test_resolve_repo(tmp_path):
    project, sibling, mapped = (tmp_path / name for name in ("project", "sibling", "mapped"))
    for path in (project, sibling, mapped):
        (path / ".git").mkdir(parents=True)
    origins = {str(sibling.resolve()): "git@github.com:acme/api.git"}

    async def origin_of(path):
        return origins.get(path)

    async def scenario():
        resolve = lambda wanted, paths={}: resolve_repo(  # noqa: E731
            wanted, str(project), "acme/app", paths, origin_of
        )
        assert await resolve(None) == str(project)
        assert await resolve("acme/app") == str(project)
        assert await resolve("acme/api") == str(sibling.resolve())
        assert await resolve("acme/web", {"acme/web": str(mapped)}) == str(mapped.resolve())
        assert await resolve(str(mapped)) == str(mapped.resolve())
        outside = tmp_path / "far" / "away"
        (outside / ".git").mkdir(parents=True)
        assert await resolve(str(outside)) is None
        assert await resolve(str(outside), {"acme/x": str(outside)}) == str(outside.resolve())
        assert await resolve("acme/unknown") is None

    asyncio.run(scenario())


# --- the DAG scheduler -------------------------------------------------------


class EpicFixture(ModelFixture):
    """A ticket-plan epic: each child's plan phase is scripted; `fail` names
    the refs whose plan fails, `order` records plan starts and `peak` the
    most plans in flight at once."""

    def __init__(self, root, items, **inputs):
        super().__init__(root)
        self.step.update({"pipeline": "ticket-plan", "groups": {}})
        self.state["inputs"] = {"worktree_mode": "new", **inputs}
        self.items = items
        self.fail = set()
        self.order = []
        self.active = 0
        self.peak = 0
        self.scripts["plan"] = self.plan

    async def plan(self, req, nth):
        ref = re.search(r"architect for ticket (\S+), one child", req["prompt"])[1]
        path = re.search(r"Write the plan to: (\S+)", req["prompt"])[1]
        self.order.append(ref)
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        if ref in self.fail:
            return answer({"plan_path": "", "status": "no-access"})
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(f"# {ref}\n")
        return answer({"plan_path": path, "status": "ready"})

    def run(self):
        result = asyncio.run(run_units_step(self.input(items=self.items)))
        return result, {row["unit"]["ref"]: row for row in result["outputs"]["units"]}


def test_sequential_children_follow_the_dependency_order(tmp_path):
    fixture = EpicFixture(
        tmp_path,
        [{"ref": "PROJ-2", "depends_on": ["PROJ-1"]}, {"ref": "PROJ-1"}],
        concurrency="1",
    )
    result, rows = fixture.run()
    assert fixture.order == ["PROJ-1", "PROJ-2"] and fixture.peak == 1
    assert {ref: row["verdict"] for ref, row in rows.items()} == {
        "PROJ-1": "plan-written",
        "PROJ-2": "plan-written",
    }
    assert rows["PROJ-1"]["reason"].startswith("plan: ")
    # Rows keep the expansion order, not the completion order.
    assert [row["unit"]["ref"] for row in result["outputs"]["units"]] == ["PROJ-2", "PROJ-1"]
    assert "plan-written=2" in result["verdict"]


def test_parallel_children_respect_the_concurrency_cap(tmp_path):
    fixture = EpicFixture(tmp_path, [f"PROJ-{n}" for n in range(1, 6)], concurrency="2")
    _, rows = fixture.run()
    assert fixture.peak == 2
    assert all(row["verdict"] == "plan-written" for row in rows.values())


def test_a_failed_blocker_blocks_its_dependents_transitively(tmp_path):
    fixture = EpicFixture(
        tmp_path,
        [
            {"ref": "PROJ-1"},
            {"ref": "PROJ-2", "depends_on": ["PROJ-1"]},
            {"ref": "PROJ-3", "depends_on": ["PROJ-2"]},
            {"ref": "PROJ-4"},
        ],
        concurrency="2",
    )
    fixture.fail = {"PROJ-1"}
    result, rows = fixture.run()
    assert rows["PROJ-1"]["verdict"] == "failed"
    assert rows["PROJ-2"]["verdict"] == "blocked" and rows["PROJ-2"]["blocked_by"] == ["PROJ-1"]
    assert rows["PROJ-2"]["reason"] == "dependency: PROJ-1 ended failed"
    assert rows["PROJ-3"]["verdict"] == "blocked" and rows["PROJ-3"]["blocked_by"] == ["PROJ-2"]
    assert rows["PROJ-4"]["verdict"] == "plan-written"
    assert "PROJ-2" not in fixture.order and "PROJ-3" not in fixture.order
    assert "blocked=2" in result["verdict"]


def test_a_child_without_a_checkout_blocks_its_dependents(tmp_path):
    fixture = EpicFixture(
        tmp_path,
        [
            {"ref": "PROJ-1", "repo": "acme/nowhere"},
            {"ref": "PROJ-2", "depends_on": ["PROJ-1"]},
        ],
        concurrency="1",
    )
    result, rows = fixture.run()
    assert rows["PROJ-1"]["verdict"] == "skipped"
    assert "no local checkout found" in rows["PROJ-1"]["reason"]
    assert rows["PROJ-2"]["verdict"] == "blocked" and rows["PROJ-2"]["blocked_by"] == ["PROJ-1"]
    assert fixture.order == []
    assert "not in this run" not in result["log"]


def test_on_child_failure_stop_starts_no_new_child(tmp_path):
    fixture = EpicFixture(tmp_path, ["PROJ-1", "PROJ-2"], concurrency="1", on_child_failure="stop")
    fixture.fail = {"PROJ-1"}
    _, rows = fixture.run()
    assert fixture.order == ["PROJ-1"]
    assert rows["PROJ-2"]["verdict"] == "skipped"
    assert rows["PROJ-2"]["reason"] == "on_child_failure=stop: PROJ-1 ended failed"


def test_children_sharing_a_serialize_key_never_overlap(tmp_path):
    fixture = EpicFixture(
        tmp_path,
        [
            {"ref": "PROJ-1", "serialize": ["migrations"]},
            {"ref": "PROJ-2", "serialize": ["migrations"]},
        ],
        concurrency="2",
    )
    _, rows = fixture.run()
    assert fixture.peak == 1
    assert all(row["verdict"] == "plan-written" for row in rows.values())


def test_terminal_tracker_state_is_skipped_without_a_child(tmp_path):
    fixture = EpicFixture(
        tmp_path, [{"ref": "PROJ-1", "state": "Done"}, {"ref": "PROJ-2"}], concurrency="2"
    )
    _, rows = fixture.run()
    assert (
        rows["PROJ-1"]["verdict"] == "skipped" and "tracker state Done" in rows["PROJ-1"]["reason"]
    )
    assert fixture.order == ["PROJ-2"]


def test_current_tree_runs_one_child_at_a_time(tmp_path):
    fixture = EpicFixture(
        tmp_path,
        ["PROJ-1", "PROJ-2", "PROJ-3"],
        concurrency="4",
        worktree_mode="current",
        branch_mode="current",
    )
    _, rows = fixture.run()
    assert fixture.peak == 1
    assert all(row["verdict"] == "plan-written" for row in rows.values())


def test_single_ticket_run_is_unchanged(tmp_path):
    """Regression: one plain ref through the ticket pipeline plans,
    implements, reviews, pushes, opens a PR and merges it, as before."""
    fixture = ModelFixture(tmp_path)
    result = asyncio.run(run_units_step(fixture.input()))
    row = result["outputs"]["units"][0]
    assert row["verdict"] == "merged" and row["unit"]["pr"]["number"] == 5
    assert result["verdict"] == "units=1 merged=1 open=0 failed=0 skipped=0"
    assert [phase for phase, _, _ in fixture.child_calls][:3] == ["plan", "implement", "review"]


# --- pre-flight --------------------------------------------------------------


def epic_definition():
    raw = {
        "version": 2,
        "name": "epic",
        "preflight": {"worktree": "current"},
        "tuning": {"groups": [{"id": "g", "default": {"harness": "claude", "model": "sonnet"}}]},
        "inputs": [
            {
                "name": "worktree_mode",
                "prompt": "?",
                "default": "new",
                "validate": "^(current|new)$",
            },
            {"name": "tickets", "prompt": "Tickets?", "from-context": "ticket[].ref"},
            {
                "name": "concurrency",
                "prompt": "Concurrency?",
                "default": "2",
                "validate": "^(1|2|3|4)$",
                "needs-fanout": "tickets",
            },
            {
                "name": "review_mode",
                "prompt": "Review?",
                "default": "auto",
                "validate": "^(auto|ask)$",
                "unless-fanout": "tickets",
            },
        ],
        "steps": [
            {"id": "a", "type": "agent", "group": "g", "prompt": "x", "when": "fanout != 'yes'"}
        ],
    }
    result = validate_def(raw, "epic.yaml")
    assert "def" in result, result
    return result["def"]


def test_fanout_inputs_are_asked_only_for_a_fanout_run():
    defn = epic_definition()
    single = p.build_questionary(defn, {"context": {"ticket": [{"ref": "A-1"}]}})
    assert "input.concurrency" not in ids(single) and "input.review_mode" in ids(single)
    several = p.build_questionary(defn, {}, {"input.tickets": "A-1, B-2"})
    assert "input.concurrency" in ids(several) and "input.review_mode" not in ids(several)
    epic = {"context": {"ticket": [{"ref": "E-1", "children": ["A-1", "B-2"]}]}}
    assert "input.concurrency" in ids(p.build_questionary(defn, epic))


def test_fanout_is_predicted_for_when_gates():
    defn = epic_definition()
    answers = {"worktree": "new", "input.tickets": "A-1", "permissions.claude": "auto"}
    assert "model.g" in ids(p.build_questionary(defn, {}, {**answers, "input.review_mode": "auto"}))
    fanned = {**answers, "input.tickets": "A-1,B-2", "input.concurrency": "2"}
    assert "model.g" not in ids(p.build_questionary(defn, {}, fanned))


def test_concurrency_defaults_to_one_in_the_current_tree():
    defn = epic_definition()
    result = p.build_questionary(defn, {}, {"worktree": "current", "input.tickets": "A-1,B-2"})
    concurrency = next(q for q in result["questions"] if q["id"] == "input.concurrency")
    assert concurrency["default"] == "1"


def test_parallel_with_the_current_tree_is_rejected():
    assert p.invalid_concurrency_ids({"worktree_mode": "current", "concurrency": "2"}, True) == [
        "input.concurrency"
    ]
    assert p.invalid_concurrency_ids({"worktree_mode": "current", "concurrency": "1"}, True) == []
    assert p.invalid_concurrency_ids({"worktree_mode": "new", "concurrency": "4"}, True) == []
    # An unasked default (single-ticket run) is not an answer.
    assert p.invalid_concurrency_ids({"worktree_mode": "current", "concurrency": "2"}, False) == []


def test_fanout_keys_are_validated():
    base = epic_definition()
    raw = {
        "version": 2,
        "name": "bad",
        "inputs": [
            {"name": "tickets", "prompt": "?"},
            {"name": "c", "prompt": "?", "needs-fanout": "nope", "default": "1"},
            {"name": "d", "prompt": "?", "unless-fanout": "tickets"},
            {
                "name": "e",
                "prompt": "?",
                "default": "1",
                "needs-fanout": "tickets",
                "unless-fanout": "tickets",
            },
        ],
        "steps": base["steps"],
        "tuning": {"groups": [{"id": "g", "default": {"harness": "claude", "model": "sonnet"}}]},
    }
    messages = [issue["message"] for issue in validate_def(raw, "bad.yaml")["issues"]]
    assert 'input "nope" is not another declared input' in messages
    assert any("unless-fanout needs a `default:`" in m for m in messages)
    assert "an input takes needs-fanout or unless-fanout, not both" in messages


def ids(result):
    return [q["id"] for q in result["questions"]]


# --- F1: a duplicate PR is resumed -------------------------------------------


class OpenPrFixture(PhaseFixture):
    def __init__(self, root, author="me"):
        super().__init__(root)
        self.open_prs = [
            {
                "number": 9,
                "url": "https://github.invalid/a/r/pull/9",
                "headRefName": "proj-1-2",
                "baseRefName": "release",
                "isCrossRepository": False,
                "author": {"login": author},
            },
            {
                "number": 4,
                "url": "https://github.invalid/fork/r/pull/4",
                "headRefName": "proj-1",
                "isCrossRepository": True,
                "author": {"login": author},
            },
        ]
        self.api["user"] = lambda args: command_result("me\n")

    async def execute(self, cmd, args, opts):
        if cmd == "gh" and args[:2] == ["pr", "list"] and "open" in args:
            self.calls.append((cmd, args, opts))
            return command_result(json.dumps(self.open_prs))
        return await super().execute(cmd, args, opts)


def test_claim_adopts_our_open_pr_instead_of_a_duplicate(tmp_path):
    fixture = OpenPrFixture(tmp_path)
    fixture.ctx["unit"]["branch"] = fixture.ctx["ledger"]["unit"]["branch"] = "proj-1"
    result = asyncio.run(fixture.phase(claim_phase))
    unit = fixture.ctx["unit"]
    assert result["ok"] and unit["adopted"] is True
    assert unit["branch"] == "proj-1-2" and unit["pr"]["number"] == 9 and unit["base"] == "release"
    assert unit["worktree"].endswith("/worktrees/proj-1-2")
    assert any(args[:2] == ["fetch", "origin"] for cmd, args, _ in fixture.calls if cmd == "git")


def test_claim_skips_someone_elses_open_pr(tmp_path):
    fixture = OpenPrFixture(tmp_path, author="alice")
    fixture.ctx["unit"]["branch"] = fixture.ctx["ledger"]["unit"]["branch"] = "proj-1"
    result = asyncio.run(fixture.phase(claim_phase))
    assert not result["ok"] and result["verdict"] == "skipped"
    assert result["reason"].startswith("open-pr-exists: #9 on proj-1-2 by @alice")


def test_claim_skips_an_open_pr_when_the_gh_user_is_unknown(tmp_path):
    fixture = OpenPrFixture(tmp_path)
    fixture.api["user"] = lambda args: command_result(code=1)
    fixture.ctx["unit"]["branch"] = fixture.ctx["ledger"]["unit"]["branch"] = "proj-1"
    result = asyncio.run(fixture.phase(claim_phase))
    assert not result["ok"] and result["verdict"] == "skipped"
    assert "current gh user unknown" in result["reason"]


def test_claim_fails_when_open_prs_cannot_be_listed(tmp_path):
    fixture = OpenPrFixture(tmp_path)
    original = fixture.execute

    async def execute(cmd, args, opts):
        if cmd == "gh" and args[:2] == ["pr", "list"] and "open" in args:
            return command_result(code=1)
        return await original(cmd, args, opts)

    fixture.execute = execute
    fixture.ctx["exec"] = execute
    fixture.ctx["unit"]["branch"] = fixture.ctx["ledger"]["unit"]["branch"] = "proj-1"
    result = asyncio.run(fixture.phase(claim_phase))
    assert not result["ok"] and result["reason"].startswith("claim: cannot list open PRs")


def test_an_adopted_pr_resumes_at_the_watch_loop(tmp_path):
    fixture = ModelFixture(tmp_path)
    fixture.pr = {"number": 9, "url": "https://github.invalid/a/r/pull/9", "state": "OPEN"}
    fixture.api["user"] = lambda args: command_result("me\n")
    original = fixture.execute

    async def execute(cmd, args, opts):
        if cmd == "gh" and args[:2] == ["pr", "list"] and "open" in args:
            return command_result(
                json.dumps(
                    [
                        {
                            "number": 9,
                            "url": "https://github.invalid/a/r/pull/9",
                            "headRefName": "PROJ-1",
                            "baseRefName": "main",
                            "isCrossRepository": False,
                            "author": {"login": "me"},
                        }
                    ]
                )
            )
        return await original(cmd, args, opts)

    fixture.execute = execute
    result = asyncio.run(run_units_step(fixture.input(exec=execute)))
    phases = [phase for phase, _, _ in fixture.child_calls]
    assert "plan" not in phases and "implement" not in phases and phases[0] == "watch"
    assert result["outputs"]["units"][0]["verdict"] == "merged"
    assert not any(args[:2] == ["pr", "create"] for cmd, args, _ in fixture.calls if cmd == "gh")


# --- F2: merged only when gh says so ------------------------------------------


def test_a_merged_claim_without_gh_confirmation_is_ignored(tmp_path):
    fixture = ModelFixture(tmp_path)
    fixture.step["caps"] = ["watch_minutes", "watch_poll_seconds"]
    fixture.state["caps"].update(watch_minutes=3, watch_poll_seconds=60)
    fixture.scripts["watch"] = lambda req, nth: answer(watch_output(merged=True, verdict="wait"))
    original = fixture.execute

    async def execute(cmd, args, opts):
        if cmd == "gh" and args[:2] == ["pr", "merge"]:
            return command_result(code=1)
        return await original(cmd, args, opts)

    result = asyncio.run(run_units_step(fixture.input(exec=execute)))
    row = result["outputs"]["units"][0]
    assert row["verdict"] != "merged"
    assert read_unit(fixture.run_dir, "PROJ-1")["verdict"] != "merged"


# --- F4: bot-only comments never stand the run down ---------------------------


def test_bot_only_comments_do_not_stop_the_watch(tmp_path):
    fixture = ModelFixture(tmp_path)
    fixture.api["repos/a/r/issues/5/comments"] = [
        {
            "user": {"login": "coderabbitai[bot]", "type": "Bot"},
            "created_at": "2099-01-01T00:00:00Z",
        },
        {"user": {"login": "sonarqubecloud", "type": "Bot"}, "created_at": "2099-01-01T00:00:00Z"},
    ]
    fixture.scripts["watch"] = lambda req, nth: answer(
        watch_output(human_comment=True) if nth == 1 else watch_output()
    )
    result = asyncio.run(run_units_step(fixture.input()))
    assert result["outputs"]["units"][0]["verdict"] == "merged"
    assert fixture.counts["watch"] >= 2


# --- F3: a numbered-sequence collision gets one fix pass before the push -----


@pytest.mark.parametrize("renumbered,verdict", [(True, "merged"), (False, "failed")])
def test_sequence_collision_fix_pass(tmp_path, renumbered, verdict):
    fixture = ModelFixture(tmp_path)
    original = fixture.execute
    state = {"fixed": False}

    async def execute(cmd, args, opts):
        if cmd == "git" and args[:3] == ["diff", "--name-only", "--diff-filter=A"]:
            name = "db/003_orders.sql" if state["fixed"] and renumbered else "db/002_orders.sql"
            return command_result(name + "\n")
        if cmd == "git" and args[:2] == ["ls-tree", "--name-only"]:
            return command_result("db/001_init.sql\ndb/002_users.sql\n")
        return await original(cmd, args, opts)

    def fix(req, nth):
        assert (
            "002_orders.sql" in Path(re.search(r"(/\S+findings\.md)", req["prompt"])[1]).read_text()
        )
        state["fixed"] = True
        return answer({"fixed": 1, "skipped": 0, "commits": 1})

    fixture.scripts["fix"] = fix
    result = asyncio.run(run_units_step(fixture.input(exec=execute)))
    row = result["outputs"]["units"][0]
    assert fixture.counts["fix"] == 1 and row["verdict"] == verdict
    if not renumbered:
        assert row["reason"].startswith("sequence-collision: db/002_orders.sql: number 002")
        assert not any(args[0] == "push" for cmd, args, _ in fixture.calls if cmd == "git")


# --- F5, F3, F7 against a real git repository ---------------------------------


def git(cwd, *args, env=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    git(work, "config", "user.email", "t@example.invalid")
    git(work, "config", "user.name", "T")
    git(work, "checkout", "-b", "main")
    (work / "db").mkdir()
    (work / "db" / "001_init.sql").write_text("-- init\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "init")
    git(work, "push", "origin", "main")
    return work


def real_ctx(work, branch="feat", base="main"):
    logs = []
    return {
        "unit": {
            "ref": "T-1",
            "branch": branch,
            "worktree": str(work),
            "base": base,
            "base_ref": f"origin/{base}",
        },
        "ledger": {"unit": {}, "cursors": {}},
        "cwd": str(work),
        "run_dir": str(work.parent / "run"),
        "env": clean_env(parent=dict(os.environ)),
        "exec": spawn_runner,
        "config": {"pipeline": "ticket", "remote": {"kind": "github"}},
        "log": logs.append,
        "logs": logs,
    }


def raw_commit(repo, message):
    """Commit the index as a raw object: a git wrapper or hook that edits
    commit messages (some machines strip this very trailer) cannot touch it."""
    tree = git(repo, "write-tree")
    parent = git(repo, "rev-parse", "HEAD")
    who = "T <t@example.invalid> 1700000000 +0000"
    body = f"tree {tree}\nparent {parent}\nauthor {who}\ncommitter {who}\n\n{message}"
    sha = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
        cwd=repo,
        input=body,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git(repo, "update-ref", "HEAD", sha)
    return sha


def test_strip_trailer_keeps_other_trailers():
    message = "feat: x\n\nbody\n\nCo-authored-by: Cursor Agent <cursoragent@cursor.com>\nSigned-off-by: A <a@b>\n"
    assert strip_trailer(message) == "feat: x\n\nbody\n\nSigned-off-by: A <a@b>\n"
    assert (
        strip_trailer("feat: y\n\nCo-authored-by: Ann <ann@x>\n")
        == "feat: y\n\nCo-authored-by: Ann <ann@x>\n"
    )


def test_cursor_trailers_are_stripped_from_new_commits(repo):
    git(repo, "checkout", "-b", "feat")
    before = git(repo, "rev-parse", "HEAD")
    for n in (1, 2):
        (repo / f"f{n}.txt").write_text(str(n))
        git(repo, "add", ".")
        raw_commit(repo, f"feat: {n}\n\nCo-authored-by: Cursor <cursoragent@cursor.com>\n")
    assert "cursoragent" in git(repo, "log", "--format=%B", f"{before}..HEAD")
    ctx = real_ctx(repo)
    assert asyncio.run(strip_cursor_trailers(ctx, before)) == 2
    log = git(repo, "log", "--format=%B", f"{before}..HEAD")
    assert "cursor" not in log.lower() and "feat: 1" in log and "feat: 2" in log
    assert git(repo, "status", "--porcelain") == ""
    assert asyncio.run(strip_cursor_trailers(ctx, before)) == 0


def test_sequence_collision_is_reported(repo, tmp_path):
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(other)], check=True, capture_output=True
    )
    git(other, "config", "user.email", "t@example.invalid")
    git(other, "config", "user.name", "T")
    (other / "db" / "002_users.sql").write_text("-- users\n")
    git(other, "add", ".")
    git(other, "commit", "-m", "sibling migration")
    git(other, "push", "origin", "main")
    git(repo, "checkout", "-b", "feat")
    (repo / "db" / "002_orders.sql").write_text("-- orders\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "orders migration")
    git(repo, "fetch", "origin")
    findings = asyncio.run(sequence_collisions(real_ctx(repo)))
    assert len(findings) == 1 and "db/002_orders.sql" in findings[0] and "002" in findings[0]


def test_sequence_number_ignores_dates_and_number_only_stems():
    assert sequence_number("0042_add_users.sql") == "0042"
    assert sequence_number("V7__init.sql") == "7"
    assert sequence_number("2024-05-01-new.md") is None
    assert sequence_number("404.svg") is None
    assert sequence_number("readme.md") is None


def test_rebase_onto_a_moved_base_before_the_first_push(repo, tmp_path):
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(other)], check=True, capture_output=True
    )
    git(other, "config", "user.email", "t@example.invalid")
    git(other, "config", "user.name", "T")
    (other / "base.txt").write_text("moved\n")
    git(other, "add", ".")
    git(other, "commit", "-m", "base moved")
    git(other, "push", "origin", "main")
    git(repo, "checkout", "-b", "feat")
    (repo / "child.txt").write_text("child\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "child work")
    result = asyncio.run(rebase_onto_base(real_ctx(repo)))
    assert result["ok"], result
    git(repo, "merge-base", "--is-ancestor", "origin/main", "HEAD")
    assert (repo / "base.txt").exists() and (repo / "child.txt").exists()
