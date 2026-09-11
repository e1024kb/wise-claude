from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from wise_engine import defs, resolve

REPO = Path(__file__).resolve().parents[4]
VALIDATOR = REPO / "scripts/validate_repo.py"


@pytest.fixture
def validator(tmp_path):
    spec = importlib.util.spec_from_file_location("repo_validator", VALIDATOR)
    assert spec and spec.loader
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.REPO_ROOT = tmp_path
    return module


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_actual_repository_validates_without_legacy_runtime():
    result = subprocess.run(
        [sys.executable, str(VALIDATOR)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    assert "OK: workflow.yaml files" in result.stdout and "OK: agent roster" in result.stdout


def test_missing_workflows_fail(validator):
    errors: list[str] = []
    validator.check_workflows(errors, defs)
    assert len(errors) == 1 and "no bundled workflow definitions" in errors[0]


def test_new_skill_requires_shared_question_lifecycle(validator, tmp_path):
    path = write(
        tmp_path,
        "plugins/wise/skills/wise-new/SKILL.md",
        "# New skill\nAsk the user to choose.\n",
    )
    errors: list[str] = []
    validator.check_question_lifecycle(errors)
    assert len(errors) == 1 and "wise-new/SKILL.md" in errors[0]
    path.write_text(
        path.read_text() + "Follow the [question lifecycle]"
        "(../../references/workflow-host-control.md#keep-asynchronous-questions-open).\n"
    )
    errors = []
    validator.check_question_lifecycle(errors)
    assert errors == []


def test_unreadable_skill_reports_question_lifecycle_error(validator, tmp_path):
    path = tmp_path / "plugins/wise/skills/wise-new/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff")

    errors: list[str] = []
    validator.check_question_lifecycle(errors)

    assert len(errors) == 1
    assert "wise-new/SKILL.md: could not read file" in errors[0]


@pytest.mark.parametrize(
    "body,expected",
    [
        (
            "version: 2\nname: demo\nsteps: [{id: a, type: bash, run: echo ok, depends_on: [missing]}]\n",
            "missing",
        ),
        ("version: 1\nname: demo\nsteps: []\n", "v1 workflow"),
        ("[broken", "cannot validate workflow"),
        ("version: 2\nname: wrong\nsteps: [{id: a, type: bash, run: echo ok}]\n", "storage name"),
        (
            "version: 2\nname: demo\nsteps: [{id: a, type: bash, run: echo ok, when: {invalid: true}}]\n",
            "when",
        ),
    ],
)
def test_invalid_workflows_use_canonical_errors(validator, tmp_path, body, expected):
    write(tmp_path, "plugins/wise/workflows/demo/workflow.yaml", body)
    errors: list[str] = []
    validator.check_workflows(errors, defs)
    assert any(expected in error for error in errors), errors


def test_flat_and_folder_definitions_are_checked(validator, tmp_path):
    for relative, name in (("a.yaml", "a"), ("b/workflow.yaml", "b")):
        write(
            tmp_path,
            "plugins/wise/workflows/" + relative,
            f"version: 2\nname: {name}\nsteps: [{{id: run, type: bash, run: echo ok}}]\n",
        )
    errors: list[str] = []
    validator.check_workflows(errors, defs)
    assert errors == []


def test_missing_engine_still_reports_structural_errors(tmp_path):
    write(tmp_path, ".claude-plugin/marketplace.json", "broken")
    write(tmp_path, "plugins/wise/references/bad.md", "${CLAUDE_PLUGIN_ROOT}/missing.py")
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 1
    assert "cannot load canonical validator" in result.stderr
    assert "invalid JSON" in result.stderr and "missing.py does not resolve" in result.stderr
    assert "Traceback" not in result.stderr


def test_broken_engine_import_cannot_reuse_loaded_package(validator, tmp_path):
    validator.REPO_ROOT = REPO
    validator._load_engine_modules()
    validator.REPO_ROOT = tmp_path
    write(
        tmp_path,
        "plugins/wise/engine/wise_engine/__init__.py",
        "raise RuntimeError('broken package')",
    )
    with pytest.raises(RuntimeError, match="broken package"):
        validator._load_engine_modules()


def test_roster_and_skill_frontmatter_fail_closed(validator, tmp_path):
    write(tmp_path, "plugins/wise/agents/worker.md", "---\nname: wrong\ndescription: ''\n---\n")
    errors: list[str] = []
    validator.check_roster(errors, resolve)
    assert len(errors) == 2
    write(
        tmp_path,
        "plugins/wise/skills/wise-bad/SKILL.md",
        "---\nname: wise-bad\ndescription: Example\nunknown: true\nallowed-tools: 'Read, Bash(broken'\n---\n",
    )
    errors = []
    validator.check_skill_frontmatter(errors, resolve.parse_frontmatter)
    assert any("unknown frontmatter key" in error for error in errors)
    assert any("malformed allowed-tools" in error for error in errors)
    write(tmp_path, "plugins/wise/skills/wise-bad/SKILL.md", "---\n[broken\n---\n")
    errors = []
    validator.check_skill_frontmatter(errors, resolve.parse_frontmatter)
    assert any("description" in error for error in errors)


def test_document_catalog_and_source_checks_are_retained(validator, tmp_path):
    write(
        tmp_path,
        "plugins/wise/skills/wise-test/SKILL.md",
        "---\nname: wise-test\ndescription: Test\nargument-hint: '<text>'\n---\n",
    )
    write(tmp_path, "plugins/wise/README.md", "| `/wise-missing` | bad |")
    write(tmp_path, "plugins/wise/CLAUDE.md", "`/wise-missing`")
    write(
        tmp_path,
        ".claude-plugin/marketplace.json",
        json.dumps({"plugins": [{"name": "x", "source": "github:x/y#main"}]}),
    )
    errors: list[str] = []
    validator.check_skill_doc_sync(errors, resolve.parse_frontmatter)
    assert len(errors) == 4
    errors = []
    validator.check_marketplace_sources(errors)
    assert len(errors) == 1 and "SHA-pinned" in errors[0]
