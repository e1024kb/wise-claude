"""The `code-review` workflow's bash steps, run for real against a temp repo.

Each test pulls one step's `run` script out of the bundled YAML, substitutes
`{{run.dir}}`, passes inputs and outputs as `WISE_*` env vars (as the engine
does) and stubs `gh` with a script on PATH.
"""

import os
import subprocess
from pathlib import Path

import pytest

from wise_engine.yaml_compat import parse_yaml

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / "workflows/code-review/workflow.yaml"

GH_STUB = """#!/bin/bash
echo "$*" >> "$GH_LOG"
case "$1 $2" in
  "repo view")
    case "$*" in
      *nameWithOwner*) [ -n "${GH_REPO-}" ] || exit 1; echo "$GH_REPO" ;;
      *defaultBranchRef*) echo main ;;
    esac ;;
  "pr view")
    [ "$3" = 7 ] || { echo "no pull requests found" >&2; exit 1; }
    case "$*" in
      *isCrossRepository*) echo "${GH_CROSS-false}" ;;
      *baseRefName*) echo main ;;
      *headRefOid*) echo "${GH_HEAD-}" ;;
    esac ;;
  "pr review") cp "${@: -1}" "$GH_POSTED" ;;
  *) exit 1 ;;
esac
"""


def step_run(step_id: str) -> str:
    steps = parse_yaml(WORKFLOW.read_text())["steps"]
    return next(step["run"] for step in steps if step["id"] == step_id)


def git(cwd: Path, *args: str, env: dict[str, str]) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A clone of a bare `acme/widget.git` origin, on a branch one commit over main."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(GH_STUB)
    gh.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("WISE_", "GIT_", "GH_"))}
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
        GH_LOG=str(tmp_path / "gh.log"),
        GH_POSTED=str(tmp_path / "posted.md"),
        GH_REPO="acme/widget",
    )
    origin = tmp_path / "acme" / "widget.git"
    origin.parent.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], env=env, check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], env=env, check=True)
    (work / "a.txt").write_text("a\n")
    git(work, "add", "a.txt", env=env)
    git(work, "commit", "-qm", "base", env=env)
    git(work, "push", "-q", "origin", "HEAD:main", env=env)
    git(work, "fetch", "-q", "origin", env=env)
    git(work, "checkout", "-qb", "feature", env=env)
    (work / "a.txt").write_text("b\n")
    git(work, "commit", "-qam", "change", env=env)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return {"tmp": tmp_path, "work": work, "env": env, "run_dir": run_dir}


def run_step(repo, step_id: str, **wise: str) -> subprocess.CompletedProcess[str]:
    env = dict(repo["env"])
    for key, value in wise.items():
        if key.startswith("GH_"):
            env[key] = value
        else:
            env["WISE_" + key.upper()] = value
    script = step_run(step_id).replace("{{run.dir}}", str(repo["run_dir"]))
    return subprocess.run(
        ["bash", "-c", script], cwd=repo["work"], env=env, capture_output=True, text=True
    )


@pytest.mark.parametrize(
    "target, expected",
    [
        ("", "branch"),
        ("working", "working"),
        ("7", "pr:7"),
        ("https://github.com/acme/widget/pull/7", "pr:7"),
    ],
)
def test_resolve_target_normalises(repo, target, expected):
    result = run_step(repo, "resolve-target", target=target, mode="apply")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    "target, mode, cross, error",
    [
        ("https://github.com/other/widget/pull/7", "report", "false", "is not a PR of acme/widget"),
        ("branch", "comment", "false", "mode=comment needs a PR target"),
        ("7", "apply", "true", "mode=apply is refused for fork PRs"),
        ("8", "report", "false", "PR #8 not found"),
    ],
)
def test_resolve_target_rejects(repo, target, mode, cross, error):
    result = run_step(repo, "resolve-target", target=target, mode=mode, GH_CROSS=cross)
    assert result.returncode == 1
    assert error in result.stderr


@pytest.mark.parametrize("mode", ["report", "comment"])
def test_resolve_target_allows_fork_pr_without_apply(repo, mode):
    result = run_step(repo, "resolve-target", target="7", mode=mode, GH_CROSS="true")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "pr:7"


def test_resolve_remote_picks_the_remote_gh_resolves(repo):
    assert run_step(repo, "resolve-remote").stdout.strip() == "origin"
    # A fork checkout: `origin` is the fork, `upstream` the repository gh resolves.
    env = repo["env"]
    upstream = git(repo["work"], "remote", "get-url", "origin", env=env)
    git(repo["work"], "remote", "set-url", "origin", "git@github.com:me/widget.git", env=env)
    git(repo["work"], "remote", "add", "upstream", upstream, env=env)
    assert run_step(repo, "resolve-remote").stdout.strip() == "upstream"
    git(
        repo["work"], "remote", "set-url", "upstream", "https://github.com/Acme/Widget.git", env=env
    )
    assert run_step(repo, "resolve-remote").stdout.strip() == "upstream"
    git(repo["work"], "remote", "remove", "upstream", env=env)
    failed = run_step(repo, "resolve-remote")
    assert failed.returncode == 1 and "no git remote points at acme/widget" in failed.stderr
    assert run_step(repo, "resolve-remote", GH_REPO="").stdout.strip() == "origin"


def test_resolve_base_accepts_a_branch_and_rejects_a_bad_ref(repo):
    ok = run_step(repo, "resolve-base", base="origin/main", remote="origin", review_target="branch")
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.strip() == "main"
    bad = run_step(repo, "resolve-base", base="main..x", remote="origin", review_target="branch")
    assert bad.returncode == 1
    assert "not a valid branch name" in bad.stderr


@pytest.mark.parametrize("target, expected", [("branch", "1"), ("working", "2")])
def test_count_changes_counts_untracked_files_for_working(repo, target, expected):
    (repo["work"] / "new.txt").write_text("new\n")
    result = run_step(
        repo,
        "count-changes",
        review_dir=str(repo["work"]),
        review_target=target,
        remote="origin",
        base="main",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_cleanup_tree_removes_only_the_pr_worktree(repo):
    review = repo["run_dir"] / "review"
    review.mkdir()
    (review / "findings.md").write_text("1. a.txt:1 - x - y - info\n")
    tree = review / "tree"
    git(repo["work"], "worktree", "add", "--detach", "-q", str(tree), "HEAD", env=repo["env"])

    kept = run_step(repo, "cleanup-tree", review_dir=str(repo["work"]))
    assert kept.returncode == 0, kept.stderr
    assert tree.is_dir() and (repo["work"] / "a.txt").is_file()

    removed = run_step(repo, "cleanup-tree", review_dir=str(tree))
    assert removed.returncode == 0, removed.stderr
    assert not tree.exists()
    assert (review / "findings.md").is_file() and (repo["work"] / "a.txt").is_file()


def comment(repo, findings: str, missing: str) -> subprocess.CompletedProcess[str]:
    path = repo["tmp"] / "findings.md"
    path.write_text(findings)
    return run_step(
        repo,
        "comment",
        review_target="pr:7",
        review_dir=str(repo["work"]),
        findings_path=str(path),
        missing_reviews=missing,
    )


def test_comment_refuses_a_body_with_a_token(repo):
    token = "ghp_" + "A1b2C3" * 6
    result = comment(repo, f"1. a.txt:1 - leaked {token} - remove it - critical\n", "none")
    assert result.returncode == 1
    assert "looks like it contains a secret" in result.stderr
    assert token not in result.stderr
    assert not Path(repo["env"]["GH_POSTED"]).exists()


def test_comment_strips_the_review_dir_and_names_reporting_lenses(repo):
    result = comment(repo, f"1. {repo['work']}/a.txt:1 - bug - fix - warning\n", "none")
    assert result.returncode == 0, result.stderr
    posted = Path(repo["env"]["GH_POSTED"]).read_text()
    assert "(correctness, security, tests)" in posted
    assert "1. a.txt:1 - bug" in posted and str(repo["work"]) not in posted


def test_comment_never_says_no_findings_with_a_missing_lens(repo):
    result = comment(repo, "", "security")
    assert result.returncode == 0, result.stderr
    posted = Path(repo["env"]["GH_POSTED"]).read_text()
    assert "no findings" not in posted
    assert "(correctness, tests)" in posted
    assert "the security lens did not report" in posted


def test_finalize_prints_blank_for_unset_values(repo):
    result = run_step(repo, "finalize", review_target="branch", base="main", change_count="0")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "code-review: target=branch base=main changes=0 findings= kept= refuted= "
        "applied= skipped= committed= commented= file="
    )
