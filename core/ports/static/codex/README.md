# wise — OpenAI Codex CLI port

The `wise` copilot ported to [OpenAI Codex CLI](https://developers.openai.com/codex/).
Flat `/wise-*` skills, the workflow engine, and the SDLC agent roster,
adapted from the canonical Claude Code plugin
([github.com/e1024kb/wise-claude](https://github.com/e1024kb/wise-claude)).

This folder is **generated** by `scripts/build_ports.py` from the repo's
`core/`, the Claude port's skills, and the inputs under `core/ports/` —
don't edit it by hand. See the root `CONTRIBUTING.md` §10.

## Install

```bash
git clone https://github.com/e1024kb/wise-claude && cd wise-claude
./install.sh codex        # or: just install codex
```

The installer prefers the Codex plugin marketplace when the `codex` CLI
is available (`codex plugin marketplace add` + `codex plugin install
wise`, reading the catalog at the repo root
`.agents/plugins/marketplace.json`) and falls back to a plain skills
copy into `~/.agents/skills/`. Either way it also lays the **whole
intact pack** (`references/`, `agents/`, `workflows/`, `scripts/`,
`skills/`) at `~/.local/share/wise/harness/codex` — the shared root
every skill resolves by default when it reads shared files or invokes
the workflow engine.

It finishes with a read-only dependency probe; run the `wise-init` skill
inside Codex to finish any missing setup.

### Manual install

The same two steps by hand:

```bash
cp -R harnesses/codex/wise/skills/* ~/.agents/skills/
mkdir -p ~/.local/share/wise/harness/codex
cp -R harnesses/codex/wise/references harnesses/codex/wise/agents \
      harnesses/codex/wise/workflows harnesses/codex/wise/scripts \
      harnesses/codex/wise/skills ~/.local/share/wise/harness/codex/
```

Skipping the second command leaves only the four self-contained skills
(`wise-estimation`, `wise-feedback`, `wise-prd-architect`,
`wise-trd-architect`) working — everything else reads shared files or
the workflow engine from the shared root.

A marketplace-only install (`codex plugin install wise` without the
installer) puts the pack in a **versioned cache dir** that moves on
every update — you can point skills at it with
`export WISE_PLUGIN_ROOT="$HOME/.codex/plugins/cache/wise-claude/wise/<version>"`,
but the stable shared root above is the supported path.

## Prerequisites

- **git** and, for the PR skills, the authenticated **`gh` CLI**.
- For the **workflow engine** (`/wise-workflow-*`): **Python 3** with
  `pyyaml` and `python-ulid`
  (`pip install pyyaml python-ulid typing_extensions`). The `wise-init`
  skill probes and guides all of this.
- **`WISE_PLUGIN_ROOT` is optional.** Skills resolve shared files from
  `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/codex`
  by default — exactly where the installer puts the pack. Export
  `WISE_PLUGIN_ROOT` only to point them somewhere else. Persistent
  per-user state (workflow runs, definitions) defaults to
  `~/.local/share/wise` (override with `WISE_DATA_DIR`).

## What works here

Skills are grouped by how completely they port. The full matrix (with
per-skill reasons) is in [`docs/compatibility.md`](../../../docs/compatibility.md).

- **Full** (11) — pure prose + `git`/`gh`: the commit trio, the PR
  create/reviewer skills, `wise-estimation`, `wise-feedback`, and the
  PRD/TRD authors. (Beyond the four self-contained ones, these read
  shared `references/` — the shared-root default covers that.)
- **Adapted** (15) — the same logic, with a **Harness adaptation note**
  at the top of each skill mapping Claude-specific tools (subagent
  dispatch, `AskUserQuestion`, the `Skill` tool, `TodoWrite`) to Codex
  equivalents: `wise-grill`, `wise-revise`, the PR-watch pair, the two
  quality passes, `wise-implement-plan-auto`, the `/wise` helper,
  `wise-init` (the guided dependency-setup wizard), and the six
  `wise-workflow-*` skills.
- **Claude-only** (6, not shipped here) — `wise-supervise`, the three
  `wise-insights-*` skills (need Claude's SessionEnd hook + transcript
  format), and `wise-skills-create` / `wise-skills-edit` (delegate to
  Claude's `skill-creator`).

## Workflows

The workflow engine (`scripts/workflows.py`) runs unchanged and
self-locates its bundled workflows and agent roster — no environment
variables required after `./install.sh codex`. The conductor skills
(`/wise-workflow-run`, `/wise-workflow-resume`) carry a per-harness
execution note describing how each workflow step type maps to Codex
primitives (subagents where available, otherwise sequential in-context
role adoption; plain-chat questions for `ask`/`approval` steps).
Model / effort step hints are advisory on Codex.
