from __future__ import annotations

from pathlib import Path

import pytest

from wise_engine.permissions import (
    MUTATING_VERBS,
    READ_ONLY_BUILTINS,
    READ_VERBS,
    decide_permission,
    effective_mode,
    is_auto_bash_command,
    is_read_mcp_tool,
    mcp_tool_part,
    name_tokens,
    provider_permission,
)


@pytest.mark.parametrize(
    "name,tokens",
    [
        ("getJiraIssue", ["get", "jira", "issue"]),
        ("slack_read-channel", ["slack", "read", "channel"]),
        ("searchJiraIssuesUsingJql", ["search", "jira", "issues", "using", "jql"]),
        ("query-docs", ["query", "docs"]),
    ],
)
def test_name_tokens(name: str, tokens: list[str]) -> None:
    assert name_tokens(name) == tokens


@pytest.mark.parametrize(
    "name,part",
    [
        ("mcp__jira__getJiraIssue", "getJiraIssue"),
        ("mcp__plugin_linear_linear__get_issue", "get_issue"),
        ("mcp__wise-engine", ""),
        ("WebFetch", None),
    ],
)
def test_mcp_tool_part(name: str, part: str | None) -> None:
    assert mcp_tool_part(name) == part


@pytest.mark.parametrize(
    "name",
    [
        "mcp__jira__getJiraIssue",
        "mcp__jira__searchJiraIssuesUsingJql",
        "mcp__plugin_linear_linear__list_issues",
        "mcp__691668ad__slack_read_channel",
        "mcp__691668ad__slack_search_public",
        "mcp__plugin_context7_context7__query-docs",
        "mcp__9614c717__list_events",
        "mcp__betterstack__status_page",
    ],
)
def test_read_mcp_names(name: str) -> None:
    assert is_read_mcp_tool(name)
    value = {"key": "issue"}
    assert decide_permission(name, value) == {"behavior": "allow", "updatedInput": value}


@pytest.mark.parametrize(
    "name",
    [
        "mcp__691668ad__slack_send_message",
        "mcp__jira__createJiraIssue",
        "mcp__jira__transitionJiraIssue",
        "mcp__plugin_linear_linear__update_issue",
        "mcp__betterstack__update_status_page",
        "mcp__betterstack__resolve_incident",
        "mcp__f63586e2__trash_message",
        "mcp__x__slack_send_message_draft",
        "mcp__x__unknownVerb",
        "mcp__wise-engine",
    ],
)
def test_mutating_and_unknown_mcp_names(name: str) -> None:
    assert not is_read_mcp_tool(name)
    assert decide_permission(name, {}, mode="auto")["behavior"] == "deny"


def test_permission_builtins_keep_input_identity() -> None:
    value = {"url": "https://example.com"}
    for name in READ_ONLY_BUILTINS:
        result = decide_permission(name, value)
        assert result["behavior"] == "allow"
        assert result["updatedInput"] is value
    for name in ("Bash", "Edit", "Skill", "TodoWrite"):
        result = decide_permission(name, {})
        assert result["behavior"] == "deny"
        assert result["message"].startswith(f"{name} ")
        assert "allowed_tools" in result["message"]
        assert "Bypass permissions" in result["message"]
    for name in ("Skill", "TodoWrite", "Bash", "mcp__server__send_message"):
        assert decide_permission(name, value, mode="full-access") == {
            "behavior": "allow",
            "updatedInput": value,
        }


def test_read_verb_position_and_mutation_precedence() -> None:
    for verb in MUTATING_VERBS:
        assert not is_read_mcp_tool(f"mcp__vendor__{verb}_get_item")
        assert not is_read_mcp_tool(f"mcp__vendor__vendor_{verb}_item")
    for verb in READ_VERBS:
        assert is_read_mcp_tool(f"mcp__vendor__{verb}_item")
        assert is_read_mcp_tool(f"mcp__vendor__vendor_{verb}_item")
    assert not is_read_mcp_tool("mcp__vendor__prefix_vendor_get_item")


@pytest.mark.parametrize(
    "command",
    [
        "git status --short",
        "git branch --show-current",
        "rg needle src",
        "git diff --output-indicator-new=+",
        "git diff",
        "git show HEAD",
        "git log --oneline",
        "git rev-parse HEAD",
        "git merge-base HEAD main",
        "git ls-files",
        "git ls-tree HEAD",
        "git cat-file -p HEAD",
        "git branch -a -vv",
        "pwd",
        "ls .",
        "grep needle src/a.py",
        "jq . package.json",
        "cat README.md",
        "head README.md",
        "tail README.md",
        "wc README.md",
        "test -f README.md",
    ],
)
def test_auto_inspection_commands(command: str) -> None:
    assert is_auto_bash_command(command)
    assert decide_permission("Bash", {"command": command}, mode="auto")["behavior"] == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "git branch feature",
        "git diff --output=review.diff",
        "git diff --output review.diff",
        "git diff --output=/tmp/wise-output",
        "git diff --output /tmp/wise-output",
        "rg --pre sh needle .",
        "rg --pre-glob=*.py needle .",
        "cat /etc/passwd",
        'cat "/etc/passwd"',
        "cat foo/../../etc/passwd",
        "cat foo/../bar",
        "cat ~/secret",
        "ls ..",
        "ls ../x",
        "ls; pwd",
        "ls && pwd",
        "ls | cat",
        "cat `pwd`",
        "cat $(pwd)",
        "cat <input",
        "ls >output",
        "ls\npwd",
        "ls\rpwd",
        "npm test",
        "npm run build",
        "pnpm run build",
        "yarn lint",
        "bun test",
        "just check",
        "make test",
        "cargo test",
        "go test ./...",
        "pytest",
        "ruff check .",
        "eslint .",
        "tsc --noEmit",
        "rm -rf ./dist",
        "echo ready\nrm -rf ./dist",
        "sh -c 'rm -rf ./dist'",
        'bash -c "rm -rf ./dist"',
        "",
        "  ",
    ],
)
def test_auto_denies_shell_controls_external_paths_and_task_runners(command: str) -> None:
    assert not is_auto_bash_command(command)
    result = decide_permission("Bash", {"command": command}, mode="auto")
    assert result == {
        "behavior": "deny",
        "message": "Bash command blocked by wise auto mode; select Bypass permissions to run it",
    }


def test_auto_local_mutations_and_workspace_bounds(tmp_path: Path) -> None:
    workspace, shared = tmp_path / "project", tmp_path / "shared"
    workspace.mkdir()
    shared.mkdir()
    roots = [workspace, shared]
    for tool, value in [
        ("Edit", {"file_path": "src/a.ts"}),
        ("Write", {"file_path": str(workspace / "src/a.ts")}),
        ("MultiEdit", {"file_path": str(shared / "a.ts")}),
        ("NotebookEdit", {"notebook_path": "notebooks/a.ipynb"}),
    ]:
        assert (
            decide_permission(tool, value, mode="auto", workspace_roots=roots)["behavior"]
            == "allow"
        )
    for tool, value in [
        ("Edit", {"file_path": "../outside.ts"}),
        ("Write", {"file_path": str(tmp_path / "project-other/a.ts")}),
        ("MultiEdit", {}),
        ("Write", {"file_path": None}),
        ("Write", []),
    ]:
        assert (
            decide_permission(tool, value, mode="auto", workspace_roots=roots)["behavior"] == "deny"
        )
    assert decide_permission("TodoWrite", {}, mode="auto")["behavior"] == "allow"
    assert decide_permission("Skill", {}, mode="auto")["behavior"] == "deny"
    assert decide_permission("Edit", {"file_path": "x"}, mode="auto")["behavior"] == "deny"


def test_auto_file_mutations_cannot_escape_symlinks(tmp_path: Path) -> None:
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    safe = workspace / "safe"
    safe.mkdir(parents=True)
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    (workspace / "safe-link").symlink_to(safe, target_is_directory=True)
    (workspace / "dangling.ts").symlink_to(outside / "missing.ts")
    (workspace / "loop").symlink_to(workspace / "loop")
    for path, expected in [
        (workspace / "escape/new.ts", "deny"),
        (workspace / "dangling.ts", "deny"),
        (workspace / "safe-link/new.ts", "allow"),
        (workspace / "loop", "deny"),
    ]:
        assert (
            decide_permission(
                "Write", {"file_path": str(path)}, mode="auto", workspace_roots=[workspace]
            )["behavior"]
            == expected
        )


def test_permission_floors_and_legacy_state() -> None:
    assert effective_mode("approval-required", "auto") == "auto"
    assert effective_mode("full-access", "auto") == "full-access"
    assert effective_mode(None, "approval-required") == "approval-required"
    assert (
        provider_permission({"provider_permissions": {"cursor": "full-access"}}, "cursor")
        == "full-access"
    )
    assert provider_permission({"permissions": "full"}, "claude") == "full-access"
    assert provider_permission({"permissions": "allowlist"}, "codex") == "approval-required"
    assert provider_permission({}, "gemini") == "auto"
    assert (
        provider_permission(
            {"provider_permissions": {"cursor": "auto"}, "permissions": "full"}, "cursor"
        )
        == "auto"
    )


@pytest.mark.parametrize(
    "separator,allowed", [("\ufeff", True), ("\u00a0", True), ("\x1c", False), ("\u0085", False)]
)
def test_javascript_whitespace_does_not_widen_permission_matching(
    separator: str, allowed: bool
) -> None:
    assert is_auto_bash_command(f"git{separator}status") is allowed
    assert name_tokens(f"get{separator}issue") == (
        ["get", "issue"] if allowed else [f"get{separator}issue"]
    )


def test_invalid_path_bytes_are_denied(tmp_path: Path) -> None:
    assert (
        decide_permission(
            "Write", {"file_path": "bad\0path"}, mode="auto", workspace_roots=[tmp_path]
        )["behavior"]
        == "deny"
    )
    assert (
        decide_permission(
            "Write", {"file_path": "safe"}, mode="auto", workspace_roots=["bad\0root"]
        )["behavior"]
        == "deny"
    )
