# PLAN-007 — Bootstrap/engine cold-start fixes + dependency pinning

## Source
- Scope: plugins/wise/scripts/bootstrap-deps.sh, plugins/wise/scripts/engine.py
- Found by: security + deps + correctness lenses (4 deduped findings) · leverage 1.0 (impact 3 ÷ effort 3 × confidence 1.0)
- Commit: e9971c5
- Evidence: bootstrap-deps.sh:26-34 (exit contract), :106-109 (probe exit 3), :111-128 (unpinned PKGS + pip install), :282 (quote-unsafe interpolation); engine.py:42-58 (`return result.returncode or 1` at :50)

## Summary
Four cold-start/supply-chain defects: (1) `pip install --user` of
completely unpinned packages — a compromised or breaking PyPI release
lands on every user's machine at bootstrap; (2) `--probe` mode exits 3
(`BOOTSTRAP:missing-modules`) but the documented contract reserves 3 for
"module install failed", and the tag is undocumented — callers can't
tell routine cold start from broken install; (3) plugin version probe
interpolates `${CLAUDE_PLUGIN_ROOT}` into inline Python source — a path
containing a quote breaks it silently (`|| true`); (4) `engine.py`'s
missing-deps bail returns `result.returncode or 1`, so when
bootstrap-deps.sh SUCCEEDS (exits 0 after auto-installing), engine still
exits 1 without retrying the import — first-ever `/wise` invocation on a
fresh machine fails confusingly even though the env was just fixed.

## Assumptions
- Version floors (not exact pins) are right for a user-site install that
  must coexist with other user packages: `pyyaml>=6,<7`,
  `python-ulid>=2,<4`, `typing_extensions>=4,<5`. Verify current majors
  on PyPI before finalizing ceilings; keep floors conservative.
- The BOOTSTRAP: protocol consumers are the wise skills themselves
  (grep `BOOTSTRAP:` across plugins/wise/skills and references to
  enumerate them) — any exit-code change must update every consumer in
  the same commit.
- engine.py may re-exec itself safely: the failing import happens at
  module import time, so `os.execv(sys.executable, [sys.executable] +
  sys.argv)` after a successful bootstrap re-runs with the same argv;
  guard with an env flag (e.g. `WISE_ENGINE_REEXEC=1`) to prevent loops.

## Decisions Made
- Pin via version ranges in the `PKGS` case-map (bootstrap-deps.sh:113-119),
  e.g. `yaml) PKGS+=("pyyaml>=6,<7")`. No requirements.txt — the
  script is the single install path today (keep one source of truth).
  `--require-hashes` is NOT adopted (would break `--user` convenience
  installs; noted in index as a possible follow-up).
- Probe mode: missing modules exit **2** with tag
  `BOOTSTRAP:missing-modules <mods>` — consistent with the other
  "needs something" exits — and the header contract (:26-34) documents
  the tag. Exit 3 stays exclusively pip-failed.
- Version probe at :282 switches to the argv-passing heredoc pattern
  already used 2 lines later for REG_JSON (:284+) — path goes in as
  `sys.argv[1]`, never interpolated into source.
- engine.py `_bail_missing_deps`: if bootstrap returncode == 0 →
  re-exec self once (env-flag guard); non-zero → propagate that code
  (drop the `or 1` coercion).

## Current state
bootstrap-deps.sh:106-109:
```bash
if [[ -n "$MISSING" ]]; then
  if [[ "$MODE" == "probe" ]]; then
    echo "BOOTSTRAP:missing-modules $MISSING"
    exit 3
```
bootstrap-deps.sh:128: `if ! "$PY" -m pip install --user --quiet "${PKGS[@]}"; then` — PKGS built from bare names (`pyyaml`, `python-ulid`, `typing_extensions`) at :113-119.
bootstrap-deps.sh:282:
```bash
PLUGIN_VERSION="$("$PY" -c "import json; print(json.load(open('${CLAUDE_PLUGIN_ROOT:-$(dirname "$0")/..}/.claude-plugin/plugin.json'))['version'])" 2>/dev/null || true)"
```
engine.py:46-50:
```python
    if bootstrap.is_file():
        result = subprocess.run(["bash", str(bootstrap)], capture_output=True, text=True)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode or 1
```

## Tasks
Wave 1
- [ ] Pin PKGS with version ranges; document ranges in the header
      comment — Reuse: bootstrap-deps.sh:111-128 — 1 SP
- [ ] Probe exit-code: missing-modules → exit 2; document the tag in the
      :26-34 contract; grep `BOOTSTRAP:` consumers across
      plugins/wise/{skills,references,workflows} and update any that
      branch on exit 3 for probe — Reuse: bootstrap-deps.sh:20-35,
      :106-109 — 1 SP
- [ ] Fix :282 via the argv/heredoc pattern from :284 — Reuse:
      bootstrap-deps.sh:284-315 — 0.5 SP
- [ ] engine.py: re-exec on bootstrap success (env-flag loop guard),
      propagate real code on failure — Reuse: engine.py:42-58 — 1 SP

Total: 3 SP (rounded)

## Testing
- Shell: `bash -n plugins/wise/scripts/bootstrap-deps.sh` + shellcheck
  (non-blocking) — no bats infra required for this plan.
- Python: if plugins/wise/tests exists, add a subprocess test invoking
  engine.py with a PYTHONPATH that hides yaml + a stub bootstrap script
  exiting 0, asserting one re-exec then success; otherwise validate
  manually per Validation.

## Validation
- `bash -n plugins/wise/scripts/bootstrap-deps.sh` → exits 0
- `python3 -m py_compile plugins/wise/scripts/engine.py` → exits 0
- `plugins/wise/scripts/bootstrap-deps.sh --probe` on a complete env → exits 0, `READY:` lines
- `grep -n 'exit 3' plugins/wise/scripts/bootstrap-deps.sh` → only the pip-failed path
- `grep -rn 'BOOTSTRAP:missing-modules' plugins/wise` → tag documented in header + emitted in probe path; consumers consistent
- In a scratch venv without pyyaml: `python3 plugins/wise/scripts/engine.py --help` (or its catalog mode) → bootstrap runs, engine re-execs, exits 0 — no manual re-run needed

## Stop conditions
- If any skill consumes `exit 3` as "missing modules" in a way that
  cannot be updated in the same commit, STOP and report the coupling.
- If python-ulid / pyyaml current major versions differ from the
  assumed ranges, adjust floors only after checking the scripts' actual
  API usage; if usage is incompatible with the latest major, report
  instead of pinning blind.
- Do not introduce a requirements.txt or change the install location
  (`--user`) — out of scope.
