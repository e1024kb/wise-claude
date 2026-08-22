#!/usr/bin/env python3
"""Repo validation harness for wise-claude.

Cross-checks the invariants this repo relies on contributor discipline
for today: JSON manifests parse, every bundled workflow.yaml is
internally consistent (step ids/types/trigger-rules/depends_on), every
skill's frontmatter is well-formed (`name:` matches its directory,
`description` non-empty, only known keys, no forbidden v1 fields,
allowed-tools entries parse), the skill catalog stays in sync with
the docs that list it (standalone skills ↔ README command-table rows
and CLAUDE.md mentions, both directions), every
`${CLAUDE_PLUGIN_ROOT}/<path>` reference in the scanned docs resolves
to a real file, every `{{workflow.dir}}/prompts/<file>` reference
inside a workflow's own markdown resolves to a real file (other
`{{workflow.dir}}/...` forms, e.g. `templates/...`, are not checked),
and every marketplace plugin `source` is either a local path or
SHA-pinned.

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

try:
    import yaml
except ImportError:
    sys.exit(
        "error: PyYAML is required to run this validator "
        "(pip install pyyaml)"
    )

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
RUNTIME_GENERATED_REFS = {".wise-init-registry.yaml", ".wise-version"}

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
            json.loads(path.read_text(encoding="utf-8"))
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
            data = yaml.safe_load(workflow_yaml.read_text(encoding="utf-8"))
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

        steps = data.get("steps", [])
        if not isinstance(steps, list):
            errors.append(f"{rel}: top-level 'steps' is not a list: {steps!r}")
            continue
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
            if not isinstance(step_type, str) or step_type not in step_types:
                errors.append(
                    f"{rel}: step {step_id!r} has unknown type {step_type!r}"
                )

            trigger_rule = step.get("trigger-rule")
            if trigger_rule is not None and (
                not isinstance(trigger_rule, str) or trigger_rule not in trigger_rules
            ):
                errors.append(
                    f"{rel}: step {step_id!r} has unknown trigger-rule {trigger_rule!r}"
                )

        for step in steps:
            if not isinstance(step, dict):
                continue
            step_id = step.get("id")
            depends_on = step.get("depends_on", [])
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


# The frontmatter keys wise skills use today, plus the upstream Agent
# Skills spec keys (`license`, `metadata`) so a legitimate upstream key
# never hard-fails CI. Extend deliberately when a new key is adopted —
# an unknown key is an error rather than a warning because a typo of a
# meaningful key changes behaviour silently: engine.py buckets a skill
# as standalone-vs-reference on the literal "argument-hint" key, so a
# misspelling demotes the skill from the /wise catalog with no failure.
KNOWN_SKILL_KEYS = {
    "name",
    "description",
    "argument-hint",
    "allowed-tools",
    "model",
    "effort",
    "disable-model-invocation",
    "license",
    "metadata",
}

# v1 dispatcher-routing fields with no meaning in v2, plus
# `user-invocable` (must not be set at all — the default `true` is the
# only supported value). See CONTRIBUTING §2.1.
FORBIDDEN_SKILL_KEYS = {
    "command",
    "subcommand",
    "subcommand-aliases",
    "arguments",
    "user-invocable",
}

# One allowed-tools entry: a bare tool name (`Read`, `Write`) or a
# parenthesised scoped grant (`Bash(git:*)`,
# `Bash(${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py:*)`). Catches
# unbalanced parens and stray characters. Well-formedness ONLY — it
# does not judge grant scope or danger (`Bash(rm -rf ~:*)` is
# well-formed); the "narrowly scoped" invariant stays a review call.
ALLOWED_TOOL_ENTRY_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_-]*(\([^()]+\))?$"
)


def _split_allowed_tools(value: str) -> list[str]:
    """Split a comma-separated allowed-tools string, ignoring commas
    inside parens so `Bash(gh pr list --json number,title)` stays one
    entry."""
    entries: list[str] = []
    depth = 0
    current = ""
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            entries.append(current)
            current = ""
        else:
            current += ch
    entries.append(current)
    return entries


def check_skill_frontmatter(errors: list[str], parse_frontmatter) -> None:
    skills_dir = REPO_ROOT / WISE_PLUGIN_DIR / "skills"
    for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        if not (skill_dir / "SKILL.md").is_file():
            errors.append(
                f"{skill_dir.relative_to(REPO_ROOT)}: skill directory has no SKILL.md"
            )
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        rel = skill_md.relative_to(REPO_ROOT)
        dir_name = skill_md.parent.name
        frontmatter = parse_frontmatter(skill_md)

        name = frontmatter.get("name")
        if name != dir_name:
            errors.append(f"{rel}: frontmatter name {name!r} != dir name {dir_name!r}")

        description = frontmatter.get("description")
        if not isinstance(description, str) or not description.strip():
            errors.append(f"{rel}: frontmatter 'description' missing or empty")

        # The plugin contract (CLAUDE.md invariants): exactly one skill
        # sets `disable-model-invocation: true` — the /wise helper.
        dmi = frontmatter.get("disable-model-invocation")
        if dir_name == "wise":
            if dmi is not True:
                errors.append(
                    f"{rel}: the wise helper must set "
                    "'disable-model-invocation: true'"
                )
        elif dmi is not None:
            errors.append(
                f"{rel}: only skills/wise/SKILL.md may set "
                "'disable-model-invocation'"
            )

        for key in frontmatter:
            if key in FORBIDDEN_SKILL_KEYS:
                errors.append(
                    f"{rel}: forbidden frontmatter field {key!r} "
                    "(CONTRIBUTING §2.1 — must not be set, remove it)"
                )
            elif key not in KNOWN_SKILL_KEYS:
                errors.append(
                    f"{rel}: unknown frontmatter key {key!r} "
                    f"(allowed: {', '.join(sorted(KNOWN_SKILL_KEYS))})"
                )

        allowed_tools = frontmatter.get("allowed-tools")
        if allowed_tools is not None:
            # Both shapes are valid: a comma-separated string (the common
            # form) or a YAML list (wise-estimation, wise-human-writing).
            entries: list[str] = []
            if isinstance(allowed_tools, str):
                entries = _split_allowed_tools(allowed_tools)
            elif isinstance(allowed_tools, list):
                for e in allowed_tools:
                    if isinstance(e, str):
                        entries.append(e)
                    else:
                        errors.append(
                            f"{rel}: allowed-tools list entry {e!r} is "
                            f"{type(e).__name__}, not a string (unquoted "
                            "YAML null/bool?)"
                        )
            else:
                errors.append(
                    f"{rel}: 'allowed-tools' must be a comma-separated string "
                    f"or a list, got {type(allowed_tools).__name__}"
                )
            for entry in entries:
                entry = entry.strip()
                if not entry:
                    errors.append(
                        f"{rel}: empty allowed-tools entry "
                        "(doubled or trailing comma)"
                    )
                elif not ALLOWED_TOOL_ENTRY_RE.match(entry):
                    errors.append(
                        f"{rel}: malformed allowed-tools entry {entry!r}"
                    )


# Full `/wise-<name>` tokens in backticks, with or without trailing
# args inside the backticks (`/wise-report --save`) and with the
# optional canonical `wise:` namespace prefix. Partial prefix mentions
# like `/wise-` or `/wise-workflow-` (trailing dash) and templated
# forms like `/wise-<action>` don't match — the name must end in
# [a-z0-9].
WISE_CMD_TOKEN_RE = re.compile(
    r"`/(?:wise:)?(wise-[a-z0-9-]*[a-z0-9])(?:\s[^`]*)?`"
)
# A README command-table row's leading invocation token; tolerates
# bold/link wrapping and the canonical `wise:` prefix.
README_ROW_RE = re.compile(
    r"^\|\s*(?:\*\*|\[)?\s*`/(?:wise:)?(wise-[a-z0-9-]*[a-z0-9])"
)


def check_skill_doc_sync(errors: list[str], parse_frontmatter) -> None:
    """Set-diff skills/*/ against the two docs that catalog them.

    Standalone skills (frontmatter has `argument-hint`; the `/wise`
    helper excluded — it is documented as the natural-language helper,
    not a command-table row) must appear as a row in README.md's
    command table and be mentioned in CLAUDE.md. In reverse, every
    full `/wise-*` token those docs name must have a backing skill
    directory — a typo'd or stale row otherwise survives silently.
    """
    plugin_root = REPO_ROOT / WISE_PLUGIN_DIR
    skills_dir = plugin_root / "skills"
    skill_names = {p.parent.name for p in skills_dir.glob("*/SKILL.md")}
    standalone = {
        name
        for name in skill_names
        if name != "wise"
        and "argument-hint" in parse_frontmatter(skills_dir / name / "SKILL.md")
    }

    readme = plugin_root / "README.md"
    claude_md = plugin_root / "CLAUDE.md"
    texts: dict[Path, str] = {}
    for doc in (readme, claude_md):
        try:
            texts[doc] = doc.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{doc.relative_to(REPO_ROOT)}: could not read file ({exc})")
    if len(texts) < 2:
        return

    readme_rel = readme.relative_to(REPO_ROOT)
    claude_rel = claude_md.relative_to(REPO_ROOT)
    readme_text = texts[readme]
    claude_text = texts[claude_md]

    readme_rows = {
        m.group(1)
        for line in readme_text.splitlines()
        if (m := README_ROW_RE.match(line))
    }
    claude_tokens = set(WISE_CMD_TOKEN_RE.findall(claude_text))

    for name in sorted(standalone - readme_rows):
        errors.append(
            f"{readme_rel}: standalone skill {name!r} has no command-table row"
        )
    for name in sorted(standalone - claude_tokens):
        errors.append(
            f"{claude_rel}: standalone skill {name!r} is not mentioned"
        )
    for name in sorted(readme_rows - skill_names):
        errors.append(
            f"{readme_rel}: command-table row `/{name}` has no skills/{name}/ directory"
        )
    for name in sorted(claude_tokens - skill_names):
        errors.append(
            f"{claude_rel}: mentions `/{name}` but skills/{name}/ does not exist"
        )


def _clean_ref(ref: str) -> str | None:
    """Strip trailing shell-glob-permission suffixes (`:*`, `\\`) and
    trailing punctuation from a captured path reference. Returns None
    for references that are templated placeholders (`<name>`, `${x}`)
    or bare directory mentions (trailing `/`, e.g. prose like
    "...under `${CLAUDE_PLUGIN_ROOT}/references/pr/`") rather than
    concrete file paths — those aren't files to check."""
    ref = ref.split(":*", 1)[0]
    ref = ref.rstrip("\\").rstrip(".,;:")
    if "<" in ref or "${" in ref or "…" in ref or ref.endswith("/"):
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
    # Root-level plugin/repo docs and the repo's live docs/wise/ tree
    # also carry ${CLAUDE_PLUGIN_ROOT} references in prose (e.g.
    # plugins/wise/README.md, plugins/wise/CLAUDE.md,
    # CONTRIBUTING.md, docs/wise/workflows.md) — not just the
    # skills/workflows/references
    # markdown scanned above. docs/plans/ is deliberately excluded: those
    # are point-in-time planning artifacts describing proposed or
    # historical states, not live docs whose references must resolve now.
    for extra in (
        plugin_root / "README.md",
        plugin_root / "CLAUDE.md",
        REPO_ROOT / "CONTRIBUTING.md",
    ):
        if extra.is_file():
            md_files.append(extra)
    docs_wise_dir = REPO_ROOT / "docs" / "wise"
    if docs_wise_dir.is_dir():
        md_files.extend(sorted(docs_wise_dir.rglob("*.md")))

    for md_path in md_files:
        rel = md_path.relative_to(REPO_ROOT)
        try:
            text = md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{rel}: could not read file ({exc})")
            continue

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
    for rel_manifest in (".claude-plugin/marketplace.json",):
        path = REPO_ROOT / rel_manifest
        if not path.is_file():
            continue
        rel = path.relative_to(REPO_ROOT)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            errors.append(f"{rel}: invalid JSON")
            continue
        if not isinstance(data, dict):
            errors.append(f"{rel}: top-level JSON is not a mapping")
            continue
        plugins = data.get("plugins", [])
        if not isinstance(plugins, list):
            errors.append(f"{rel}: 'plugins' is not a list: {plugins!r}")
            continue
        for plugin in plugins:
            if not isinstance(plugin, dict):
                errors.append(f"{rel}: plugin entry is not a mapping: {plugin!r}")
                continue
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
    doc_sync_errors: list[str] = []
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
        workflows_py = f"{WISE_PLUGIN_DIR}/scripts/workflows.py"
        load_error = f"{workflows_py}: {exc.code}"
        workflow_errors.append(
            f"{load_error} (workflow.yaml checks skipped — they depend on this module)"
        )
        skill_errors.append(
            f"{load_error} (skill frontmatter check skipped — it depends on this module)"
        )
        doc_sync_errors.append(
            f"{load_error} (skill doc-sync check skipped — it depends on this module)"
        )
    else:
        workflows_py = f"{WISE_PLUGIN_DIR}/scripts/workflows.py"
        try:
            step_types = workflows_module.STEP_TYPES
            trigger_rules = workflows_module.TRIGGER_RULES
            parse_frontmatter = workflows_module._parse_frontmatter
        except AttributeError as exc:
            missing_error = f"{workflows_py}: missing expected export ({exc})"
            workflow_errors.append(
                f"{missing_error} (workflow.yaml checks skipped — they depend on this export)"
            )
            skill_errors.append(
                f"{missing_error} (skill frontmatter check skipped — it depends on this export)"
            )
            doc_sync_errors.append(
                f"{missing_error} (skill doc-sync check skipped — it depends on this export)"
            )
        else:
            check_workflows(workflow_errors, step_types, trigger_rules)
            check_skill_frontmatter(skill_errors, parse_frontmatter)
            check_skill_doc_sync(doc_sync_errors, parse_frontmatter)

    sections = [
        ("json manifests", json_errors),
        ("workflow.yaml files", workflow_errors),
        ("skill frontmatter", skill_errors),
        ("skill doc sync", doc_sync_errors),
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
