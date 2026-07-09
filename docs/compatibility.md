# Cross-harness compatibility matrix

How completely each `wise` skill ports to each harness. Generated from the
committed ports; see each `harnesses/<harness>/wise/README.md` for install
steps and `CONTRIBUTING.md` §10 for the maintenance model.

Legend: **✓ full** — ships as-is (pure prose + git/gh; on non-Claude ports
this still means shared-file paths rewritten to `${WISE_PLUGIN_ROOT}` with
its baked default, plus a one-line shared-file-resolution note). **~
adapted** — ships with a per-harness *Harness adaptation note* mapping
Claude-specific tools (subagent dispatch, AskUserQuestion, the Skill tool,
TodoWrite) to the harness's equivalents. **✗ —** — not shipped (Claude Code
only).

| Skill | Claude | Codex | Cursor | Hermes |
|---|:--:|:--:|:--:|:--:|
| `wise` | ✓ full | ~ adapted | ~ adapted | ~ adapted |
| `wise-code-review-auto` | ✓ full | ~ adapted | ~ adapted | ~ adapted |
| `wise-commit` | ✓ full | ✓ full | ✓ full | ✓ full |
| `wise-commit-message` | ✓ full | ✓ full | ✓ full | ✓ full |
| `wise-commit-push` | ✓ full | ✓ full | ✓ full | ✓ full |
| `wise-estimation` | ✓ full | ✓ full | ✓ full | ✓ full |
| `wise-feedback` | ✓ full | ✓ full | ✓ full | ✓ full |
| `wise-grill` | ✓ full | ~ adapted | ~ adapted | ~ adapted |
| `wise-implement-plan-auto` | ✓ full | ~ adapted | ~ adapted | ~ adapted |
| `wise-init` | ✓ full | ~ adapted | ~ adapted | ~ adapted |
| `wise-insights-mine` | ✓ full | ✗ — | ✗ — | ✗ — |
| `wise-insights-refine` | ✓ full | ✗ — | ✗ — | ✗ — |
| `wise-insights-reset` | ✓ full | ✗ — | ✗ — | ✗ — |
| `wise-pr-add-reviewers` | ✓ full | ✓ full | ✓ full | ✓ full |
| `wise-pr-create` | ✓ full | ✓ full | ✓ full | ✓ full |
| `wise-pr-create-auto` | ✓ full | ✓ full | ✓ full | ✓ full |
| `wise-pr-request-review-auto` | ✓ full | ✓ full | ✓ full | ✓ full |
| `wise-pr-watch` | ✓ full | ~ adapted | ~ adapted | ~ adapted |
| `wise-pr-watch-auto` | ✓ full | ~ adapted | ~ adapted | ~ adapted |
| `wise-prd-architect` | ✓ full | ✓ full | ✓ full | ✓ full |
| `wise-revise` | ✓ full | ~ adapted | ~ adapted | ~ adapted |
| `wise-simplify-auto` | ✓ full | ~ adapted | ~ adapted | ~ adapted |
| `wise-skills-create` | ✓ full | ✗ — | ✗ — | ✗ — |
| `wise-skills-edit` | ✓ full | ✗ — | ✗ — | ✗ — |
| `wise-supervise` | ✓ full | ✗ — | ✗ — | ✗ — |
| `wise-trd-architect` | ✓ full | ✓ full | ✓ full | ✓ full |
| `wise-workflow-create` | ✓ full | ~ adapted | ~ adapted | ~ adapted |
| `wise-workflow-list` | ✓ full | ~ adapted | ~ adapted | ~ adapted |
| `wise-workflow-remove` | ✓ full | ~ adapted | ~ adapted | ~ adapted |
| `wise-workflow-resume` | ✓ full | ~ adapted | ~ adapted | ~ adapted |
| `wise-workflow-run` | ✓ full | ~ adapted | ~ adapted | ~ adapted |
| `wise-workflow-status` | ✓ full | ~ adapted | ~ adapted | ~ adapted |

## Claude-only skills — why

- **`wise-insights-mine`** — self-improvement loop needs the SessionEnd hook + Claude transcript format.
- **`wise-insights-refine`** — operates on the Claude insights store.
- **`wise-insights-reset`** — operates on the Claude insights store.
- **`wise-skills-create`** — delegates to Claude's skill-creator.
- **`wise-skills-edit`** — delegates to Claude's skill-creator.
- **`wise-supervise`** — background-team watchdog (TeamCreate / Monitor / heartbeats).

## Workflows

All four bundled workflows (`ticket-auto`, `ticket-plan`, `impl-plan-auto`,
`example-workflow`) ship to every port. The workflow **engine**
(`scripts/workflows.py`) runs unchanged; the **conductor**
(`/wise-workflow-run` / `-resume`) carries a per-harness execution note
mapping each step type to that harness's primitives. Parallelism varies:
Claude and Hermes run team steps as parallel subagents; Codex uses subagents
where available; Cursor runs them sequentially (same result, longer
wall-clock). The self-improvement loop (SessionEnd hook + insights) is
**Claude Code only**.

