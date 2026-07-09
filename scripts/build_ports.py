#!/usr/bin/env python3
"""Deterministic port generator for wise-claude.

Renders every generated file under harnesses/ from the canonical
sources plus the per-harness inputs in core/ports/:

  harnesses/claude/wise/references/   <- core/references/  (byte-copy)
  harnesses/claude/wise/workflows/    <- core/workflows/   (doc links
                                          re-based one dir deeper)
  harnesses/claude/wise/agents/       <- core/agents/ + frontmatter
                                          lines from profiles/claude.yaml
  harnesses/claude/wise/scripts/      <- core/scripts/ engine trio
                                          (byte-copy)
  harnesses/<p>/wise/agents/          <- core/agents/      (byte-copy)
  harnesses/<p>/wise/scripts/         <- core/scripts/ trio + the
                                          claude port's init trio
                                          (byte-copy; scripts self-locate
                                          and are never env-rewritten)
  harnesses/<p>/wise/references/      <- core/references/  (env rewrite)
  harnesses/<p>/wise/workflows/       <- core/workflows/   (env rewrite)
  harnesses/<p>/wise/skills/          <- harnesses/claude/wise/skills/
                                          (transform pipeline below)
  harnesses/<p>/wise/.gitignore       <- harnesses/claude/wise/.gitignore
  static extras                       <- core/ports/static/<p>/ (byte-copy)

for <p> in the non-Claude harness profiles (codex, cursor, hermes).
The Claude port's skills/ are the canonical skill source and are never
generated.

Skill transform pipeline (claude -> port), per CONTRIBUTING §10.3 and
the per-harness profile:
  1. frontmatter: keep only the profile's `frontmatter_keep` keys;
     strip the "(bare alias) or `/wise:<name>` (canonical)" invocation
     prose and spell out ${CLAUDE_PLUGIN_DATA} in the description;
     re-emit as a `>-` folded scalar greedily wrapped at 72 chars.
  2. env rewrite (body): `"${CLAUDE_PLUGIN_ROOT}` (the quoted,
     executable form — every fenced-bash occurrence is quoted) becomes
     the port's canonical defaulted expansion; any remaining
     ${CLAUDE_PLUGIN_ROOT} becomes the short ${WISE_PLUGIN_ROOT};
     ${CLAUDE_PLUGIN_DATA} becomes ${WISE_DATA_DIR:-$HOME/.local/share/wise}.
  3. overlays: exact find/replace hunks from core/ports/overlays/<p>/
     (genuinely divergent prose; anchors are post-rewrite text).
  4. preamble / adaptation note per the profile's tier lists, rendered
     from core/ports/notes/ ({{harness_name}}/{{harness_id}} vars;
     notes/<skill>.<p>.md overrides notes/<skill>.md).
  5. skills in the profile's `excluded_skills` are not ported.

Pure function of the committed inputs — no timestamps, no randomness.

Usage:
  python3 scripts/build_ports.py            # regenerate in place
  python3 scripts/build_ports.py --check    # render to a temp dir,
                                            # diff against the tree,
                                            # exit non-zero on any diff
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
import textwrap
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("error: PyYAML is required to run the port generator "
             "(pip install pyyaml)")

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE = REPO_ROOT / "core"
PORTS_INPUTS = CORE / "ports"
CLAUDE_PORT = REPO_ROOT / "harnesses" / "claude" / "wise"

# The engine trio lives in core/scripts/ and is byte-copied everywhere.
ENGINE_SCRIPTS = ("engine.py", "engine.sh", "workflows.py")
# The init trio has no core/ source: the Claude port is its source of
# truth and every port vendors it byte-identical. (insights.py is
# Claude-only and is not vendored.)
CLAUDE_SOURCED_SCRIPTS = ("bootstrap-deps.sh", "init-registry.py", "init.sh")

# Files that exist at runtime inside a port dir but are never
# generated (nor committed).
RUNTIME_IGNORE_NAMES = {".wise-init-registry.yaml", ".wise-version", ".DS_Store"}
RUNTIME_IGNORE_DIRS = {"__pycache__"}

DESCRIPTION_WRAP_WIDTH = 72
INVOCATION_PROSE_RE = re.compile(r" \(bare alias\) or `/wise:[a-z-]+` \(canonical\)")

OVERLAY_FIND = "<<<<<<<"
OVERLAY_SEP = "======="
OVERLAY_END = ">>>>>>>"


def long_root(harness: str) -> str:
    """The canonical defaulted expansion for a port's executable bash
    contexts (CONTRIBUTING §10.3) — must byte-match what install.sh
    lays down and what validate_repo.py enforces."""
    return ("${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-"
            "$HOME/.local/share}/wise}/harness/" + harness + "}")


def rewrite_env(text: str, harness: str) -> str:
    """Context-sensitive env-var rewrite for port markdown/yaml.

    The executable-context discriminator is a preceding double quote:
    every fenced-bash / pasted-into-a-shell occurrence in the sources
    is written as "${CLAUDE_PLUGIN_ROOT}/..." (including escaped-quote
    forms like \\"${...}\\" inside prompt templates), while prose and
    Read references are backticked or bare. Quoted occurrences get the
    defaulted expansion; the rest get the short neutral var.
    """
    text = text.replace('"${CLAUDE_PLUGIN_ROOT}', '"' + long_root(harness))
    text = text.replace("${CLAUDE_PLUGIN_ROOT}", "${WISE_PLUGIN_ROOT}")
    # The data dir keeps the short chain in both prose and bash — the
    # committed convention (no XDG level here).
    text = text.replace("${CLAUDE_PLUGIN_DATA}",
                        "${WISE_DATA_DIR:-$HOME/.local/share/wise}")
    return text


def render_template(template: str, profile: dict) -> str:
    return (template
            .replace("{{harness_name}}", profile["name"])
            .replace("{{harness_id}}", profile["id"]))


# ---------------------------------------------------------------------------
# frontmatter


def split_frontmatter(text: str, rel: str) -> tuple[str, str]:
    """Return (frontmatter_yaml, body). `body` starts right after the
    closing '---\\n' line (i.e. usually with a blank line then the H1)."""
    if not text.startswith("---\n"):
        sys.exit(f"error: {rel}: no frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        sys.exit(f"error: {rel}: unterminated frontmatter")
    return text[4:end + 1], text[end + len("\n---\n"):]


def port_description(desc: str) -> str:
    """Flatten a claude skill description and apply the port edits."""
    flat = " ".join(desc.split())
    flat = INVOCATION_PROSE_RE.sub("", flat)
    flat = flat.replace("${CLAUDE_PLUGIN_DATA}/", "the wise data dir's ")
    return flat


def emit_frontmatter(meta: dict, keep: list[str]) -> str:
    lines = ["---"]
    for key in keep:
        if key not in meta:
            continue
        if key == "description":
            wrapped = textwrap.wrap(
                port_description(meta["description"]),
                width=DESCRIPTION_WRAP_WIDTH,
                break_long_words=False,
                break_on_hyphens=False,
            )
            lines.append("description: >-")
            lines.extend("  " + line for line in wrapped)
        else:
            value = meta[key]
            if isinstance(value, bool):
                value = "true" if value else "false"
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# overlays


def parse_overlay(text: str, rel: str) -> list[tuple[str, str]]:
    """Parse an overlay file into (find, replace) hunks. Lines outside
    the <<<<<<< / ======= / >>>>>>> markers are commentary."""
    hunks: list[tuple[str, str]] = []
    state = "out"
    find_lines: list[str] = []
    repl_lines: list[str] = []
    for line in text.split("\n"):
        if line == OVERLAY_FIND and state == "out":
            state, find_lines, repl_lines = "find", [], []
        elif line == OVERLAY_SEP and state == "find":
            state = "replace"
        elif line == OVERLAY_END and state == "replace":
            hunks.append(("\n".join(find_lines), "\n".join(repl_lines)))
            state = "out"
        elif state == "find":
            find_lines.append(line)
        elif state == "replace":
            repl_lines.append(line)
    if state != "out":
        sys.exit(f"error: {rel}: unterminated overlay hunk")
    if not hunks:
        sys.exit(f"error: {rel}: no hunks found")
    return hunks


def apply_overlay(body: str, hunks: list[tuple[str, str]], rel: str) -> str:
    for find, replace in hunks:
        count = body.count(find)
        if count != 1:
            sys.exit(f"error: {rel}: overlay hunk matches {count} times "
                     f"(expected exactly 1):\n{find}")
        body = body.replace(find, replace)
    return body


# ---------------------------------------------------------------------------
# skill pipeline


def load_note(skill: str, profile: dict) -> str:
    override = PORTS_INPUTS / "notes" / f"{skill}.{profile['id']}.md"
    base = PORTS_INPUTS / "notes" / f"{skill}.md"
    path = override if override.is_file() else base
    if not path.is_file():
        sys.exit(f"error: no adaptation-note template for note-tier "
                 f"skill {skill!r} (expected {base})")
    return render_template(path.read_text(encoding="utf-8"), profile)


def render_skill_md(text: str, skill: str, profile: dict, rel: str) -> str:
    fm_text, body = split_frontmatter(text, rel)
    meta = yaml.safe_load(fm_text)

    body = rewrite_env(body, profile["id"])

    overlay_path = PORTS_INPUTS / "overlays" / profile["id"] / f"{skill}.md"
    if overlay_path.is_file():
        hunks = parse_overlay(
            overlay_path.read_text(encoding="utf-8"),
            str(overlay_path.relative_to(REPO_ROOT)))
        body = apply_overlay(body, hunks, rel)

    if skill in profile["note_skills"]:
        note = load_note(skill, profile).rstrip("\n")
        body = "\n" + note + "\n\n\n" + body.lstrip("\n")
    elif skill in profile["blockquote_skills"]:
        preamble = render_template(
            (PORTS_INPUTS / "notes" / "_preamble.md").read_text(encoding="utf-8"),
            profile).rstrip("\n")
        lines = body.split("\n")
        h1 = next((i for i, l in enumerate(lines) if l.startswith("# ")), None)
        if h1 is None or lines[h1 + 1] != "":
            sys.exit(f"error: {rel}: cannot find 'H1 + blank line' "
                     "insertion point for the preamble blockquote")
        lines[h1 + 2:h1 + 2] = [preamble, ""]
        body = "\n".join(lines)

    return emit_frontmatter(meta, profile["frontmatter_keep"]) + body


# ---------------------------------------------------------------------------
# tree rendering


def _files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def render_all() -> dict[str, bytes]:
    """Render every generated file: {repo-relative posix path: bytes}."""
    out: dict[str, bytes] = {}

    def put(rel: Path | str, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        out[str(rel)] = data

    profiles_dir = PORTS_INPUTS / "profiles"
    profiles = {
        p.stem: yaml.safe_load(p.read_text(encoding="utf-8"))
        for p in sorted(profiles_dir.glob("*.yaml"))
    }
    claude_profile = profiles.pop("claude")

    # ----- claude port (references / workflows / agents / engine trio)
    claude_rel = Path("harnesses/claude/wise")
    for f in _files_under(CORE / "references"):
        put(claude_rel / "references" / f.relative_to(CORE / "references"),
            f.read_bytes())
    for f in _files_under(CORE / "workflows"):
        rel = claude_rel / "workflows" / f.relative_to(CORE / "workflows")
        text = f.read_text(encoding="utf-8")
        # core's workflow docs link to the repo's docs/ tree relative to
        # a port's workflows/<name>/ dir; the claude port sits one level
        # deeper (harnesses/claude/wise/...), so re-base the links.
        put(rel, text.replace("../../../../docs/", "../../../../../docs/"))
    for f in _files_under(CORE / "agents"):
        name = f.stem
        extra = claude_profile["agent_frontmatter"].get(name)
        if extra is None:
            sys.exit(f"error: profiles/claude.yaml has no agent_frontmatter "
                     f"entry for {name!r}")
        text = f.read_text(encoding="utf-8")
        end = text.find("\n---\n", 4)
        if not text.startswith("---\n") or end == -1:
            sys.exit(f"error: core/agents/{f.name}: no frontmatter")
        extra_lines = "".join(f"{k}: {v}\n" for k, v in extra.items())
        put(claude_rel / "agents" / f.name,
            text[:end + 1] + extra_lines + text[end + 1:])
    for name in ENGINE_SCRIPTS:
        put(claude_rel / "scripts" / name, (CORE / "scripts" / name).read_bytes())

    # ----- non-claude ports
    claude_skills = CLAUDE_PORT / "skills"
    for harness in sorted(profiles):
        profile = profiles[harness]
        port_rel = Path("harnesses") / harness / "wise"

        for f in _files_under(CORE / "agents"):
            put(port_rel / "agents" / f.name, f.read_bytes())
        for name in ENGINE_SCRIPTS:
            put(port_rel / "scripts" / name, (CORE / "scripts" / name).read_bytes())
        for name in CLAUDE_SOURCED_SCRIPTS:
            put(port_rel / "scripts" / name,
                (CLAUDE_PORT / "scripts" / name).read_bytes())
        for src_dir in ("references", "workflows"):
            for f in _files_under(CORE / src_dir):
                rel = port_rel / src_dir / f.relative_to(CORE / src_dir)
                put(rel, rewrite_env(f.read_text(encoding="utf-8"), harness))

        put(port_rel / ".gitignore", (CLAUDE_PORT / ".gitignore").read_bytes())

        excluded = set(profile["excluded_skills"])
        for skill_dir in sorted(claude_skills.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name in excluded:
                continue
            skill = skill_dir.name
            for f in _files_under(skill_dir):
                rel = port_rel / "skills" / skill / f.relative_to(skill_dir)
                if f.name == "SKILL.md":
                    put(rel, render_skill_md(
                        f.read_text(encoding="utf-8"), skill, profile, str(rel)))
                elif f.suffix in (".md", ".yaml", ".yml"):
                    put(rel, rewrite_env(f.read_text(encoding="utf-8"), harness))
                else:
                    put(rel, f.read_bytes())

        static_dir = PORTS_INPUTS / "static" / harness
        if static_dir.is_dir():
            for f in _files_under(static_dir):
                put(port_rel / f.relative_to(static_dir), f.read_bytes())

    return out


# ---------------------------------------------------------------------------
# generated-tree bookkeeping


def fully_generated_roots(rendered: dict[str, bytes]) -> list[str]:
    """Directories whose every committed file is generator-owned, so a
    stray file there means drift. The claude port is only partially
    generated (skills/, tests/, hooks/, ... are sources), so only its
    generated subtrees are listed."""
    roots = [
        "harnesses/claude/wise/references",
        "harnesses/claude/wise/workflows",
        "harnesses/claude/wise/agents",
    ]
    harnesses = sorted({p.split("/")[1] for p in rendered
                        if p.startswith("harnesses/") and
                        not p.startswith("harnesses/claude/")})
    roots.extend(f"harnesses/{h}/wise" for h in harnesses)
    return roots


def find_strays(rendered: dict[str, bytes]) -> list[str]:
    strays: list[str] = []
    for root in fully_generated_roots(rendered):
        root_path = REPO_ROOT / root
        if not root_path.is_dir():
            continue
        for f in _files_under(root_path):
            if f.name in RUNTIME_IGNORE_NAMES:
                continue
            if RUNTIME_IGNORE_DIRS & set(f.parts):
                continue
            rel = str(f.relative_to(REPO_ROOT))
            if rel not in rendered:
                strays.append(rel)
    return strays


def write_in_place(rendered: dict[str, bytes]) -> int:
    changed = 0
    for rel, data in sorted(rendered.items()):
        path = REPO_ROOT / rel
        if path.is_file() and path.read_bytes() == data:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"wrote   {rel}")
        changed += 1
    for rel in find_strays(rendered):
        (REPO_ROOT / rel).unlink()
        print(f"removed {rel} (not a generated file)")
        changed += 1
    if changed == 0:
        print(f"up to date ({len(rendered)} generated files, no changes)")
    else:
        print(f"done ({changed} change(s), {len(rendered)} generated files)")
    return 0


def check(rendered: dict[str, bytes]) -> int:
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="build-ports-check-") as tmp:
        tmp_root = Path(tmp)
        for rel, data in sorted(rendered.items()):
            tmp_path = tmp_root / rel
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_bytes(data)
            tree_path = REPO_ROOT / rel
            if not tree_path.is_file():
                problems.append(f"missing {rel}")
            elif tree_path.read_bytes() != data:
                problems.append(f"differs {rel}")
        for rel in find_strays(rendered):
            problems.append(f"stray   {rel}")
    if problems:
        for p in problems:
            print(p, file=sys.stderr)
        print(f"FAILED: {len(problems)} generated file(s) out of sync — "
              "run `python3 scripts/build_ports.py`", file=sys.stderr)
        return 1
    print(f"OK: {len(rendered)} generated files match the tree")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the harness ports from core/ + core/ports/.")
    parser.add_argument(
        "--check", action="store_true",
        help="render to a temp dir and diff against the working tree; "
             "exit non-zero if any generated file differs")
    args = parser.parse_args()

    rendered = render_all()
    return check(rendered) if args.check else write_in_place(rendered)


if __name__ == "__main__":
    sys.exit(main())
