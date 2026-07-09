#!/usr/bin/env python3
"""engine.py — wise plugin skill-catalog emitter.

Invoked only by the `/wise` natural-language helper skill (via
scripts/engine.sh, which handles the Python bootstrap) with the
subcommand `list-skills`. Walks the plugin's skills/ dir, reads
SKILL.md frontmatter, and emits a JSON catalog on stdout that the
helper consumes when classifying the user's free-form request.

stdout (on `list-skills`): a JSON document with three top-level keys:

  {
    "standalone":          list of skills with `argument-hint:` in fm
    "reference":           list of skills without `argument-hint:`
    "siblings_installed":  always empty — wise ships as a single plugin
  }

Each skill entry carries `name`, `plugin`, `description`, and (for
standalone) `argument-hint`. The helper uses the first sentence of
`description` for display and the full text for intent classification.

Exit codes:
  0 — catalog emitted
  1 — catastrophic failure (missing skills dir, malformed frontmatter,
      unsupported subcommand)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or SCRIPT_DIR.parent)
SKILLS_DIR = PLUGIN_ROOT / "skills"
HELPER_NAME = "wise"


def _bail_missing_deps() -> int:
    """If yaml is missing, re-invoke bootstrap-deps.sh so the caller
    gets the BOOTSTRAP: protocol tags it already knows how to handle."""
    bootstrap = SCRIPT_DIR / "bootstrap-deps.sh"
    if bootstrap.is_file():
        result = subprocess.run(["bash", str(bootstrap)], capture_output=True, text=True)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode or 1
    print("engine: PyYAML missing and bootstrap-deps.sh not found", file=sys.stderr)
    return 1


try:
    import yaml
except ImportError:
    sys.exit(_bail_missing_deps())


def load_frontmatter(path: Path) -> dict | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end == -1:
        return None
    fm_text = text[4:end]
    try:
        loaded = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, dict) else {}


def _discover_in(skills_dir: Path, plugin: str) -> tuple[list[dict], list[dict]]:
    """Walk one plugin's `skills/` dir; return (standalone, reference).

    Discriminator: `argument-hint:` present in frontmatter → standalone
    (user-invocable slash command). Absent → reference (auto-triggered
    by description matching). Skills named `wise` (the helper itself)
    are skipped in both buckets."""
    standalone: list[dict] = []
    reference: list[dict] = []
    if not skills_dir.is_dir():
        return standalone, reference
    for d in sorted(skills_dir.iterdir()):
        if not d.is_dir():
            continue
        md = d / "SKILL.md"
        if not md.is_file():
            continue
        fm = load_frontmatter(md)
        if not fm:
            continue
        name = fm.get("name")
        if not name or name == HELPER_NAME:
            continue
        entry = {
            "name": name,
            "plugin": plugin,
            "description": (fm.get("description") or "").strip(),
        }
        if "argument-hint" in fm:
            entry["argument-hint"] = (fm.get("argument-hint") or "").strip()
            standalone.append(entry)
        else:
            reference.append(entry)
    return standalone, reference


def discover_all_skills() -> dict:
    """Walk the plugin's skills/ dir; return the catalog."""
    standalone, reference = _discover_in(SKILLS_DIR, plugin="wise")
    return {
        "standalone": standalone,
        "reference": reference,
        # wise ships as a single plugin; the key is kept for catalog-shape stability.
        "siblings_installed": {},
    }


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] != "list-skills":
        print(
            "engine.py: the only supported subcommand is `list-skills`.\n"
            f"Usage: {Path(sys.argv[0]).name} list-skills",
            file=sys.stderr,
        )
        return 1
    if not SKILLS_DIR.is_dir():
        print(f"engine: skills dir not found at {SKILLS_DIR}", file=sys.stderr)
        return 1
    catalog = discover_all_skills()
    json.dump(catalog, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
