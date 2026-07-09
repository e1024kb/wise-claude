# wise-claude

Cross-harness marketplace for the `wise` copilot — plugins, skills,
agents, hooks, and workflows for **Claude Code, OpenAI Codex CLI,
Cursor, Nous Research Hermes Agent, opencode, and Pi**.

## Structure

Since **v3.0.0** the repo is organized by harness. `core/` is the
canonical, harness-neutral source; each `harnesses/<harness>/wise/`
folder is an independently installable port. Port content is
**generated** from `core/`, the Claude port's skills, and the inputs
under `core/ports/` by `scripts/build_ports.py` — the generated output
stays committed, and CI regenerates and diffs to enforce it.

```
wise-claude/
├── .claude-plugin/
│   └── marketplace.json      # Claude Code marketplace index → harnesses/claude/wise
├── .agents/plugins/
│   └── marketplace.json      # Codex marketplace index → harnesses/codex/wise (added by the Codex port)
├── core/                     # canonical harness-neutral source (not installable itself)
│   ├── references/           # shared prose routines
│   ├── agents/               # SDLC role cards (neutral form)
│   ├── workflows/            # workflow definitions
│   ├── scripts/              # the workflow engine (workflows.py) + catalog emitter
│   └── ports/                # generator inputs (hand-maintained)
│       ├── profiles/         # one <harness>.yaml per port: tier lists, frontmatter keep-list, …
│       ├── notes/            # harness-adaptation note templates (+ per-harness overrides)
│       ├── overlays/         # per-skill find/replace hunks for genuinely divergent prose
│       └── static/           # byte-copied per-port files (README, manifests, …)
├── harnesses/
│   └── <harness>/wise/       # one port per harness (claude, codex, cursor, hermes, opencode, pi)
│       ├── skills/           # SKILL.md skills / slash commands (claude = source, others generated)
│       ├── agents/           # role cards (generated)
│       ├── workflows/        # workflow defs (generated)
│       ├── scripts/          # engine (generated)
│       └── README.md         # canonical install instructions for that harness
├── scripts/
│   ├── validate_repo.py      # structural validation
│   └── build_ports.py        # the port generator (regenerate in place, or --check to diff)
├── install.sh · justfile     # universal installer + task runner
├── CLAUDE.md                 # This file
└── README.md                 # Repo docs
```

The Claude Code plugin lives at `harnesses/claude/wise/` (moved from the
old `plugins/wise/` in v3.0.0 — see the README migration note).

## Editing model

- **Sources of truth**: `core/` (references, workflows, engine, neutral
  agent cards), `harnesses/claude/wise/skills/` (the canonical skills),
  and `core/ports/` (profiles, note templates, overlays, statics).
  Edit these — never a generated file.
- **Everything else under `harnesses/` is generated and committed.**
  After editing a source, run `python3 scripts/build_ports.py` (or
  `just build`) to regenerate the ports in place.
- **CI enforces sync**: `python3 scripts/build_ports.py --check` (or
  `just build-check`) renders to a temp dir and fails on any diff
  between the committed tree and a fresh render.

## Adding a New Harness Port

1. Write `core/ports/profiles/<name>.yaml` (tier lists, frontmatter
   keep-list, harness id/name)
2. Add any needed overlays under `core/ports/overlays/<name>/` and
   statics under `core/ports/static/<name>/` (README, manifests)
3. Add an `install.sh` arm for the harness
4. Run `python3 scripts/build_ports.py` to generate the port

## Conventions

- One version source: `harnesses/claude/wise/.claude-plugin/plugin.json`;
  every port + marketplace manifest carries the same version.
- Include a LICENSE file for open-source plugins
- Pin external sources to a commit SHA in marketplace.json
