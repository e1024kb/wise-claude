#!/usr/bin/env python3
"""Repo validation harness for wise-claude.

Cross-checks the invariants this repo relies on contributor discipline
for today: JSON manifests parse, every bundled workflow.yaml is
internally consistent (step ids/types/trigger-rules/depends_on), every
skill's frontmatter `name:` matches its directory, every
`${CLAUDE_PLUGIN_ROOT}` / `{{workflow.dir}}` reference in the docs
resolves to a real file, and every marketplace plugin `source` is
either a local path or SHA-pinned.

Exits 0 and prints a per-section OK summary when everything checks out;
exits non-zero and prints one `file: reason` line per failure otherwise
(all failures are collected before exiting, not just the first).

Usage: python3 scripts/validate_repo.py [--root <repo-root>]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WISE_PLUGIN_DIR = "plugins/wise"

STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# Matches ${CLAUDE_PLUGIN_ROOT}/<path>, stopping at the first char that
# cannot appear in a bare filesystem path reference embedded in prose /
# shell snippets (whitespace, quote, backtick, paren, colon-suffixed
# shell globs like `:*`).
PLUGIN_ROOT_REF_RE = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([^\s'\"`)]+)")
WORKFLOW_DIR_REF_RE = re.compile(r"\{\{workflow\.dir\}\}/prompts/([^\s'\"`)]+)")

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# References to files a skill writes at runtime (not shipped in the
# repo, so they never exist on disk here) rather than a static asset
# the skill reads. Documented as "gets wiped on every /plugin install"
# in wise-init/SKILL.md — a real absence, not a broken doc link.
RUNTIME_GENERATED_REFS = {".wise-init-registry.yaml"}


def _load_workflows_module():
    """Load `plugins/wise/scripts/workflows.py` by absolute path via
    importlib — it is not importable by package name from the repo
    root — so the constants below are the single source of truth
    instead of a duplicated, driftable copy."""
    path = REPO_ROOT / WISE_PLUGIN_DIR / "scripts" / "workflows.py"
    spec = importlib.util.spec_from_file_location("wise_workflows", path)
    if spec is None or spec.loader is None:
        sys.exit(f"error: cannot load {path} for validation")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"error: failed to load {path}: {exc}")
    return module


def check_json_manifests(errors: list[str]) -> None:
    for rel in (
        ".claude-plugin/marketplace.json",
        f"{WISE_PLUGIN_DIR}/.claude-plugin/plugin.json",
    ):
        path = REPO_ROOT / rel
        try:
            json.loads(path.read_text())
        except FileNotFoundError:
            errors.append(f"{rel}: file not found")
        except json.JSONDecodeError as exc:
            errors.append(f"{rel}: invalid JSON ({exc})")


def check_workflows(errors: list[str], step_types: set, trigger_rules: set) -> None:
    workflows_dir = REPO_ROOT / WISE_PLUGIN_DIR / "workflows"
    for workflow_yaml in sorted(workflows_dir.glob("*/workflow.yaml")):
        rel = workflow_yaml.relative_to(REPO_ROOT)
        folder_name = workflow_yaml.parent.name
        try:
            data = yaml.safe_load(workflow_yaml.read_text())
        except yaml.YAMLError as exc:
            errors.append(f"{rel}: invalid YAML ({exc})")
            continue
        if not isinstance(data, dict):
            errors.append(f"{rel}: top-level YAML is not a mapping")
            continue

        top_name = data.get("name")
        if top_name != folder_name:
            errors.append(
                f"{rel}: folder name {folder_name!r} != top-level name {top_name!r}"
            )

        steps = data.get("steps") or []
        seen_ids: set[str] = set()
        for step in steps:
            if not isinstance(step, dict):
                errors.append(f"{rel}: step entry is not a mapping: {step!r}")
                continue
            step_id = step.get("id")
            if not step_id or not STEP_ID_RE.match(str(step_id)):
                errors.append(f"{rel}: invalid step id {step_id!r}")
            elif step_id in seen_ids:
                errors.append(f"{rel}: duplicate step id {step_id!r}")
            else:
                seen_ids.add(step_id)

            step_type = step.get("type")
            if step_type not in step_types:
                errors.append(
                    f"{rel}: step {step_id!r} has unknown type {step_type!r}"
                )

            trigger_rule = step.get("trigger-rule")
            if trigger_rule is not None and trigger_rule not in trigger_rules:
                errors.append(
                    f"{rel}: step {step_id!r} has unknown trigger-rule {trigger_rule!r}"
                )

        for step in steps:
            if not isinstance(step, dict):
                continue
            step_id = step.get("id")
            depends_on = step.get("depends_on") or []
            if not isinstance(depends_on, list) or not all(
                isinstance(dep, str) for dep in depends_on
            ):
                errors.append(
                    f"{rel}: step {step_id!r} depends_on must be a list of strings, "
                    f"got {depends_on!r}"
                )
                continue
            for dep in depends_on:
                if dep not in seen_ids:
                    errors.append(
                        f"{rel}: step {step_id!r} depends_on unresolved id {dep!r}"
                    )


def check_skill_frontmatter(errors: list[str], parse_frontmatter) -> None:
    skills_dir = REPO_ROOT / WISE_PLUGIN_DIR / "skills"
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        rel = skill_md.relative_to(REPO_ROOT)
        dir_name = skill_md.parent.name
        frontmatter = parse_frontmatter(skill_md)
        name = frontmatter.get("name")
        if name != dir_name:
            errors.append(f"{rel}: frontmatter name {name!r} != dir name {dir_name!r}")


def _clean_ref(ref: str) -> str | None:
    """Strip trailing shell-glob-permission suffixes (`:*`, `\\`) and
    trailing punctuation from a captured path reference. Returns None
    for references that are templated placeholders (`<name>`, `${x}`)
    or bare directory mentions (trailing `/`, e.g. prose like
    "...under `${CLAUDE_PLUGIN_ROOT}/references/pr/`") rather than
    concrete file paths — those aren't files to check."""
    ref = ref.split(":*", 1)[0]
    ref = ref.rstrip("\\").rstrip(".,;:")
    if "<" in ref or "${" in ref or ref.endswith("/"):
        return None
    return ref


def check_doc_references(errors: list[str]) -> None:
    plugin_root = REPO_ROOT / WISE_PLUGIN_DIR
    search_dirs = [
        plugin_root / "skills",
        plugin_root / "workflows",
        plugin_root / "references",
    ]
    md_files: list[Path] = []
    for d in search_dirs:
        md_files.extend(sorted(d.rglob("*.md")))

    for md_path in md_files:
        rel = md_path.relative_to(REPO_ROOT)
        text = md_path.read_text()

        for match in PLUGIN_ROOT_REF_RE.finditer(text):
            ref = _clean_ref(match.group(1))
            if ref is None or ref in RUNTIME_GENERATED_REFS:
                continue
            target = plugin_root / ref
            if not target.is_file():
                errors.append(
                    f"{rel}: ${{CLAUDE_PLUGIN_ROOT}}/{ref} does not resolve to a file"
                )

        if "workflows" in md_path.parts:
            # The enclosing `workflows/<name>` dir, regardless of how
            # deep under it (e.g. workflows/<name>/prompts/foo.md).
            idx = md_path.parts.index("workflows")
            workflow_dir = Path(*md_path.parts[: idx + 2])
            for match in WORKFLOW_DIR_REF_RE.finditer(text):
                ref = _clean_ref(match.group(1))
                if ref is None:
                    continue
                target = workflow_dir / "prompts" / ref
                if not target.is_file():
                    errors.append(
                        f"{rel}: {{{{workflow.dir}}}}/prompts/{ref} does not resolve to a file"
                    )


def check_marketplace_sources(errors: list[str]) -> None:
    path = REPO_ROOT / ".claude-plugin/marketplace.json"
    rel = path.relative_to(REPO_ROOT)
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        # Already reported by check_json_manifests.
        return
    for plugin in data.get("plugins") or []:
        source = plugin.get("source", "")
        name = plugin.get("name", "<unnamed>")
        if not isinstance(source, str):
            errors.append(
                f"{rel}: plugin {name!r} source {source!r} is neither a local "
                "./ path nor SHA-pinned"
            )
            continue
        if source.startswith("./"):
            continue
        # Accept a 40-char hex SHA anywhere in the source string (e.g.
        # a `github:owner/repo#<sha>` or `git+https://...#<sha>` form).
        if any(SHA_RE.match(tok) for tok in re.split(r"[#/@]", source)):
            continue
        errors.append(
            f"{rel}: plugin {name!r} source {source!r} is neither a local "
            "./ path nor SHA-pinned"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repo root (default: parent of this script's directory)",
    )
    args = parser.parse_args()

    global REPO_ROOT
    if args.root is not None:
        REPO_ROOT = args.root.resolve()

    json_errors: list[str] = []
    workflow_errors: list[str] = []
    skill_errors: list[str] = []
    ref_errors: list[str] = []
    source_errors: list[str] = []

    # Run the checks that don't depend on workflows.py first, so a
    # missing/broken workflows.py still gets json/doc-ref/source errors
    # reported instead of aborting the whole harness before any output.
    check_json_manifests(json_errors)
    check_doc_references(ref_errors)
    check_marketplace_sources(source_errors)

    try:
        workflows_module = _load_workflows_module()
    except SystemExit as exc:
        workflow_errors.append(str(exc.code))
        skill_errors.append(
            "skipped: workflows.py failed to load, see workflow.yaml errors above"
        )
    else:
        step_types = workflows_module.STEP_TYPES
        trigger_rules = workflows_module.TRIGGER_RULES
        parse_frontmatter = workflows_module._parse_frontmatter
        check_workflows(workflow_errors, step_types, trigger_rules)
        check_skill_frontmatter(skill_errors, parse_frontmatter)

    sections = [
        ("json manifests", json_errors),
        ("workflow.yaml files", workflow_errors),
        ("skill frontmatter", skill_errors),
        ("doc cross-references", ref_errors),
        ("marketplace source pins", source_errors),
    ]

    all_errors = [e for _, errs in sections for e in errs]

    for label, errs in sections:
        if errs:
            for e in errs:
                print(e, file=sys.stderr)
        else:
            print(f"OK: {label}")

    if all_errors:
        print(f"FAILED: {len(all_errors)} issue(s) found", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
