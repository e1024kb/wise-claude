# PLAN-010 — Ship the workflow-authoring reference inside the plugin

## Source
- Scope: docs/wise/workflows.md, plugins/wise/skills/wise-workflow-create/SKILL.md
- Found by: docs lens · leverage 0.75 (impact 3 ÷ effort 3 × confidence 0.75)
- Commit: e9971c5
- Evidence: plugins/wise/skills/wise-workflow-create/SKILL.md:381 (points at `docs/wise/workflows.md`); repo layout — docs/wise/ is a sibling of plugins/, so it is NOT packaged under `${CLAUDE_PLUGIN_ROOT}` in a marketplace install

## Summary
The workflow-authoring schema reference (docs/wise/workflows.md — the
doc that defines workflow.yaml fields, step types, trigger rules,
preflight keys) lives outside `plugins/wise/`, so users who installed
the plugin from the marketplace (rather than cloning this repo) cannot
reach it: the wise-workflow-create skill points at a repo-relative path
that does not exist in `${CLAUDE_PLUGIN_ROOT}`. Done = the authoring
reference ships with the plugin and every skill pointer resolves in an
installed context.

## Assumptions
- Marketplace installs copy only the plugin `source` directory
  (`./plugins/wise`) into the cache — files outside it are absent at
  runtime. (Confidence medium: verify by inspecting an actual installed
  cache, e.g. `~/.claude/plugins/cache/wise-claude/wise/<version>/` —
  the executor machine has one. If docs/ IS present in the cache,
  this plan's premise fails → STOP condition.)
- Only workflows.md among docs/wise/* is needed at plugin runtime
  (dispatcher/insights/skills-authoring docs are contributor-facing).
  Verify: grep plugins/wise/ for `docs/wise/` references to enumerate
  runtime pointers.

## Decisions Made
- MOVE (not copy) docs/wise/workflows.md →
  plugins/wise/references/workflow-authoring.md; leave
  docs/wise/workflows.md as a 3-line stub pointing at the new path
  (avoids two full copies drifting; repo readers follow one hop).
  Rationale: references/ already holds skill-consumed prose; a copy
  would violate the repo's single-source-of-truth doc rule
  (plugins/wise/CLAUDE.md).
- wise-workflow-create/SKILL.md:381 (and any other runtime pointer
  found by the grep) switches to
  `${CLAUDE_PLUGIN_ROOT}/references/workflow-authoring.md`.
- Contributor-facing links (README, CONTRIBUTING, other docs/wise files)
  update to the new in-plugin path directly (no stub-hop for docs that
  render on GitHub).
- Content is NOT edited (the docs lens spot-checked it in sync with
  workflows.py — effort enum, preflight keys, trigger rules, step types
  all match at e9971c5).

## Current state
plugins/wise/skills/wise-workflow-create/SKILL.md:381 references
`docs/wise/workflows.md`. Repo layout:

```
wise-claude/
├── docs/wise/workflows.md      ← authoring schema reference (repo-only)
└── plugins/wise/               ← the only directory a marketplace install ships
    └── references/             ← skill-consumed shared prose (packaged)
```

## Tasks
Wave 1
- [ ] Verify the install-layout assumption against the local plugin
      cache; grep plugins/wise/ for every `docs/wise/` reference —
      inventory runtime vs contributor pointers — Reuse: local cache at
      ~/.claude/plugins/cache/wise-claude/wise/ — 0.5 SP

Wave 2
- [ ] `git mv docs/wise/workflows.md plugins/wise/references/workflow-authoring.md`;
      write the stub at the old path; update
      wise-workflow-create/SKILL.md:381 + all inventoried pointers
      (README tables, CONTRIBUTING, docs/wise cross-links) — Reuse:
      Wave 1 inventory — 1.5 SP
- [ ] Bump plugin.json patch version (packaged content changed) —
      Reuse: CONTRIBUTING §9 — 0.5 SP

Total: 3 SP (rounded)

## Testing
Link-integrity: PLAN-002's `scripts/validate_repo.py` cross-reference
check must pass (it asserts `${CLAUDE_PLUGIN_ROOT}/...` references
exist). If not landed, manual grep + `ls` per Validation.

## Validation
- `ls plugins/wise/references/workflow-authoring.md` → exists
- `cat docs/wise/workflows.md` → ≤5-line stub pointing at the new path
- `grep -rn 'docs/wise/workflows.md' plugins/wise/` → no matches
- `grep -n 'workflow-authoring.md' plugins/wise/skills/wise-workflow-create/SKILL.md` → ≥1
- `python3 scripts/validate_repo.py` (if present) → exits 0

## Stop conditions
- If Wave 1 shows marketplace installs DO ship docs/ (premise false),
  STOP and report — no move needed, close as invalid.
- If wise-workflow-create/SKILL.md at HEAD no longer references the doc
  (already fixed), STOP and report.
- If PLAN-009 is mid-flight (it edits docs/wise link targets),
  sequence after it.
