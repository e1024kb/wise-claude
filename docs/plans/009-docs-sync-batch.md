# PLAN-009 — Docs sync batch (version badge, README tables, step types, rename leftovers)

## Source
- Scope: README.md, plugins/wise/README.md, docs/wise/dispatcher.md (+ its 3 inbound links)
- Found by: docs lens (findings 1,2,3,4,5,7) + debt lens (finding 10), deduped · leverage 1.0 (impact 2 ÷ effort 2 × confidence 1.0)
- Commit: e9971c5
- Evidence: README.md:5 (badge `version-1.0.0-blue` vs plugin.json `2.2.1`); plugins/wise/README.md:209-213 (workflow table lacks impl-plan-auto) and :17; plugins/wise/README.md:66-92 (commands table lacks /wise-code-review-auto and /wise-simplify-auto); plugins/wise/README.md:173-178 ("Six step types" vs 7 in workflows.py:124-127, `supervised-prompt` missing); README.md:30-63 (no insights/supervise bullet vs plugin.json:4 headline); README.md:39 (unnamed ticket-planning invocation); docs/wise/dispatcher.md:1-15 (self-disclaiming name) with inbound links at plugins/wise/README.md:293, docs/wise/insights.md:312, docs/wise/skills-authoring.md:21

## Summary
Seven verified doc-drift defects, all user-facing: the repo landing page
claims v1.0.0; the plugin README omits one of four bundled workflows and
two shipped slash commands; the step-type list understates the engine by
one type; the flagship insights subsystem is absent from the root
README's feature list; a feature bullet names no invocation; and
docs/wise/dispatcher.md is named for a concept its first paragraph
disclaims. Done = every doc claim matches the shipped 2.2.1 surface.

## Assumptions
- plugin.json version at execution time may be >2.2.1 — badge should
  reflect whatever plugin.json says THEN (single source of truth).
- Renaming dispatcher.md → helper.md is safe: the 3 inbound links are
  all in-repo (verified); external deep links are accepted collateral
  (repo is pre-1000-stars personal project).
- CONTRIBUTING.md:807 mandates the plugin README workflow table stays in
  sync — this plan enforces it once; PLAN-002's CI does not check README
  tables (prose), so note the manual invariant remains.

## Decisions Made
- Badge: keep a static badge but set it from plugin.json's current
  value; do NOT introduce a dynamic badge service (adds an external
  dependency for cosmetics).
- Commands table: add rows for `/wise-code-review-auto` and
  `/wise-simplify-auto` using each SKILL.md frontmatter description as
  the summary source (same convention as existing rows).
- Workflow table: add the impl-plan-auto row (summary from
  workflows/impl-plan-auto/README.md intro) and fix the :17 intro
  sentence listing bundled workflows.
- Step types: rewrite the sentence to enumerate all seven incl.
  `supervised-prompt` (one-line description from docs/wise/workflows.md:164,195).
- Root README: add one "Self-improvement" bullet (insights loop +
  /wise-supervise pointer) to "What you get"; name
  `/wise-workflow-run ticket-plan` in the planning bullet (README.md:39).
- dispatcher.md → helper.md via `git mv`; update the 3 inbound links;
  first line of the doc keeps explaining there is no dispatcher.

## Current state
README.md:5: `![version](https://img.shields.io/badge/version-1.0.0-blue)`
plugins/wise/README.md:173-174: "Six step types are supported — `skill`
…, `prompt` …, `interactive` …, `bash` …, `approval` …, and `ask` …"
plugins/wise/README.md workflow table (:209-213): rows for
example-workflow, ticket-plan, ticket-auto only.
Commands table (:66-92): no `/wise-code-review-auto`,
no `/wise-simplify-auto` (grep confirms zero mentions file-wide).
docs/wise/dispatcher.md:1 opens by stating there is no dispatcher.

## Tasks
Wave 1
- [ ] README.md: badge → current plugin.json version; add
      self-improvement bullet; name the ticket-plan invocation at :39 —
      Reuse: plugin.json version field — 0.5 SP
- [ ] plugins/wise/README.md: add impl-plan-auto workflow row + fix :17;
      add 2 missing command rows; fix step-type sentence to 7 — Reuse:
      frontmatter descriptions + workflows/impl-plan-auto/README.md —
      1 SP
- [ ] `git mv docs/wise/dispatcher.md docs/wise/helper.md`; update
      inbound links at plugins/wise/README.md:293,
      docs/wise/insights.md:312, docs/wise/skills-authoring.md:21 —
      Reuse: git mv — 0.5 SP

Total: 2 SP

## Testing
No executable surface. Verification is grep gates below.

## Validation
- `grep -n 'version-1.0.0' README.md` → no match; badge shows plugin.json's version
- `grep -c 'impl-plan-auto' plugins/wise/README.md` → ≥1
- `grep -n 'wise-code-review-auto\|wise-simplify-auto' plugins/wise/README.md` → both present in the commands table
- `grep -n 'Six step types' plugins/wise/README.md` → no match; `grep -n 'supervised-prompt' plugins/wise/README.md` → ≥1
- `ls docs/wise/dispatcher.md` → No such file; `grep -rn 'dispatcher.md' README.md plugins/wise docs/` → no matches
- `python3 scripts/validate_repo.py` (if PLAN-002 landed) → exits 0

## Stop conditions
- If the README tables have been restructured at HEAD (row format
  changed), match the NEW format instead of the excerpts here; if the
  tables are gone entirely, STOP and report.
- If docs/wise/ has been relocated (PLAN-010 moves the workflows
  authoring doc), re-verify inbound link targets before the rename; on
  conflict STOP.
- Doc-only plan: touch no file outside README.md, plugins/wise/README.md,
  docs/wise/. (Version badge reads plugin.json but never edits it — no
  version bump for doc-only changes per CONTRIBUTING §9.)
