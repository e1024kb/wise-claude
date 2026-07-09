# wise — opencode port

The `wise` copilot ported to [opencode](https://opencode.ai/docs) as an
Agent Skills pack. Flat `wise-*` skills, the workflow engine, and the
SDLC agent roster, adapted from the canonical Claude Code plugin
([github.com/e1024kb/wise-claude](https://github.com/e1024kb/wise-claude)) —
plus two opencode-native extras: thin `/wise-<action>` command wrappers
and real subagent role cards.

This folder is **generated** by `scripts/build_ports.py` from the repo's
`core/`, the Claude port's skills, and the inputs under `core/ports/` —
don't edit it by hand. See the root `CONTRIBUTING.md` §10.

## Install

opencode has no plugin marketplace for skill packs, so the install is
the repo's universal installer (or the manual copies below):

```bash
git clone https://github.com/e1024kb/wise-claude && cd wise-claude
./install.sh opencode        # or: just install opencode
```

The installer lays down four copies:

1. the skills into `~/.config/opencode/skills/` (opencode's global
   discovery dir) — opencode loads them on demand via its native
   `skill` tool, triggered by each skill's description;
2. the command wrappers into `~/.config/opencode/commands/` — one thin
   `/wise-<action>` slash command per user-invocable skill, so the
   slash UX matches the other harnesses;
3. the role cards into `~/.config/opencode/agents/` as
   `wise-<role>.md` — real opencode subagents (`mode: subagent`) that
   the workflow conductor dispatches via the Task tool or `@wise-<role>`
   mentions;
4. the **whole intact pack** (`references/`, `agents/`, `workflows/`,
   `scripts/`, `skills/`, `commands/`) at
   `~/.local/share/wise/harness/opencode` —
   the shared root every skill resolves by default when it reads shared
   files or invokes the workflow engine.

Steps 1 and 4 are the working core; steps 2 and 3 add the slash-command
and subagent surface on top. It finishes with a read-only dependency
probe; run the `wise-init` skill inside opencode to finish any missing
setup.

### Manual install

The same four steps by hand:

```bash
cp -R harnesses/opencode/wise/skills/* ~/.config/opencode/skills/
mkdir -p ~/.config/opencode/commands ~/.config/opencode/agents
cp harnesses/opencode/wise/commands/*.md ~/.config/opencode/commands/
for f in harnesses/opencode/wise/agents/*.md; do
  cp "$f" ~/.config/opencode/agents/"wise-$(basename "$f")"
done
mkdir -p ~/.local/share/wise/harness/opencode
cp -R harnesses/opencode/wise/references harnesses/opencode/wise/agents \
      harnesses/opencode/wise/workflows harnesses/opencode/wise/scripts \
      harnesses/opencode/wise/skills harnesses/opencode/wise/commands \
      ~/.local/share/wise/harness/opencode/
```

Skipping the shared-root copy (the last two commands) leaves only the
four self-contained skills (`wise-estimation`, `wise-feedback`,
`wise-prd-architect`, `wise-trd-architect`) working — everything else
reads shared files or the workflow engine from the shared root. Skipping
the `commands/` or `agents/` copies costs only the extras: skills stay
description-triggered without their slash wrappers, and workflow team
steps lose their `@wise-<role>` subagents.

## Prerequisites

- **git** and, for the PR skills, the authenticated **`gh` CLI**.
- For the **workflow engine** (`/wise-workflow-*`): **Python 3** with
  `pyyaml` and `python-ulid`
  (`pip install pyyaml python-ulid typing_extensions`). The `wise-init`
  skill probes and guides all of this.
- **`WISE_PLUGIN_ROOT` is optional.** Skills resolve shared files from
  `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/opencode`
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
  mapping Claude-specific tools to opencode equivalents, including
  `wise-init` (the guided dependency-setup wizard). Frontmatter is
  reduced to `name` + `description` (the widely-supported Agent Skills
  core); everything wise-specific lives in the body.
- **Claude-only (6, not shipped)** — `wise-supervise`, the three
  `wise-insights-*` skills, and `wise-skills-create` / `wise-skills-edit`.

opencode skills are **description-triggered** (its `skill` tool has no
slash form), so every user-invocable skill also ships a `/wise-<action>`
command wrapper in `commands/` that loads the matching skill and passes
your arguments along. The reference skills (`wise-estimation`,
`wise-prd-architect`, `wise-trd-architect`) are description-triggered
only and get no wrapper.

## Workflows

The workflow engine (`scripts/workflows.py`) runs unchanged and
self-locates its bundled workflows and agent roster — no environment
variables required after `./install.sh opencode`. opencode supports
concurrent subagents, so the conductor (`/wise-workflow-run`) dispatches
`prompt` steps as subagents and team steps as **parallel subagents** —
via the Task tool or `@wise-<role>` mentions against the role cards
installed in `~/.config/opencode/agents/`. See the conductor skill's
execution note.
