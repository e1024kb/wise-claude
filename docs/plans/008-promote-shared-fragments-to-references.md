# PLAN-008 — Promote cross-skill shared fragments out of `workflows/ticket-auto/prompts/`

## Source
- Scope: plugins/wise/workflows/ticket-auto/prompts/, plugins/wise/skills/*/SKILL.md, plugins/wise/workflows/impl-plan-auto/prompts/process-plans.md, plugins/wise/skills/wise-implement-plan-auto/agents/executor.md
- Found by: debt lens (findings 1 + 11 deduped: fragment placement + bidirectional skill↔workflow coupling) · leverage 1.0 (impact 3 ÷ effort 3 × confidence 1.0)
- Commit: e9971c5
- Evidence: skills/wise-code-review-auto/SKILL.md:54, skills/wise-implement-plan-auto/SKILL.md:49, skills/wise-pr-create-auto/SKILL.md:47, skills/wise-pr-request-review-auto/SKILL.md:48, skills/wise-pr-watch-auto/SKILL.md:61, workflows/impl-plan-auto/prompts/process-plans.md:274,297,350,359,372, workflows/ticket-auto/prompts/implement-plan.md:52 (reads skills/wise-implement-plan-auto/agents/executor.md); plugins/wise/CLAUDE.md invariant "Cross-skill shared prose lives in plugins/wise/references/"

## Summary
Five standalone skills and the impl-plan-auto workflow hard-code
`${CLAUDE_PLUGIN_ROOT}/workflows/ticket-auto/prompts/*.md` as their
source of truth, and ticket-auto's implement-plan.md reaches into a
skill's private `agents/executor.md` — a three-way coupling across two
workflows and a skill dir, violating the repo's own placement invariant
("cross-skill shared prose lives in references/"). The 2.x rename habit
(PR #18 renamed a workflow) makes this a live breakage risk: renaming or
removing ticket-auto would silently break 5 skills. Done = shared
fragments live under `references/`, all consumers point there, layering
is one-directional (workflows/skills → references; never sideways).

## Assumptions
- Files moving to `plugins/wise/references/pipeline/` (new subdir,
  mirroring the existing `references/pr/` convention):
  implement-plan.md, review-branch-auto.md, ensure-pr-auto.md,
  request-review-auto.md, watch-pipelines-auto.md,
  handle-bot-reviews-auto.md, handle-sonar-issues-auto.md, and
  executor.md (from skills/wise-implement-plan-auto/agents/).
  plan-ticket.md and process-tickets.md stay (ticket-auto-only);
  process-plans.md stays (impl-plan-auto-only). Verify single-consumer
  status by grep before deciding each file's fate.
- `git mv` preserves history; all path references are updatable by
  exact-string search (`workflows/ticket-auto/prompts/`,
  `skills/wise-implement-plan-auto/agents/executor.md`).
- CLAUDE.md's mirror invariant requires workflow READMEs and
  plugins/wise/CLAUDE.md's own file-map prose to be updated in the same
  commit.

## Decisions Made
- Destination `references/pipeline/` (not flat `references/`) — the pr/
  subdir precedent groups fragment families; these are the autonomous
  pipeline family.
- Consumers keep reading fragments via
  `${CLAUDE_PLUGIN_ROOT}/references/pipeline/<file>` — same include
  mechanism, only the path changes. No content edits beyond
  intra-fragment relative links (grep each moved file for
  `workflows/ticket-auto` self-references).
- handle-bot-reviews-auto.md and handle-sonar-issues-auto.md move too
  (consumed by watch-pipelines-auto.md which moves): keep the family
  together so no reference/ file points back into a workflow dir.
- ticket-auto/workflow.yaml prompt-step paths that referenced moved
  files switch from `{{workflow.dir}}/prompts/...` to
  `${CLAUDE_PLUGIN_ROOT}/references/pipeline/...` — confirm the engine
  renders `${CLAUDE_PLUGIN_ROOT}` in prompt paths (check how
  workflow.yaml references prompts today and how `_render_step`
  workflows.py:694-734 substitutes; if only `{{workflow.dir}}` is
  supported for step prompt paths, add no engine feature — instead
  leave per-workflow thin pointer files in ticket-auto/prompts/ that
  contain a single "Read `${CLAUDE_PLUGIN_ROOT}/references/pipeline/X`"
  line. Prefer the mechanism that needs NO workflows.py change.)
- If PLAN-003 is in flight, land it first — it edits the same fragments;
  this plan is a pure move + path rewrite on top.

## Current state
Skill include lines (5 sites), e.g. skills/wise-pr-watch-auto/SKILL.md:61:
```
Read `${CLAUDE_PLUGIN_ROOT}/workflows/ticket-auto/prompts/watch-pipelines-auto.md`
```
Cross-workflow reads, process-plans.md:274-372 (5 sites), e.g.:
```
`${CLAUDE_PLUGIN_ROOT}/workflows/ticket-auto/prompts/implement-plan.md`
```
Skill-private file consumed by a workflow,
workflows/ticket-auto/prompts/implement-plan.md:52 →
`skills/wise-implement-plan-auto/agents/executor.md`.

## Tasks
Wave 1
- [ ] Grep-inventory every consumer of each ticket-auto/prompts/*.md and
      of executor.md (skills, workflow.yaml files, prompts, READMEs,
      CLAUDE.md, docs/wise/) — output the full move list with consumer
      counts before moving anything — Reuse: grep — 1 SP

Wave 2
- [ ] `git mv` the multi-consumer fragments to
      plugins/wise/references/pipeline/ (+ executor.md); update ALL
      referencing paths (skills, both workflows' prompts + workflow.yaml,
      READMEs, plugins/wise/CLAUDE.md file map, docs/wise/) — Reuse:
      the inventory from Wave 1 — 2 SP

Wave 3
- [ ] Verify + version bump: full-repo grep proves zero references to
      old paths; bump plugin.json minor; update workflow READMEs per
      the CLAUDE.md sync invariant — Reuse: CONTRIBUTING §9 release
      steps — 1 SP

Total: 3 SP (rounded)

## Testing
Path-integrity only (no behaviour change intended):
`scripts/validate_repo.py` (PLAN-002) cross-reference check is the
regression net — it asserts every `${CLAUDE_PLUGIN_ROOT}/...` and
`{{workflow.dir}}/prompts/...` reference resolves. If PLAN-002 has not
landed, run the equivalent one-off grep loop and file-existence check.

## Validation
- `grep -rn 'workflows/ticket-auto/prompts' plugins/wise/skills plugins/wise/workflows/impl-plan-auto` → no matches
- `grep -rn 'skills/wise-implement-plan-auto/agents' plugins/wise/workflows` → no matches
- `python3 scripts/validate_repo.py` (if present) → exits 0
- `ls plugins/wise/references/pipeline/` → the moved fragment set
- Smoke: `/wise-workflow-run ticket-auto` dry preflight — `python3 plugins/wise/scripts/workflows.py locate-def ticket-auto` → resolves; workflow.yaml parses

## Stop conditions
- If the engine cannot reference prompt files outside
  `{{workflow.dir}}` AND the thin-pointer fallback is judged to violate
  the "fragments are source of truth" doc rule, STOP and report the
  options (engine feature vs pointer files) instead of changing
  workflows.py here.
- If PLAN-003 is mid-flight on the same files, STOP until it lands.
- Do not merge/dedupe fragment CONTENT (interactive vs -auto forks) —
  that is the index's noted item, not this plan.
