import subprocess

from wise_engine.branches import branch_choices, is_base_branch


def git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@x",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@x",
        },
    )


def repo(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "f").write_text("1")
    git(origin, "add", "f")
    git(origin, "commit", "-q", "-m", "one")
    git(origin, "branch", "release-26-8-0")
    git(origin, "branch", "release-26-9-0")
    git(origin, "branch", "feature/x")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    return clone


def test_is_base_branch():
    assert is_base_branch("main") and is_base_branch("master")
    assert is_base_branch("release-26-9-0") and is_base_branch("release/26.9")
    assert not is_base_branch("feature/x") and not is_base_branch("PROJ-1")


def test_branch_choices_prefers_the_checked_out_base_branch(tmp_path):
    clone = repo(tmp_path)
    git(clone, "checkout", "-q", "release-26-9-0")
    result = branch_choices(str(clone))
    assert result["current"] == "release-26-9-0" and result["default"] == "release-26-9-0"
    values = [option["value"] for option in result["options"]]
    assert values[:2] == ["release-26-9-0", "main"]
    assert set(values) == {"release-26-9-0", "main", "release-26-8-0"}


def test_branch_choices_from_a_feature_branch_defaults_to_main(tmp_path):
    clone = repo(tmp_path)
    git(clone, "checkout", "-q", "-b", "PROJ-1")
    result = branch_choices(str(clone))
    assert result["default"] == "main"
    values = [option["value"] for option in result["options"]]
    assert values[0] == "main" and values[-1] == "PROJ-1"
    assert "feature/x" not in values


def test_branch_choices_outside_a_repo(tmp_path):
    assert branch_choices(str(tmp_path)) is None
