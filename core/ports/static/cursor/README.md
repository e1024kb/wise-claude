# wise — Cursor port

The `wise` copilot ported to [Cursor](https://cursor.com/docs/context/skills)
as an Agent Skills pack. Flat `/wise-*` skills, the workflow engine, and
the SDLC agent roster, adapted from the canonical Claude Code plugin
([github.com/e1024kb/wise-claude](https://github.com/e1024kb/wise-claude)).

This folder is **generated** by `scripts/build_ports.py` from the repo's
`core/`, the Claude port's skills, and the inputs under `core/ports/` —
don't edit it by hand. See the root `CONTRIBUTING.md` §10.

## Install

```bash
git clone https://github.com/e1024kb/wise-claude && cd wise-claude
./install.sh cursor        # or: just install cursor
```

The installer does two things, and a working install needs both:

1. copies the skills into `~/.cursor/skills/` (Cursor's user-wide
   discovery dir);
2. lays the **whole intact pack** (`references/`, `agents/`,
   `workflows/`, `scripts/`, `skills/`) at
   `~/.local/share/wise/harness/cursor` — the shared root every skill
   resolves by default when it reads shared files or invokes the
   workflow engine.

It finishes with a read-only dependency probe; run the `wise-init` skill
inside Cursor to finish any missing setup.

Cursor also reads project `.cursor/skills/` / `.agents/skills/` and
legacy `.claude/skills/` / `.codex/skills/` paths — install **one** copy
only to avoid duplicate skills in the `/` menu (per-project:
`./install.sh cursor --project <dir>`).

### Manual install

The same two steps by hand:

```bash
cp -R harnesses/cursor/wise/skills/* ~/.cursor/skills/
mkdir -p ~/.local/share/wise/harness/cursor
cp -R harnesses/cursor/wise/references harnesses/cursor/wise/agents \
      harnesses/cursor/wise/workflows harnesses/cursor/wise/scripts \
      harnesses/cursor/wise/skills ~/.local/share/wise/harness/cursor/
```

Skipping the second command leaves only the four self-contained skills
(`wise-estimation`, `wise-feedback`, `wise-prd-architect`,
`wise-trd-architect`) working — everything else reads shared files or
the workflow engine from the shared root.

## Prerequisites

- **git** and, for the PR skills, the authenticated **`gh` CLI**.
- For the **workflow engine** (`/wise-workflow-*`): **Python 3** with
  `pyyaml` and `python-ulid`
  (`pip install pyyaml python-ulid typing_extensions`). The `wise-init`
  skill probes and guides all of this.
- **`WISE_PLUGIN_ROOT` is optional.** Skills resolve shared files from
  `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/cursor`
  by default — exactly where the installer puts the pack. Export
  `WISE_PLUGIN_ROOT` only to point them somewhere else. Persistent state
  (workflow runs, registries) defaults to `~/.local/share/wise`
  (override with `WISE_DATA_DIR`).

## What works here

The full matrix (with per-skill reasons) is in
[`docs/compatibility.md`](../../../docs/compatibility.md).

- **Full (11)** — pure prose + `git`/`gh`: the commit trio, the PR
  create/reviewer skills, `wise-estimation`, `wise-feedback`, and the
  PRD/TRD authors. (Beyond the four self-contained ones, these read
  shared `references/` — the shared-root default covers that.)
- **Adapted (15)** — the same logic with a **Harness adaptation note**
  mapping Claude-specific tools to Cursor equivalents, including
  `wise-init` (the guided dependency-setup wizard). `disable-model-
  invocation` is preserved on the `/wise` helper (a Cursor-native
  frontmatter field), so Cursor never auto-invokes it.
- **Claude-only (6, not shipped)** — `wise-supervise`, the three
  `wise-insights-*` skills, and `wise-skills-create` / `wise-skills-edit`.

## Workflows

The workflow engine (`scripts/workflows.py`) runs unchanged and
self-locates its bundled workflows and agent roster — no environment
variables required after `./install.sh cursor`. Because Cursor has no
first-class subagent primitive, the conductor (`/wise-workflow-run`)
runs `prompt` steps by adopting the role in-context and team steps
**sequentially** — the result matches Claude, the wall-clock is longer.
See the conductor skill's execution note.
