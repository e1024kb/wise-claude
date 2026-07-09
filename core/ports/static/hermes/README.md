# wise — Hermes Agent port

The `wise` copilot ported to
[Nous Research Hermes Agent](https://github.com/NousResearch/hermes-agent)
as an Agent Skills pack. Flat `/wise-*` skills, the workflow engine, and
the SDLC agent roster, adapted from the canonical Claude Code plugin
([github.com/e1024kb/wise-claude](https://github.com/e1024kb/wise-claude)).

This folder is **generated** by `scripts/build_ports.py` from the repo's
`core/`, the Claude port's skills, and the inputs under `core/ports/` —
don't edit it by hand. See the root `CONTRIBUTING.md` §10.

## Install

```bash
git clone https://github.com/e1024kb/wise-claude && cd wise-claude
./install.sh hermes        # or: just install hermes
```

The installer does two things, and a working install needs both:

1. copies the skills into `~/.hermes/skills/` (Hermes's discovery dir) —
   `/skills` lists them and `/<skill-name>` invokes one;
2. lays the **whole intact pack** (`references/`, `agents/`,
   `workflows/`, `scripts/`, `skills/`) at
   `~/.local/share/wise/harness/hermes` — the shared root every skill
   resolves by default when it reads shared files or invokes the
   workflow engine.

It finishes with a read-only dependency probe; run the `wise-init` skill
inside Hermes to finish any missing setup.

### Manual install

The same two steps by hand:

```bash
cp -R harnesses/hermes/wise/skills/* ~/.hermes/skills/
mkdir -p ~/.local/share/wise/harness/hermes
cp -R harnesses/hermes/wise/references harnesses/hermes/wise/agents \
      harnesses/hermes/wise/workflows harnesses/hermes/wise/scripts \
      harnesses/hermes/wise/skills ~/.local/share/wise/harness/hermes/
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
  `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/hermes`
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
  mapping Claude-specific tools to Hermes equivalents, including
  `wise-init` (the guided dependency-setup wizard). Frontmatter is
  reduced to `name` + `description` (the widely-supported Agent Skills
  core); everything wise-specific lives in the body.
- **Claude-only (6, not shipped)** — `wise-supervise`, the three
  `wise-insights-*` skills, and `wise-skills-create` / `wise-skills-edit`.

## Workflows

The workflow engine (`scripts/workflows.py`) runs unchanged and
self-locates its bundled workflows and agent roster — no environment
variables required after `./install.sh hermes`. Hermes runs isolated
subagents in parallel, so the conductor (`/wise-workflow-run`)
dispatches `prompt` steps as subagents and team steps as **parallel
subagents** — the closest match to Claude's execution model of the three
ports. See the conductor skill's execution note.
