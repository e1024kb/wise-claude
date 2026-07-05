# PLAN-006 — Make `failed` runs resumable (align code with documented semantics)

## Source
- Scope: plugins/wise/scripts/workflows.py (run-status enums + resume/conflict/prune subcommands)
- Found by: correctness lens · leverage 1.5 (impact 3 ÷ effort 2 × confidence 1.0)
- Commit: e9971c5
- Evidence: workflows.py:129 (TERMINAL_RUN), :139-141 + :213-227 area (session-conflict comment "frozen at running/paused/failed"), :1276 (list-resumable filter), :1292-1296 (prune docstring lists failed as protected), :1366-1403 (find-runs-by-session)

## Summary
The status `failed` is contradictory: code puts it in
`TERMINAL_RUN = {"completed", "failed", "cancelled"}` so failed runs are
invisible to `list-resumable-runs` / `find-runs-by-session` and are
prune-eligible — while the prose (prune docstring: protected set
"initializing/running/paused/failed"; session-conflict comment: runs
"frozen at running/paused/failed" are findable) treats failed as
resumable. A failed run today can never be resumed through the
documented picker and its state can be deleted. Done = one consistent
semantic, implemented and documented.

## Assumptions
- PLAN-001 (prune fix + tests) lands first — this plan changes which
  runs the protected set contains, directly on top of that fix.
- The wise-workflow-resume skill's picker consumes
  `list-resumable-runs` output; making failed runs appear there is the
  intended UX (a failed run is exactly what a user wants to resume after
  fixing the cause). Verify by reading
  plugins/wise/skills/wise-workflow-resume/SKILL.md before editing; if
  that skill explicitly excludes failed runs by design, STOP (see Stop
  conditions).

## Decisions Made
- **Decision: `failed` is resumable/protected.** Rationale: both prose
  sites independently describe failed as protected/findable — the enum
  is the outlier; a failed run holds exactly the state needed to retry;
  deleting or hiding it destroys recovery options. `completed` and
  `cancelled` remain the only terminal statuses.
- Implement as two sets to keep intent explicit:
  `TERMINAL_RUN = {"completed", "cancelled"}` (delete-eligible,
  non-resumable) — and audit every `TERMINAL_RUN` / status-comparison
  use site in workflows.py for which semantic it needs (grep
  `TERMINAL_RUN` and `"failed"`); any site that genuinely needs
  "no longer executing" (e.g. a supervisor loop exit) gets an explicit
  `{"completed", "cancelled", "failed"}` inline set with a comment.
- `cmd_next_wave`'s run-level status transitions are NOT changed — a run
  still becomes `failed` the same way; only what `failed` means for
  resume/conflict/prune changes.
- Docstrings/comments updated to match in the same commit; the
  wise-workflow-resume and wise-workflow-status skill docs re-checked
  for statements like "failed runs cannot be resumed" and corrected.

## Current state
workflows.py:129: `TERMINAL_RUN = {"completed", "failed", "cancelled"}`
Prune docstring (:1292-1296): "Non-terminal runs
(initializing/running/paused/failed) are protected…"
Session-conflict comment (:139-141): "…a run was abandoned mid-flight
and its state froze at running/paused/failed…"
`list-resumable-runs` (:1276 area) and `find-runs-by-session`
(:1366-1403) both filter on `status not in TERMINAL_RUN`, so failed runs
are excluded from both today.

## Tasks
Wave 1
- [ ] Remove `"failed"` from TERMINAL_RUN; grep-audit every use site of
      TERMINAL_RUN and literal `"failed"` run-status comparisons in
      workflows.py; give any site needing "stopped executing" its own
      explicit set + comment — Reuse: workflows.py:129 and use sites —
      2 SP
- [ ] Sync prose: prune docstring, session-conflict comment,
      wise-workflow-resume / wise-workflow-status SKILL.md and
      docs/wise/workflows.md statements about resumable statuses —
      Reuse: the files above — 1 SP

Wave 2
- [ ] Extend PLAN-001's test file (or test_state_lifecycle.py if
      PLAN-004 landed): failed run appears in list-resumable-runs, is
      NOT pruned over-cap, IS reported by find-runs-by-session when
      fresh — New: test cases — 1 SP

Total: 3 SP (rounded)

## Testing
Extend existing pytest files (pattern: plugins/wise/tests/
test_prune_runs.py from PLAN-001). Cases per Wave 2.

## Validation
- `python3 -m pytest plugins/wise/tests -q` → all pass
- `python3 -m py_compile plugins/wise/scripts/workflows.py` → exits 0
- `grep -n 'TERMINAL_RUN' plugins/wise/scripts/workflows.py` → definition shows `{"completed", "cancelled"}`
- `grep -rn 'running/paused/failed' plugins/wise/scripts/workflows.py` → comments consistent with code (failed = resumable)
- Version bump: plugins/wise/.claude-plugin/plugin.json minor bump (behaviour change)

## Stop conditions
- If wise-workflow-resume/SKILL.md documents failed-is-not-resumable as
  a deliberate product decision (not just mirroring the enum), STOP and
  report — the fix direction flips to prose-align-to-code.
- If the TERMINAL_RUN audit finds a use site where resumable-failed
  creates a loop (e.g. a watchdog that would endlessly respawn failed
  runs), STOP and report with the site.
- PLAN-001 not landed → STOP (ordering dependency).
