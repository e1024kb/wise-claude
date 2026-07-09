# wise — OpenAI Codex CLI port

The `wise` copilot ported to [OpenAI Codex CLI](https://developers.openai.com/codex/).
Flat `/wise-*` skills, the workflow engine, and the SDLC agent roster,
adapted from the canonical Claude Code plugin
([github.com/e1024kb/wise-claude](https://github.com/e1024kb/wise-claude)).

This folder is **hand-maintained** and vendors its shared assets
(`references/`, `agents/`, `workflows/`, `scripts/`) from the repo's
`core/`. See the root `CONTRIBUTING.md` §10 for the sync model.

## Install

Canonical (Codex plugin marketplace):

```
codex plugin marketplace add e1024kb/wise-claude
codex plugin install wise
```

Codex reads the marketplace catalog at the repo root
(`.agents/plugins/marketplace.json`), which points at this folder.

Plain Agent Skills fallback (if you don't use the plugin system) — copy
the skills into a discovery path:

```
cp -R harnesses/codex/wise/skills/* ~/.agents/skills/
```

…or use the repo's universal installer: `./install.sh codex` (or
`just install codex`).

## Prerequisites

- **git** and, for the PR skills, the authenticated **`gh` CLI**.
- For the **workflow engine** (`/wise-workflow-*`): **Python 3** with
  `pyyaml` and `python-ulid`
  (`pip install pyyaml python-ulid typing_extensions`).
- **`WISE_PLUGIN_ROOT`** — skills and workflows reference shared files as
  `${WISE_PLUGIN_ROOT}/references/…`, `${WISE_PLUGIN_ROOT}/agents/…`,
  etc. (the harness-neutral equivalent of Claude's `CLAUDE_PLUGIN_ROOT`).
  Export it to this pack's install directory, e.g.:
  ```
  export WISE_PLUGIN_ROOT="$HOME/.codex/plugins/cache/wise-claude/wise/<version>"
  ```
  The universal installer sets this for you. Persistent per-user state
  (workflow runs, definitions) defaults to `~/.local/share/wise`
  (override with `WISE_DATA_DIR`).

## What works here

Skills are grouped by how completely they port. The full matrix (with
per-skill reasons) is in [`docs/compatibility.md`](../../../docs/compatibility.md).

- **Full** (11) — pure prose + `git`/`gh`: the commit trio, the PR
  create/reviewer skills, `wise-estimation`, `wise-feedback`, and the
  PRD/TRD authors.
- **Adapted** (14) — the same logic, with a **Harness adaptation note**
  at the top of each skill mapping Claude-specific tools (subagent
  dispatch, `AskUserQuestion`, the `Skill` tool, `TodoWrite`) to Codex
  equivalents: `wise-grill`, `wise-revise`, the PR-watch pair, the two
  quality passes, `wise-implement-plan-auto`, the `/wise` helper, and the
  six `wise-workflow-*` skills.
- **Claude-only** (7, not shipped here) — `wise-supervise`, the three
  `wise-insights-*` skills (need Claude's SessionEnd hook + transcript
  format), `wise-skills-create` / `wise-skills-edit` (delegate to
  Claude's `skill-creator`), and `wise-init` (its dep probes are
  replaced by the Prerequisites above).

## Workflows

The workflow engine (`scripts/workflows.py`) runs unchanged. The
conductor skills (`/wise-workflow-run`, `/wise-workflow-resume`) carry a
per-harness execution note describing how each workflow step type maps to
Codex primitives (subagents where available, otherwise sequential
in-context role adoption; plain-chat questions for `ask`/`approval`
steps). Model / effort step hints are advisory on Codex.
