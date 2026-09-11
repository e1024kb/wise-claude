import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from wise_engine.ledger import empty_usage
from wise_engine.phases.claim import claim_phase
from wise_engine.phases.cleanup import cleanup_phase
from wise_engine.phases.common import (
    err_text,
    is_protected_branch,
    make_unit,
    parse_items,
    plan_branch,
    spawn_runner,
    ticket_branch,
    ticket_context,
    ticket_ref,
)
from wise_engine.phases.pr import (
    _collect_facts,
    default_pr_body,
    fill_pr_template,
    find_pr_template,
    pr_phase,
)
from wise_engine.phases.push import push_phase
from wise_engine.phases.request_review import request_review_phase
from wise_engine.phases.worktree import parse_worktrees, worktree_phase


def command_result(stdout="", code=0, stderr=""):
    return dict(code=code, stdout=stdout, stderr=stderr, timed_out=False)


class PhaseFixture:
    def __init__(self, root):
        self.root = root
        self.repo = root / "repo"
        self.repo.mkdir()
        self.run_dir = root / "run"
        self.run_dir.mkdir()
        self.calls = []
        self.logs = []
        self.branches = set()
        self.remote = set()
        self.trees = {}
        self.commits = 0
        self.head = "head-0"
        self.pr = None
        self.reviewers = []
        self.failures = {}
        unit = make_unit("ticket", "PROJ-1", str(self.repo), str(self.run_dir))
        ledger = {
            "unit": unit,
            "cursors": {},
            "cleaned": False,
            "last_phase": "claim",
            "usage": empty_usage(),
            "caps": {},
        }
        self.ctx = {
            "unit": unit,
            "ledger": ledger,
            "cwd": str(self.repo),
            "run_dir": str(self.run_dir),
            "env": {},
            "exec": self.execute,
            "config": {
                "pipeline": "ticket",
                "reviewers": ["copilot-pull-request-reviewer"],
                "tickets": [
                    {
                        "ref": "PROJ-1",
                        "title": "First",
                        "url": "https://tracker.invalid/PROJ-1",
                        "body": "Do the thing.",
                    }
                ],
                "caps": {},
                "groups": {},
                "profile": "medium",
                "resume": "fresh",
            },
            "log": self.logs.append,
            "checkpoint": self.checkpoint,
            "sleep": self.sleep,
            "now": lambda: 1000,
            "resolved": {},
        }

    def checkpoint(self, patch):
        for key, value in patch.items():
            if key in ("unit", "cursors"):
                self.ctx["ledger"][key].update(value)
            else:
                self.ctx["ledger"][key] = value
        self.ctx["unit"] = self.ctx["ledger"]["unit"]

    async def sleep(self, ms, signal=None):
        await asyncio.sleep(0)

    async def execute(self, cmd, args, opts):
        self.calls.append((cmd, args, opts))
        for prefix, result in self.failures.items():
            if (cmd, *args[: len(prefix) - 1]) == prefix:
                return result
        if cmd == "git":
            if args[0] == "ls-remote":
                return command_result("sha refs/heads/x\n" if args[-1] in self.remote else "")
            if args[0] == "show-ref":
                return command_result(
                    code=0 if args[-1].removeprefix("refs/heads/") in self.branches else 1
                )
            if args[0] == "symbolic-ref":
                return command_result("origin/main\n")
            if args[:2] == ["worktree", "list"]:
                return command_result(
                    "\n\n".join(
                        f"worktree {path}\nHEAD sha\nbranch refs/heads/{branch}\n"
                        for path, branch in self.trees.items()
                    )
                )
            if args[:2] == ["worktree", "add"]:
                path, branch = (args[3], args[5]) if "--no-track" in args else (args[2], args[3])
                self.trees[path] = branch
                self.branches.add(branch)
                Path(path).mkdir(parents=True, exist_ok=True)
            if args[:2] == ["worktree", "remove"]:
                self.trees.pop(args[-1], None)
                import shutil

                shutil.rmtree(args[-1], ignore_errors=True)
            if args[0] == "rev-list":
                return command_result(str(self.commits))
            if args[0] == "rev-parse":
                return command_result(self.head)
            if args[0] == "log":
                return command_result("feat: first change\n")
            if args[0] == "push":
                self.remote.add(args[-1])
            return command_result()
        if cmd == "gh":
            if args[:2] == ["repo", "view"]:
                return command_result(json.dumps({"defaultBranchRef": {"name": "main"}}))
            if args[:2] == ["pr", "list"]:
                return command_result(
                    json.dumps([self.pr] if self.pr and self.pr["state"] == "MERGED" else [])
                )
            if args[:2] == ["pr", "view"]:
                if args[-1] == "reviewRequests":
                    return command_result(
                        json.dumps(
                            {"reviewRequests": [{"login": value} for value in self.reviewers]}
                        )
                    )
                return command_result(json.dumps(self.pr)) if self.pr else command_result(code=1)
            if args[:2] == ["pr", "create"]:
                self.pr = {"number": 5, "url": "https://github.invalid/a/r/pull/5", "state": "OPEN"}
                return command_result(self.pr["url"] + "\n")
            if args[:2] == ["pr", "merge"]:
                if self.pr:
                    self.pr["state"] = "MERGED"
            if "--add-reviewer" in args:
                self.reviewers.append(args[-1])
            return command_result()
        raise AssertionError((cmd, args))

    async def phase(self, runner):
        result = await runner(self.ctx)
        if result.get("patch"):
            self.checkpoint(result["patch"])
        return result


@pytest.mark.parametrize(
    "value,ref,branch",
    [
        ("#42", "42", "abstract-task-42"),
        ("https://tracker.invalid/PROJ-9/description", "PROJ-9", "PROJ-9"),
        ("weird ref", "weird ref", "weird-ref"),
        ("###", "##", "abstract-task-0"),
    ],
)
def test_ticket_naming(value, ref, branch):
    assert ticket_ref(value) == ref
    assert ticket_branch(ref) == branch


def test_ticket_unit_preserves_native_ref_without_changing_branch(tmp_path):
    unit = make_unit("ticket", "owner/repo#42", str(tmp_path), str(tmp_path / "run"), "main")
    assert unit["ref"] == "42"
    assert unit["ticket_ref"] == "owner/repo#42"
    assert unit["branch"] == "abstract-task-42"


def test_ticket_context_matches_exact_url_without_ambiguous_numeric_fallback(tmp_path):
    url = "https://github.invalid/owner/repo/issues/42"
    url_unit = make_unit("ticket", url, str(tmp_path), str(tmp_path / "run"), "main")
    repository_unit = make_unit(
        "ticket", "owner/repo#42", str(tmp_path), str(tmp_path / "run"), "main"
    )
    tickets = [
        {"ref": "42", "url": url, "title": "Right repository"},
        {"ref": "owner/repo#42", "title": "Namespaced repository"},
        {"ref": "other/repo#42", "title": "Other repository"},
    ]
    assert ticket_context(tickets, url_unit)["title"] == "Right repository"
    assert ticket_context(tickets, repository_unit)["title"] == "Namespaced repository"
    assert ticket_context(tickets[:1], repository_unit) is None


def test_pr_facts_preserve_repository_qualified_ticket_ref(tmp_path):
    async def scenario():
        fixture = PhaseFixture(tmp_path)
        fixture.ctx["unit"] = make_unit(
            "ticket", "owner/repo#42", str(fixture.repo), str(fixture.run_dir), "main"
        )
        fixture.ctx["config"]["tickets"] = [
            {
                "ref": "owner/repo#42",
                "title": "Repository issue",
                "url": "https://github.invalid/owner/repo/issues/42",
            }
        ]
        facts = await _collect_facts(fixture.ctx)
        assert facts["ref"] == "owner/repo#42"
        assert facts["title"] == "owner/repo#42: Repository issue"
        assert facts["ticket_link"].startswith("[owner/repo#42]")

        url = "https://github.invalid/owner/repo/issues/42"
        fixture.ctx["unit"] = make_unit(
            "ticket", url, str(fixture.repo), str(fixture.run_dir), "main"
        )
        fixture.ctx["config"]["tickets"] = [{"ref": "42", "url": url}]
        facts = await _collect_facts(fixture.ctx)
        assert facts["ref"] == "42"

    asyncio.run(scenario())


def test_plan_protected_items_and_porcelain():
    assert plan_branch("/plans/PLAN-feature.md") == "feature"
    assert plan_branch("/plans/123.MD") == "plan-123"
    assert plan_branch("PLAN-.md") == "plan-0"
    assert all(is_protected_branch(branch) for branch in ("main", "master", "release-v1"))
    assert not is_protected_branch("feature")
    assert parse_items("a, b;a\nc") == ["a", "b", "c"]
    assert parse_items('["a",{"ref":"b"},false,"a"]') == ["a", "b"]
    assert parse_worktrees("worktree /a\nbranch refs/heads/main\n\nworktree /b\ndetached\n") == [
        {"path": "/a", "branch": "main"},
        {"path": "/b"},
    ]


def test_pr_body_template_and_ladder(tmp_path):
    facts = {
        "ref": "PROJ-1",
        "title": "PROJ-1: change",
        "ticket_link": "[PROJ-1](https://tracker.invalid/1)",
        "commits": ["feat: change"],
        "plan_path": "/plan.md",
    }
    body = default_pr_body(facts)
    assert "- feat: change" in body and "- plan: `/plan.md`" in body
    template = (
        "## Summary\nplaceholder\n## Changes\nold\n## Testing\ncustom testing\n## Context\nold\n"
    )
    filled = fill_pr_template(template, facts)
    assert "placeholder" not in filled and "custom testing" in filled and "- ticket:" in filled
    assert fill_pr_template("Custom text", facts).endswith("\nCustom text")
    assert find_pr_template(str(tmp_path)) is None
    directory = tmp_path / ".github/PULL_REQUEST_TEMPLATE"
    directory.mkdir(parents=True)
    (directory / "z.md").write_text("Z")
    (directory / "a.md").write_text("A")
    assert find_pr_template(str(tmp_path)).endswith("a.md")
    (directory / "default.md").write_text("default")
    assert find_pr_template(str(tmp_path)).endswith("default.md")


def test_claim_ownership_and_conflicts(tmp_path):
    async def scenario():
        fixture = PhaseFixture(tmp_path)
        fixture.failures[("gh", "repo")] = command_result(code=1)
        result = await fixture.phase(claim_phase)
        assert result["ok"] and fixture.ctx["unit"]["base"] == "main"
        assert fixture.ctx["ledger"]["cursors"]["claim"] == "owned"
        fixture.calls.clear()
        assert (await fixture.phase(claim_phase))["ok"] and fixture.calls == []
        fixture.ctx["ledger"]["cursors"].clear()
        fixture.remote.add("PROJ-1")
        assert (await fixture.phase(claim_phase))["verdict"] == "skipped"
        fixture.remote.clear()
        fixture.branches.add("PROJ-1")
        assert "local branch" in (await fixture.phase(claim_phase))["reason"]
        fixture.branches.clear()
        fixture.trees["/elsewhere"] = "PROJ-1"
        assert "a worktree" in (await fixture.phase(claim_phase))["reason"]
        fixture.trees.clear()
        fixture.pr = {"number": 5, "url": "https://github.invalid/pr/5", "state": "MERGED"}
        assert (await fixture.phase(claim_phase))["verdict"] == "merged"
        fixture.ctx["config"]["pipeline"] = "plan"
        assert "missing: plan file" in (await fixture.phase(claim_phase))["reason"]

    asyncio.run(scenario())


def test_worktree_create_reuse_conflicts_push_pr_review_cleanup(tmp_path):
    async def scenario():
        fixture = PhaseFixture(tmp_path)
        await fixture.phase(claim_phase)
        assert (await fixture.phase(worktree_phase))["ok"]
        assert fixture.ctx["ledger"]["cursors"]["worktree"] == "includes-done"
        fixture.calls.clear()
        assert (await fixture.phase(worktree_phase))["ok"]
        assert not any(args[:2] == ["worktree", "add"] for _, args, _ in fixture.calls)
        path = fixture.ctx["unit"]["worktree"]
        fixture.trees[path] = "other"
        assert "worktree-conflict" in (await fixture.phase(worktree_phase))["reason"]
        fixture.trees[path] = "PROJ-1"
        assert (await fixture.phase(push_phase))["ok"] and "PROJ-1" in fixture.remote
        assert (await fixture.phase(pr_phase))["ok"]
        assert fixture.ctx["unit"]["pr"]["number"] == 5
        body = (fixture.run_dir / "units/PROJ-1.pr-body.md").read_text()
        assert "https://tracker.invalid/PROJ-1" in body and "feat: first change" in body
        assert (await fixture.phase(pr_phase))["ok"]
        assert any(args[:2] == ["pr", "edit"] for _, args, _ in fixture.calls)
        await fixture.phase(request_review_phase)
        fixture.calls.clear()
        await fixture.phase(request_review_phase)
        assert not any("--add-reviewer" in args for _, args, _ in fixture.calls)
        fixture.ctx["ledger"]["verdict"] = "all-green"
        assert (await fixture.phase(cleanup_phase))["patch"]["cleaned"] is False
        fixture.ctx["ledger"]["verdict"] = "merged"
        assert (await fixture.phase(cleanup_phase))["patch"]["cleaned"] is True
        assert not Path(path).exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("state,verdict", [("MERGED", "merged"), ("CLOSED", "human-intervention")])
def test_pr_terminal_states(tmp_path, state, verdict):
    async def scenario():
        fixture = PhaseFixture(tmp_path)
        fixture.pr = {"number": 5, "url": "https://github.invalid/pr/5", "state": state}
        assert (await pr_phase(fixture.ctx))["verdict"] == verdict
        assert not any(
            args[:2] in (["pr", "create"], ["pr", "edit"]) for _, args, _ in fixture.calls
        )

    asyncio.run(scenario())


def test_protected_push_pr_and_best_effort_reviewer(tmp_path):
    async def scenario():
        fixture = PhaseFixture(tmp_path)
        fixture.ctx["unit"]["branch"] = "main"
        assert not (await push_phase(fixture.ctx))["ok"]
        assert not (await pr_phase(fixture.ctx))["ok"] and fixture.calls == []
        fixture.ctx["unit"]["pr"] = {"number": 5, "url": "https://github.invalid/pr/5"}
        fixture.failures[("gh", "pr", "edit")] = command_result(code=1, stderr="unknown reviewer")
        assert (await request_review_phase(fixture.ctx))["ok"]
        assert "unavailable" in fixture.logs[-1]

    asyncio.run(scenario())


def test_spawn_runner_real_disposable_git_and_cancellation(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    async def scenario():
        import os

        result = await spawn_runner(
            "git",
            ["rev-parse", "--is-inside-work-tree"],
            {"cwd": str(tmp_path), "env": dict(os.environ)},
        )
        assert result["code"] == 0 and result["stdout"].strip() == "true"
        signal = asyncio.Event()
        signal.set()
        result = await spawn_runner(
            "/bin/sleep", ["30"], {"cwd": str(tmp_path), "env": {}, "signal": signal}
        )
        assert result["code"] != 0
        result = await spawn_runner("/missing-binary", [], {"cwd": str(tmp_path), "env": {}})
        assert "spawn failed" in err_text(result)

    asyncio.run(scenario())


def test_real_local_worktree_include_push_and_cleanup(tmp_path):
    import os

    fixture = PhaseFixture(tmp_path)
    remote = tmp_path / "origin.git"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "empty-gitconfig"),
    }

    def local_git(*args, cwd=fixture.repo):
        return subprocess.run(
            ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
        ).stdout

    local_git("init", "--bare", "--initial-branch=main", str(remote))
    local_git("init", "--initial-branch=main")
    (fixture.repo / "tracked.txt").write_text("initial\n")
    (fixture.repo / ".gitignore").write_text(".env\n")
    (fixture.repo / ".worktreeinclude").write_text(".env\n")
    (fixture.repo / ".env").write_text("TEST_ONLY=1\n")
    local_git("add", "tracked.txt", ".gitignore", ".worktreeinclude")
    local_git("commit", "-m", "initial")
    local_git("remote", "add", "origin", str(remote))
    local_git("push", "-u", "origin", "main")

    async def scenario():
        async def execute(cmd, args, opts):
            if cmd == "git":
                return await spawn_runner(cmd, args, {**opts, "env": env})
            return await fixture.execute(cmd, args, opts)

        fixture.ctx["exec"] = execute
        await fixture.phase(claim_phase)
        assert (await fixture.phase(worktree_phase))["ok"]
        worktree = Path(fixture.ctx["unit"]["worktree"])
        assert (worktree / ".env").read_text() == "TEST_ONLY=1\n"
        (worktree / ".env").write_text("LOCAL_EDIT=1\n")
        assert (await fixture.phase(worktree_phase))["ok"]
        assert (worktree / ".env").read_text() == "LOCAL_EDIT=1\n"
        (worktree / "feature.txt").write_text("feature\n")
        local_git("add", "feature.txt", cwd=worktree)
        local_git("commit", "-m", "feature", cwd=worktree)
        assert (await fixture.phase(push_phase))["ok"]
        assert "refs/heads/PROJ-1" in local_git("ls-remote", "--heads", "origin", "PROJ-1")
        fixture.ctx["unit"]["pr"] = {"number": 5, "url": "https://github.invalid/pr/5"}
        fixture.ctx["ledger"]["verdict"] = "merged"
        assert (await fixture.phase(cleanup_phase))["patch"]["cleaned"] is True
        assert not worktree.exists()
        assert "refs/heads/PROJ-1" not in local_git("show-ref", "--heads")

    asyncio.run(scenario())
