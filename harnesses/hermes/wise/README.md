# wise — Hermes Agent port

The `wise` copilot ported to
[Nous Research Hermes Agent](https://github.com/NousResearch/hermes-agent)
as an Agent Skills pack. Flat `/wise-*` skills, the workflow engine, and
the SDLC agent roster, adapted from the canonical Claude Code plugin
([github.com/e1024kb/wise-claude](https://github.com/e1024kb/wise-claude)).

This folder is **hand-maintained** and vendors its shared assets
(`references/`, `agents/`, `workflows/`, `scripts/`) from the repo's
`core/`. See the root `CONTRIBUTING.md` §10 for the sync model.

## Install

Hermes discovers skills under `~/.hermes/skills/`. Copy them in:

```
cp -R harnesses/hermes/wise/skills/* ~/.hermes/skills/
```

…or use the repo's universal installer: `./install.sh hermes` (or
`just install hermes`). Then `/skills` lists them and `/<skill-name>`
invokes one.

## Prerequisites

- **git** and, for the PR skills, the authenticated **`gh` CLI**.
- For the **workflow engine** (`/wise-workflow-*`): **Python 3** with
  `pyyaml` and `python-ulid`
  (`pip install pyyaml python-ulid typing_extensions`).
- **`WISE_PLUGIN_ROOT`** — skills and workflows reference shared files as
  `${WISE_PLUGIN_ROOT}/references/…` etc. (the harness-neutral equivalent
  of Claude's `CLAUDE_PLUGIN_ROOT`). Export it to wherever you copied this
  pack's shared dirs. The universal installer sets this for you.
  Persistent state defaults to `~/.local/share/wise` (override with
  `WISE_DATA_DIR`).

## What works here

The full matrix (with per-skill reasons) is in
[`docs/compatibility.md`](../../../docs/compatibility.md).

- **Full (11)** — pure prose + `git`/`gh`: the commit trio, the PR
  create/reviewer skills, `wise-estimation`, `wise-feedback`, and the
  PRD/TRD authors.
- **Adapted (14)** — the same logic with a **Harness adaptation note**
  mapping Claude-specific tools to Hermes equivalents. Frontmatter is
  reduced to `name` + `description` (the widely-supported Agent Skills
  core); everything wise-specific lives in the body.
- **Claude-only (7, not shipped)** — `wise-supervise`, the three
  `wise-insights-*` skills, `wise-skills-create` / `wise-skills-edit`,
  and `wise-init`.

## Workflows

The workflow engine (`scripts/workflows.py`) runs unchanged. Hermes runs
isolated subagents in parallel, so the conductor (`/wise-workflow-run`)
dispatches `prompt` steps as subagents and team steps as **parallel
subagents** — the closest match to Claude's execution model of the three
ports. See the conductor skill's execution note.
