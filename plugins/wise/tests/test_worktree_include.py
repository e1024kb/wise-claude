"""Pins `cmd_apply_worktree_include` (plugins/wise/scripts/workflows.py) —
the PR #20 `.worktreeinclude` copier that carries untracked / gitignored
artifacts into a fresh `git worktree add` checkout, plus its path-escape
guard.
"""

from __future__ import annotations

import subprocess


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")


def test_untracked_matching_files_are_copied(workflows_module, tmp_path):
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    _init_repo(repo)
    (repo / ".worktreeinclude").write_text(".env\n")
    (repo / ".env").write_text("SECRET=1\n")
    worktree.mkdir()

    rc = workflows_module.cmd_apply_worktree_include(str(repo), str(worktree))
    assert rc == 0
    assert (worktree / ".env").read_text() == "SECRET=1\n"


def test_tracked_files_are_never_copied(workflows_module, tmp_path):
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    _init_repo(repo)
    (repo / ".worktreeinclude").write_text("*.txt\n")
    (repo / "tracked.txt").write_text("tracked content\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "add tracked")
    worktree.mkdir()

    rc = workflows_module.cmd_apply_worktree_include(str(repo), str(worktree))
    assert rc == 0
    assert not (worktree / "tracked.txt").exists()


def test_existing_dest_file_is_overwritten(workflows_module, tmp_path):
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    _init_repo(repo)
    (repo / ".worktreeinclude").write_text("config.local\n")
    (repo / "config.local").write_text("new-value\n")
    worktree.mkdir()
    (worktree / "config.local").write_text("stale-value\n")

    rc = workflows_module.cmd_apply_worktree_include(str(repo), str(worktree))
    assert rc == 0
    assert (worktree / "config.local").read_text() == "new-value\n"


def test_escape_entry_is_refused_and_counted_skipped(workflows_module, tmp_path, capsys):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    worktree = tmp_path / "worktree"
    _init_repo(repo)
    outside.mkdir()
    (outside / "secret.txt").write_text("outside content\n")
    # An untracked symlink whose target escapes repo_root — `git ls-files`
    # lists the symlink's own (in-tree) path, but `.resolve()` follows it to
    # `outside/`, tripping the `resolve().relative_to()` escape guard.
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    (repo / ".worktreeinclude").write_text("linked\n")
    worktree.mkdir()

    rc = workflows_module.cmd_apply_worktree_include(str(repo), str(worktree))
    assert rc == 0
    err = capsys.readouterr().err
    assert "skip out-of-tree path" in err
    assert "copied 0 (skipped 1)" in err
    assert not (worktree / "linked").exists()


def test_missing_worktreeinclude_prints_notice_and_returns_0(workflows_module, tmp_path, capsys):
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    _init_repo(repo)
    worktree.mkdir()

    rc = workflows_module.cmd_apply_worktree_include(str(repo), str(worktree))
    assert rc == 0
    err = capsys.readouterr().err
    assert "no .worktreeinclude" in err


def test_git_failure_is_graceful_and_returns_0(workflows_module, tmp_path):
    # repo_root is not a git repo at all -> `git ls-files` fails; must
    # still return 0 (best-effort, never abort a worktree creation).
    repo = tmp_path / "not-a-repo"
    repo.mkdir()
    (repo / ".worktreeinclude").write_text("*.env\n")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    rc = workflows_module.cmd_apply_worktree_include(str(repo), str(worktree))
    assert rc == 0


def test_ignored_directory_is_copied_via_copytree(workflows_module, tmp_path):
    # `git ls-files --directory` collapses a fully-ignored dir to a single
    # `dir/`-suffixed entry, which the function copies via `shutil.copytree`
    # instead of `shutil.copy2` — exercise that branch specifically.
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    _init_repo(repo)
    (repo / ".worktreeinclude").write_text("cache/\n")
    cache_dir = repo / "cache"
    cache_dir.mkdir()
    (cache_dir / "nested.bin").write_text("data\n")
    worktree.mkdir()

    rc = workflows_module.cmd_apply_worktree_include(str(repo), str(worktree))
    assert rc == 0
    assert (worktree / "cache" / "nested.bin").read_text() == "data\n"


def test_vanished_listed_path_is_skipped(workflows_module, tmp_path, monkeypatch):
    # A path `git ls-files` reported can still vanish before the copy loop
    # reaches it (race, or any other on-disk change) — the function must
    # skip it gracefully rather than raise. Inject a phantom entry into the
    # real `git ls-files` output to force that path deterministically.
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    _init_repo(repo)
    (repo / ".worktreeinclude").write_text("ghost.txt\n")
    worktree.mkdir()

    real_run = workflows_module.subprocess.run

    def fake_run(cmd, *args, **kwargs):
        result = real_run(cmd, *args, **kwargs)
        if isinstance(cmd, list) and "ls-files" in cmd:
            result.stdout = (result.stdout or "") + "ghost.txt\0"
        return result

    monkeypatch.setattr(workflows_module.subprocess, "run", fake_run)

    rc = workflows_module.cmd_apply_worktree_include(str(repo), str(worktree))
    assert rc == 0
    assert not (worktree / "ghost.txt").exists()
