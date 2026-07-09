# wise — Cursor port

The `wise` copilot ported to [Cursor](https://cursor.com/docs/context/skills)
as an Agent Skills pack. Flat `/wise-*` skills, the workflow engine, and
the SDLC agent roster, adapted from the canonical Claude Code plugin
([github.com/e1024kb/wise-claude](https://github.com/e1024kb/wise-claude)).

This folder is **hand-maintained** and vendors its shared assets
(`references/`, `agents/`, `workflows/`, `scripts/`) from the repo's
`core/`. See the root `CONTRIBUTING.md` §10 for the sync model.

## Install

Cursor discovers Agent Skills under `.cursor/skills/` and `.agents/skills/`
(project) or `~/.cursor/skills/` and `~/.agents/skills/` (user). Copy the
skills into one of those:

```
cp -R harnesses/cursor/wise/skills/* ~/.cursor/skills/     # user-wide
# or, per project:
cp -R harnesses/cursor/wise/skills/* <project>/.agents/skills/
```

…or use the repo's universal installer: `./install.sh cursor` (or
`just install cursor`).

Cursor also reads legacy `.claude/skills/` and `.codex/skills/` paths
directly — but install **one** copy only to avoid duplicate skills in the
`/` menu.

## Prerequisites

- **git** and, for the PR skills, the authenticated **`gh` CLI**.
- For the **workflow engine** (`/wise-workflow-*`): **Python 3** with
  `pyyaml` and `python-ulid`
  (`pip install pyyaml python-ulid typing_extensions`).
- **`WISE_PLUGIN_ROOT`** — skills and workflows reference shared files as
  `${WISE_PLUGIN_ROOT}/references/…` etc. (the harness-neutral equivalent
  of Claude's `CLAUDE_PLUGIN_ROOT`). Export it to wherever you copied this
  pack, e.g. `export WISE_PLUGIN_ROOT="$HOME/.cursor/skills/.wise"` if you
  keep the shared dirs alongside the skills. The universal installer sets
  this for you. Persistent state defaults to `~/.local/share/wise`
  (override with `WISE_DATA_DIR`).

## What works here

The full matrix (with per-skill reasons) is in
[`docs/compatibility.md`](../../../docs/compatibility.md).

- **Full (11)** — pure prose + `git`/`gh`: the commit trio, the PR
  create/reviewer skills, `wise-estimation`, `wise-feedback`, and the
  PRD/TRD authors.
- **Adapted (14)** — the same logic with a **Harness adaptation note**
  mapping Claude-specific tools to Cursor equivalents. `disable-model-
  invocation` is preserved on the `/wise` helper (a Cursor-native
  frontmatter field), so Cursor never auto-invokes it.
- **Claude-only (7, not shipped)** — `wise-supervise`, the three
  `wise-insights-*` skills, `wise-skills-create` / `wise-skills-edit`,
  and `wise-init`.

## Workflows

The workflow engine (`scripts/workflows.py`) runs unchanged. Because
Cursor has no first-class subagent primitive, the conductor
(`/wise-workflow-run`) runs `prompt` steps by adopting the role
in-context and team steps **sequentially** — the result matches Claude,
the wall-clock is longer. See the conductor skill's execution note.
