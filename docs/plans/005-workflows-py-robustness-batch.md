# PLAN-005 — workflows.py robustness batch (atomic writes, step-id validation, plugin-name probe)

## Source
- Scope: plugins/wise/scripts/workflows.py, plugins/wise/scripts/init-registry.py
- Found by: correctness lens (findings 3,4,5,9 deduped) · leverage 0.9 (impact 3 ÷ effort 3 × confidence 0.9)
- Commit: e9971c5
- Evidence: workflows.py:253-258 (save_yaml fixed tmp), :1433-1441 (heartbeat truncate-write), :480-499 (write-log unvalidated step id), :344-363 (installed_plugins fallback), init-registry.py:63-67

## Summary
Four small robustness defects in the engine: (1) `save_yaml` writes via
a FIXED sibling tmp path (`state.yaml.tmp`) so two concurrent writers to
one run can interleave and install truncated state (same pattern in
init-registry.py); (2) worker heartbeat files are truncate-written, so a
supervisor poll can read an empty file and misclassify a live worker as
stale (nudges cap at 2 before TaskStop+respawn); (3) step ids from
user-authored YAML are never validated, and `write-log` interpolates
`<step-id>.<step-run-id>` into a path — an id containing `/` or `..`
writes outside `<run-dir>/logs/` under the conductor's blanket Bash
grant; (4) `installed_plugins()`'s filesystem fallback records the
DIRECTORY name containing `.claude-plugin/plugin.json`, which in the
cache layout `cache/<mkt>/<plugin>/<version>/` is the VERSION string —
so `probe-requires` reports required plugins missing; plus `iterdir()`
is unguarded against PermissionError. Done = all four fixed + tests.

## Assumptions
- PLAN-001 landed (tests scaffold + importlib pattern exists). PLAN-004
  is not required first; tests added here are self-contained.
- Single-writer is still the design intent; unique tmp names make the
  last-writer-wins window atomic (no torn file). Full fcntl locking is
  deliberately NOT added (out of scope; noted in index).
- Existing input-name validation at workflows.py:590 shows the accepted
  identifier convention to mirror for step ids.

## Decisions Made
- `save_yaml`: use `tempfile.NamedTemporaryFile(dir=path.parent,
  prefix=path.name + ".", suffix=".tmp", delete=False)` + `os.replace`.
  Same change in init-registry.py `save_registry`.
- Heartbeat: write to `workers/<name>.hb.<pid>.tmp` then `os.replace`
  onto `<name>.hb`.
- Step-id validation: at `init-state` (and `next-wave` defensively),
  reject ids not matching `^[a-z][a-z0-9_-]*$` with a clear error
  naming the workflow file — mirroring the input-name validation style
  at workflows.py:590. This also protects `write-log`'s path build; no
  change needed inside `cmd_write_log` itself beyond relying on
  validated ids (belt: assert the id matches there too, cheap).
- `installed_plugins()` fallback: read the `name` field from the found
  plugin.json (fall back to dir name if the key is absent/unparseable);
  wrap the two `iterdir()` calls in try/except OSError → skip.
- Missing `id`/`type` keys in a step dict raise KeyError today
  (`_render_step`, :730-733) — covered by the same init-state
  validation: require both keys per step.

## Current state
workflows.py:253-258:
```python
def save_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)
    tmp.replace(path)
```
workflows.py:1440: `(workers_dir / f"{name}.hb").write_text(line + "\n")`
workflows.py:494: `path = Path(run_dir) / "logs" / f"{step_id}.{step_run_id}.log"`
workflows.py:352-354 (fallback walk):
```python
        pj = p / ".claude-plugin" / "plugin.json"
        if pj.is_file():
            names.add(p.name)
```
(comment at :348-349 claims the walk "covers
cache/<marketplace>/<plugin>/<version>/" — it finds the file there but
records the version dir's name.)

## Tasks
Wave 1
- [ ] save_yaml + init-registry save_registry → unique-tmp + os.replace —
      Reuse: workflows.py:253-258, init-registry.py:63-67 — 1 SP
- [ ] Heartbeat atomic write — Reuse: workflows.py:1433-1441 — 0.5 SP
- [ ] Step-id + required-key validation in init-state/next-wave with the
      regex above; assert in cmd_write_log — Reuse: validation style at
      workflows.py:590 — 1 SP
- [ ] installed_plugins fallback: read plugin.json `name`, guard
      iterdir — Reuse: workflows.py:344-363 — 0.5 SP

Wave 2
- [ ] Tests: concurrent-ish save_yaml (two writers, distinct tmp names —
      assert no `.tmp` collision file left and final YAML parses);
      init-state rejects `id: "../evil"` and id-less steps; fake cache
      layout `cache/m/p/1.2.3/.claude-plugin/plugin.json` with
      `name: p` → probe finds `p` not `1.2.3` — New:
      plugins/wise/tests/test_robustness.py — 2 SP

Total: 5 SP

## Testing
New: plugins/wise/tests/test_robustness.py per Wave 2 (pattern: PLAN-001's
test_prune_runs.py — importlib + tmp XDG_DATA_HOME).

## Validation
- `python3 -m py_compile plugins/wise/scripts/workflows.py plugins/wise/scripts/init-registry.py` → exits 0
- `python3 -m pytest plugins/wise/tests -q` → all pass
- Smoke: `python3 plugins/wise/scripts/workflows.py init-state --workflow plugins/wise/workflows/example-workflow/workflow.yaml ...` (per its --help) → still succeeds on the bundled workflows (all ids already conform)
- `grep -rn 'with_suffix.*\.tmp' plugins/wise/scripts/*.py` → no matches

## Stop conditions
- If any bundled workflow.yaml step id fails the new regex, STOP — the
  regex contradicts shipped content; report instead of loosening
  silently.
- If save_yaml call sites rely on the `.tmp` sibling name (grep before
  changing), halt and report.
- fcntl locking, `update-step` arbitrary-key schema guard, and
  `cmd_render` redesign are OUT of scope (index: noted items).
