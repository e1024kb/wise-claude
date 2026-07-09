#!/usr/bin/env python3
"""Non-blocking core ↔ port divergence report for wise-claude.

`core/` is the canonical, harness-neutral source. Each
`harnesses/<harness>/wise/` port vendors a copy of the core assets it
needs. This script reads `core/core-map.yaml` and, for every mapping
declared `mode: verbatim`, byte-compares the core asset against its
vendored counterpart and reports what has drifted. Mappings declared
`mode: adapted` are listed as "manually verify" and never diffed —
their divergence is intentional (harness-specific frontmatter, path
rewrites, adaptation preambles).

This is advisory tooling: it **always exits 0**. Its output helps a
human (or CI, as a visible non-blocking step) decide when a port needs
re-syncing after a `core/` edit. It never gates a merge — that would
fight the "each port is hand-maintained and may legitimately diverge"
design.

Usage: python3 scripts/report_core_drift.py [--root <repo-root>]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    # Advisory tool — never fail the caller just because pyyaml is absent.
    print("report_core_drift: PyYAML not installed; skipping drift report")
    sys.exit(0)

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_MAP = "core/core-map.yaml"


def _iter_files(base: Path):
    """Yield (relative-path, absolute-path) for every file under `base`
    if it is a directory, or just `base` itself if it is a file."""
    if base.is_dir():
        for p in sorted(base.rglob("*")):
            if p.is_file():
                yield p.relative_to(base), p
    elif base.is_file():
        yield Path(base.name), base


def _compare(
    core_path: Path,
    vendored_path: Path,
    exclude: list[str],
) -> tuple[list[str], list[str], list[str], int]:
    """Return (drifted, missing, extra, in_sync_count) between a core
    asset and its vendored counterpart. Paths may be files or dirs.
    `exclude` is a list of fnmatch patterns tested against each file's
    posix relative path — matches are skipped (per-port files that
    legitimately differ, e.g. location-dependent README links)."""
    import fnmatch

    drifted: list[str] = []
    missing: list[str] = []
    in_sync = 0

    def _excluded(rel: Path) -> bool:
        rp = rel.as_posix()
        return any(fnmatch.fnmatch(rp, pat) for pat in exclude)

    core_files = {
        rel: p for rel, p in _iter_files(core_path) if not _excluded(rel)
    }
    vendored_files = {
        rel: p for rel, p in _iter_files(vendored_path) if not _excluded(rel)
    }

    for rel, core_file in core_files.items():
        vend = vendored_files.get(rel)
        if vend is None:
            missing.append(str((vendored_path / rel)))
            continue
        try:
            same = core_file.read_bytes() == vend.read_bytes()
        except OSError as exc:
            drifted.append(f"{vendored_path / rel} (unreadable: {exc})")
            continue
        if same:
            in_sync += 1
        else:
            drifted.append(str(vendored_path / rel))

    extra = [
        str(vendored_path / rel)
        for rel in vendored_files
        if rel not in core_files
    ]
    return drifted, missing, extra, in_sync


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args()

    global REPO_ROOT
    if args.root is not None:
        REPO_ROOT = args.root.resolve()

    core_map_path = REPO_ROOT / CORE_MAP
    if not core_map_path.is_file():
        print(f"report_core_drift: {CORE_MAP} not found; nothing to check")
        return 0

    try:
        data = yaml.safe_load(core_map_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        print(f"report_core_drift: cannot parse {CORE_MAP}: {exc}")
        return 0

    mappings = data.get("mappings", [])
    if not isinstance(mappings, list):
        print(f"report_core_drift: {CORE_MAP} 'mappings' is not a list")
        return 0

    total_drifted = 0
    total_missing = 0
    total_adapted = 0
    total_in_sync = 0
    lines: list[str] = []

    for mapping in mappings:
        core_rel = mapping.get("core")
        # `core:` paths in the map are relative to the core/ dir;
        # `vendored` paths are repo-root-relative.
        core_path = REPO_ROOT / "core" / core_rel
        for vend in mapping.get("vendored", []):
            harness = vend.get("harness", "?")
            mode = vend.get("mode", "verbatim")
            vend_rel = vend.get("path")
            vend_path = REPO_ROOT / vend_rel

            if mode == "adapted":
                total_adapted += 1
                lines.append(f"  ~ adapted   [{harness}] {core_rel} → {vend_rel} (manually verify)")
                continue

            if not core_path.exists():
                lines.append(f"  ! missing   [{harness}] core asset absent: {core_rel}")
                total_missing += 1
                continue
            if not vend_path.exists():
                lines.append(f"  ! missing   [{harness}] vendored absent: {vend_rel}")
                total_missing += 1
                continue

            exclude = vend.get("exclude", []) or []
            drifted, missing, extra, in_sync = _compare(core_path, vend_path, exclude)
            total_in_sync += in_sync
            if not drifted and not missing and not extra:
                lines.append(f"  = in-sync   [{harness}] {core_rel} → {vend_rel} ({in_sync} file(s))")
            else:
                total_drifted += len(drifted)
                total_missing += len(missing)
                lines.append(f"  ✗ DRIFTED   [{harness}] {core_rel} → {vend_rel}")
                for d in drifted:
                    lines.append(f"      differs: {d}")
                for m in missing:
                    lines.append(f"      missing: {m}")
                for e in extra:
                    lines.append(f"      extra:   {e}")

    print("core ↔ port drift report (advisory — never blocks)")
    print("=" * 52)
    for line in lines:
        print(line)
    print("-" * 52)
    print(
        f"summary: {total_in_sync} file(s) in sync, "
        f"{total_drifted} drifted, {total_missing} missing, "
        f"{total_adapted} adapted-skip"
    )
    if total_drifted or total_missing:
        print("note: drift is advisory. Re-sync the port from core/ if the "
              "divergence is unintended; ignore it if the port legitimately "
              "differs (mark that mapping mode: adapted in core-map.yaml).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
