# PLAN-001 — Fix `prune-runs` deleting resumable (non-terminal) runs

## Source
- Scope: plugins/wise/scripts/workflows.py (run-history pruning)
- Found by: correctness + tests lenses · leverage 2.5 (impact 5 ÷ effort 2 × confidence 1.0)
- Commit: e9971c5   ← line numbers/excerpts below are valid at this SHA; re-verify if HEAD moved
- Evidence: plugins/wise/scripts/workflows.py:1291-1347

## Summary
`cmd_prune_runs` promises in its docstring that non-terminal runs
(initializing/running/paused) are "protected and kept regardless of the
cap — they may be actively resumable", but the implementation slices the
combined `non_term + term` list at the cap. Once non-terminal runs alone
exceed `WISE_RUN_HISTORY_CAP` (default 25 — reachable, since abandoned
runs freeze at `running` and accumulate), the oldest non-terminal runs
are `rmtree`'d: irreversible deletion of resumable run state (state.yaml
+ step logs). Done = non-terminal runs are never deleted by prune, and a
regression test pins it.

## Assumptions
- The docstring states the intended behaviour (protect non-terminal);
  the slice is the bug, not the docstring.
- No test infrastructure exists yet in the repo; this plan creates
  `plugins/wise/tests/` and the first pytest file. `pytest` is available
  via `python3 -m pytest` (install with `python3 -m pip install --user pytest`
  if missing).
- `wise_runs_root_for_cwd()` honours `$XDG_DATA_HOME`
  (workflows.py:93-121), so tests can point the runs root at a tmpdir by
  setting `XDG_DATA_HOME` and controlling the cwd.
- `TERMINAL_RUN = {"completed", "failed", "cancelled"}` (workflows.py:129)
  is left as-is here; whether `failed` should count as resumable is
  PLAN-006's decision, deliberately out of scope.

## Decisions Made
- Fix by budgeting deletions from the terminal list only:
  `to_delete = term[max(0, cap - len(non_term)):]`. When non-terminal
  runs alone exceed the cap, nothing terminal is kept but nothing
  non-terminal is deleted — matching the docstring exactly.
- Keep the early return `if len(entries) <= cap: return 0` unchanged.
- Orphan dirs (no/broken state.yaml) stay classified as oldest-terminal
  (existing deliberate behaviour, workflows.py:1315-1323).
- Test infra: plain pytest, no conftest magic beyond a tmp
  `XDG_DATA_HOME` + `monkeypatch.chdir`. Import the module via
  `importlib` from the scripts path (no package install), e.g.
  `spec_from_file_location("workflows", REPO/"plugins/wise/scripts/workflows.py")`.

## Current state
`plugins/wise/scripts/workflows.py:1335-1347`:

```python
    non_term = sorted(
        (e for e in entries if not e[1]),
        key=lambda e: (e[0], e[2].name),
        reverse=True,
    )
    term = sorted(
        (e for e in entries if e[1]),
        key=lambda e: (e[0], e[2].name),
        reverse=True,
    )
    keep = non_term + term
    to_delete = keep[cap:]
```

With `cap=25` and 26 non-terminal entries, `keep[25:]` contains the
oldest non-terminal run → deleted at workflows.py:1349-1360 via
`shutil.rmtree`.

## Tasks
Wave 1
- [ ] Change the deletion budget in `cmd_prune_runs` to
      `to_delete = term[max(0, cap - len(non_term)):]` (drop the `keep`
      concatenation; keep both sorts) — Reuse:
      plugins/wise/scripts/workflows.py:1335-1347 — 0.5 SP
- [ ] Create `plugins/wise/tests/test_prune_runs.py` with pytest cases:
      (a) non-terminal runs alone over cap → zero deletions;
      (b) mixed over cap → only oldest terminal runs deleted, all
      non-terminal kept; (c) under cap → no-op; (d) orphan dir (no
      state.yaml) deleted first. Build runs as
      `<XDG_DATA_HOME>/wise/runs/<cwd-slug>/<run-id>/state.yaml` with
      `status:`/`last_activity_at:` keys; slug per `_cwd_slug()`
      (workflows.py:105-112: absolute cwd with `/`→`-`). Set
      `WISE_RUN_HISTORY_CAP` via `monkeypatch.setenv` — New:
      plugins/wise/tests/test_prune_runs.py — 1.5 SP

Total: 2 SP

## Testing
New: `plugins/wise/tests/test_prune_runs.py` (first test file in the
repo — no existing pattern to copy; use plain pytest functions +
`tmp_path`/`monkeypatch` fixtures).

## Validation
- `python3 -m py_compile plugins/wise/scripts/workflows.py` → exits 0
- `python3 -m pytest plugins/wise/tests/test_prune_runs.py -q` → all pass
- `bash -c 'cd /tmp && python3 <repo>/plugins/wise/scripts/workflows.py prune-runs'` → exits 0 (smoke: no traceback on empty root)

## Stop conditions
- If `cmd_prune_runs` at HEAD no longer matches the excerpt above
  (someone already fixed or refactored it), halt and report.
- If the docstring's protection claim has been deliberately changed to
  "cap applies to all runs", halt — the fix direction inverts.
- Do not touch `TERMINAL_RUN` membership or resume/list subcommands
  (PLAN-006 owns those).
