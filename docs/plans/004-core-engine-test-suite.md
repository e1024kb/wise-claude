# PLAN-004 — Core engine test suite (scheduler, state, worktree-include)

## Source
- Scope: plugins/wise/scripts/workflows.py, plugins/wise/hooks/session-end-ingest.sh
- Found by: tests lens (findings 2,3,4,5,9 deduped) · leverage 0.8 (impact 4 ÷ effort 5 × confidence 1.0)
- Commit: e9971c5
- Evidence: workflows.py:659-691 (trigger rules), :737-797 (next-wave + `when:` at :769-773), :229-244 + :1366-1403 (session freshness / find-runs-by-session), :399-474 (init-state/start-run), :694-734 + :1515-1527 (render), :1532-1614 (apply-worktree-include), hooks/session-end-ingest.sh:20-50

## Summary
The workflow engine's core — DAG trigger rules, next-wave terminal
classification, `when:` evaluation, state lifecycle, session-freshness
(the PR #15 fix), template rendering, and the PR #20 worktree-include
copier with its path-escape guard — is pure or tmpdir-testable logic
with zero tests. Any regression silently wedges every workflow run for
every plugin user. Done = a pytest suite pinning these behaviours, wired
into the CI from PLAN-002.

## Assumptions
- PLAN-001 (creates plugins/wise/tests/ + the importlib loading pattern)
  and PLAN-002 (CI runs pytest) land first. If PLAN-005/006 have landed,
  test the FIXED behaviour they define (atomic tmp names, step-id
  validation, failed-run semantics) — read those plans' Decisions if the
  code looks changed.
- workflows.py stays a single file invoked by path (CONTRIBUTING §9.4
  "single doorway"); tests import it via importlib, never install it.
- `wise_data_root()` honours `$XDG_DATA_HOME` (workflows.py:93-101) —
  all state tests run against a tmpdir via `monkeypatch.setenv` +
  `monkeypatch.chdir`.
- Worktree-include tests may create throwaway git repos in `tmp_path`
  (git init + config user.email/name locally); this is test-local, not a
  mutation of the working tree.

## Decisions Made
- One test file per concern under plugins/wise/tests/:
  test_scheduler.py, test_state_lifecycle.py, test_render.py,
  test_worktree_include.py, test_session_freshness.py. Shared importlib
  loader + XDG fixture in conftest.py.
- Render tests PIN current behaviour (sequential str.replace,
  outputs-can-shadow, unresolved `{{...}}` left verbatim, non-matching
  `when:` treated truthy) — freezing the contract, not redesigning it.
  A behaviour change later must consciously edit these tests.
- Shellcheck of the hook is CI's job (PLAN-002); here add exactly 4 bats
  cases for session-end-ingest.sh's hard contract (always exit 0, no
  stdout) ONLY if `bats` is trivially available; otherwise implement the
  same 4 cases as a pytest that runs the script via subprocess with
  crafted stdin — decision: use pytest+subprocess, no new tool.
- No coverage targets, no mocking frameworks — plain pytest.

## Current state
Zero test files exist (`find . -name 'test_*'` → only PLAN-001's file if
landed). Key behaviours to pin, all currently unspecified anywhere but
code:
- `_trigger_rule_satisfied` (workflows.py:659-691): 4 rules
  (all-success default, all-done, one-success, always); unknown rule
  falls back to all-success with a stderr warning (:690-691).
- `cmd_next_wave` (:737-797): a pending step whose deps can never
  complete is classified failed; `when:` expressions that do not match
  the `<lhs> == <rhs>` regex are treated as TRUE (:769-773).
- `cmd_apply_worktree_include` (:1532-1614): reads `.worktreeinclude`
  patterns, matches `git ls-files --others` output, copies with
  overwrite, and refuses any resolved path escaping source or dest root
  via `resolve().relative_to()` (:1589-1592); always exits 0
  (best-effort contract).
- `_session_is_fresh` / `cmd_find_runs_by_session` (:229-244,
  :1366-1403): non-terminal same-session runs are conflicts only when
  `last_activity_at` is younger than WISE_SESSION_STALE_SECS (default
  1800).
- session-end-ingest.sh:20-50: parses hook JSON from stdin with python3,
  invokes insights.py ingest; `set +e`; must always exit 0 and emit no
  stdout.

## Tasks
Wave 1
- [ ] conftest.py: importlib loader for workflows.py + fixture that sets
      XDG_DATA_HOME=tmp_path and chdirs to a tmp workspace — New:
      plugins/wise/tests/conftest.py — 1 SP
- [ ] test_scheduler.py: parametrized trigger-rule truth table (each
      rule × dep-status combos), unknown-rule fallback, next-wave
      pending-unreachable→failed, `when:` regex semantics (match-false
      skips, non-matching-expression runs) — New — 2 SP

Wave 2
- [ ] test_state_lifecycle.py: init-state → start-run → update-step →
      record-output → reset-running round-trip on tmpdir state.yaml;
      find-runs-by-session fresh-vs-stale (monkeypatch
      WISE_SESSION_STALE_SECS; craft last_activity_at timestamps) — New
      — 2 SP
- [ ] test_worktree_include.py: tmp git repo fixture; patterns copy
      untracked files; tracked files not copied; overwrite semantics;
      `../escape` and absolute-path patterns refused (path-guard); exit
      0 on missing .worktreeinclude — New — 2 SP
- [ ] test_render.py (pin current contract per Decisions) +
      test_hook_contract.py (subprocess: empty stdin / garbage JSON /
      missing transcript_path / python3 absent via PATH stub → exit 0,
      stdout empty every time) — New — 2 SP

Total: 8 SP → decomposed above; treat waves as the ≤3 SP/task boundary. Total rounds to 8 SP.

## Testing
This plan IS tests. Each test must fail when its target logic is
sabotaged (spot-verify one per file by temporary local mutation during
development, then revert).

## Validation
- `python3 -m pytest plugins/wise/tests -q` → all pass, ≥15 tests collected
- `python3 -m py_compile plugins/wise/scripts/workflows.py` → exits 0 (unchanged)
- `git diff --stat -- plugins/wise/scripts` → empty (tests only; no production edits in this plan)

## Stop conditions
- If a test reveals a NEW production bug (beyond those already planned
  in 001/005/006), do not fix it here — pin the current behaviour with
  an xfail + comment and report it.
- If workflows.py internals moved so far from the cited lines that a
  target function no longer exists, halt and report.
- If PLAN-001's tests/ scaffold is absent AND creating it conflicts with
  an in-flight PLAN-001 run, halt.
