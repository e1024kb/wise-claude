# wise-claude

Cross-harness marketplace for the `wise` copilot — plugins, skills,
agents, hooks, and workflows for **Claude Code, OpenAI Codex CLI,
Cursor, and Nous Research Hermes Agent**.

## Structure

Since **v3.0.0** the repo is organized by harness. `core/` is the
canonical, harness-neutral source; each `harnesses/<harness>/wise/`
folder is a hand-maintained, independently installable port that
vendors from `core/`.

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
│   └── core-map.yaml         # maps each core asset → its vendored copy per harness
├── harnesses/
│   └── <harness>/wise/       # one hand-maintained port per harness (claude, codex, cursor, hermes)
│       ├── skills/           # SKILL.md skills / slash commands
│       ├── agents/           # role cards (harness-specific frontmatter)
│       ├── workflows/        # vendored workflow defs
│       ├── scripts/          # vendored engine
│       └── README.md         # canonical install instructions for that harness
├── scripts/
│   ├── validate_repo.py      # structural validation
│   └── report_core_drift.py  # non-blocking core↔port divergence report (added by the installer PR)
├── install.sh · justfile     # universal installer (added by the installer PR)
├── CLAUDE.md                 # This file
└── README.md                 # Repo docs
```

The Claude Code plugin lives at `harnesses/claude/wise/` (moved from the
old `plugins/wise/` in v3.0.0 — see the README migration note).

## Editing model

- **`core/` is the single source of truth** for harness-neutral assets
  (references, workflows, engine, neutral agent cards). Edit there first.
- **Propagate** the change into each affected `harnesses/<harness>/wise/`
  port by hand, then run `just drift` (or
  `python3 scripts/report_core_drift.py`) to see what diverged. The
  drift report is advisory — some port files legitimately diverge
  (adapted frontmatter, conductor prose).
- The full sync checklist lives in `CONTRIBUTING.md`.

## Adding a New Harness Port

1. Create `harnesses/<name>/wise/` and vendor the needed `core/` assets
2. Adapt skills to the harness's SKILL.md conventions (frontmatter, path rewrites)
3. Register its vendored paths in `core/core-map.yaml`
4. Add the harness's marketplace/catalog manifest if it has one
5. Add a `README.md` with canonical install instructions

## Conventions

- One version source: `harnesses/claude/wise/.claude-plugin/plugin.json`;
  every port + marketplace manifest carries the same version.
- Include a LICENSE file for open-source plugins
- Pin external sources to a commit SHA in marketplace.json
