# wise for Pi

The `wise` copilot ported to [Pi](https://github.com/earendil-works/pi)
as an Agent Skills pack. Flat `wise-*` skills, the workflow engine, and
the SDLC agent roster, adapted from the canonical Claude Code plugin
([github.com/e1024kb/wise-claude](https://github.com/e1024kb/wise-claude)).
Skills are user-invocable as `/skill:wise-<name>` and (except the
`wise` helper, which is explicit-only) also model-invoked from their
descriptions — no command wrappers needed.

This folder is **generated** by `scripts/build_ports.py` from the repo's
`core/`, the Claude port's skills, and the inputs under `core/ports/` —
don't edit it by hand. See the root `CONTRIBUTING.md` §10.

## Install

### Canonical: the universal installer

```bash
git clone https://github.com/e1024kb/wise-claude && cd wise-claude
./install.sh pi        # or: just install pi
```

The installer does two things, and a fully-wired install needs both:

1. copies the skills into `~/.pi/agent/skills/` (Pi's user-wide
   discovery dir);
2. lays the **whole intact pack** (`references/`, `agents/`,
   `workflows/`, `scripts/`, `skills/`) at
   `~/.local/share/wise/harness/pi` — the shared root every skill
   resolves by default when it reads shared files or invokes the
   workflow engine.

It finishes with a read-only dependency probe; run the `wise-init` skill
inside Pi to finish any missing setup.

Pi also reads `~/.agents/skills/` and project `.pi/skills/` /
`.agents/skills/` paths — install **one** copy only to avoid duplicate
skills (per-project: `./install.sh pi --project <dir>`).

### One command: `pi install`

```bash
pi install git:github.com/e1024kb/wise-claude
```

Pi discovers the skills from the repo's root `package.json` (its `"pi"`
manifest key globs `harnesses/pi/wise/skills/*`) — one command, nothing
to clone. A pi-installed package stays **intact** on disk, so when a
skill can't find the shared root it falls back to reading shared files
**pack-relative** — `../../` from the directory containing its own
`SKILL.md`, which in the intact pack is exactly the port root holding
`references/`,
`agents/`, `workflows/`, and `scripts/`. Most skills therefore work
as-installed.

For the fully-wired default-path setup — the shared root at
`~/.local/share/wise/harness/pi`, no fallback in play — either export
`WISE_PLUGIN_ROOT` to the installed pack's `harnesses/pi/wise`
directory, or run `./install.sh pi` from a clone, which lays the pack at
the default path.

### Manual install

The same two installer steps by hand:

```bash
mkdir -p ~/.pi/agent/skills
cp -R harnesses/pi/wise/skills/* ~/.pi/agent/skills/
mkdir -p ~/.local/share/wise/harness/pi
cp -R harnesses/pi/wise/references harnesses/pi/wise/agents \
      harnesses/pi/wise/workflows harnesses/pi/wise/scripts \
      harnesses/pi/wise/skills ~/.local/share/wise/harness/pi/
```

Skipping the second half leaves only the four self-contained skills
(`wise-estimation`, `wise-feedback`, `wise-prd-architect`,
`wise-trd-architect`) working — everything else reads shared files or
the workflow engine from the shared root, and skills copied out of the
pack have nothing to fall back to pack-relatively.

## Prerequisites

- **git** and, for the PR skills, the authenticated **`gh` CLI**.
- For the **workflow engine** (`/skill:wise-workflow-run` and friends):
  **Python 3** with `pyyaml` and `python-ulid`
  (`pip install pyyaml python-ulid typing_extensions`). The `wise-init`
  skill probes and guides all of this.
- **`WISE_PLUGIN_ROOT` is optional.** Skills resolve shared files from
  `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/pi`
  by default — exactly where the installer puts the pack — and a
  pi-installed pack additionally resolves pack-relative. Export
  `WISE_PLUGIN_ROOT` only to point them somewhere else. Persistent state
  (workflow runs, registries) defaults to `~/.local/share/wise`
  (override with `WISE_DATA_DIR`).

## What works here

The full matrix (with per-skill reasons) is in
[`docs/compatibility.md`](../../../docs/compatibility.md).

| Tier | Skills | What it means |
|---|---|---|
| **Full** | 11 | Pure prose + `git`/`gh`: the commit trio, the PR create/reviewer skills, `wise-estimation`, `wise-feedback`, and the PRD/TRD authors. Beyond the four self-contained ones, these read shared `references/` — the shared-root default (or the pack-relative fallback) covers that. |
| **Adapted** | 15 | The same logic with a **Harness adaptation note** mapping Claude-specific tools to Pi equivalents, including `wise-init` (the guided dependency-setup wizard). Frontmatter is reduced to `name` + `description` (plus `disable-model-invocation` on the `wise` helper — Pi honors it).  |
| **Claude-only** | 6 | Not shipped: `wise-supervise`, the three `wise-insights-*` skills, and `wise-skills-create` / `wise-skills-edit`. |

Every shipped skill is invocable explicitly as `/skill:wise-<name>`
(Pi's skill-command form), and all but one also implicitly — Pi loads a
skill on demand when your request matches its description. The
exception is the `wise` helper: it ships `disable-model-invocation:
true`, so only the explicit `/skill:wise` form reaches it.

## Workflows

The workflow engine (`scripts/workflows.py`) runs unchanged and
self-locates its bundled workflows and agent roster — no environment
variables required after `./install.sh pi`. Because Pi ships no subagent
primitive, the conductor (`/skill:wise-workflow-run`) runs `prompt`
steps by adopting the role in-context and team steps **sequentially** —
the result matches Claude, the wall-clock is longer. See the conductor
skill's execution note.
