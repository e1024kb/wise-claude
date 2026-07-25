# wise-claude

Claude Code plugin marketplace for the `wise` copilot — flat `/wise-*`
skills, a multi-agent workflow engine, an SDLC agent roster, and hooks
for everyday git / PR / ticket-planning tasks.

## Structure

Since **v4.0.0** the repo is Claude Code only. The plugin at
`plugins/wise/` is the canonical, hand-edited source of truth — there
is no generation step.

```
wise-claude/
├── .claude-plugin/
│   └── marketplace.json      # Claude Code marketplace index → plugins/wise
├── plugins/
│   └── wise/                 # the plugin (canonical, hand-edited)
│       ├── skills/           # SKILL.md skills / slash commands
│       ├── agents/           # SDLC role cards
│       ├── workflows/        # workflow definitions
│       ├── references/       # shared prose routines
│       ├── hooks/            # plugin hooks
│       ├── scripts/          # the workflow engine (workflows.py) + shared scripts
│       └── tests/            # engine test suite
├── scripts/
│   └── validate_repo.py      # structural validation
├── docs/wise/                # workflow engine + authoring reference
├── justfile                  # task runner (validate / test / check)
├── CLAUDE.md                 # This file
└── README.md                 # Repo docs
```

## Editing model

- **Source of truth**: the plugin at `plugins/wise/`. Edit it directly —
  nothing in the repo is generated.
- **Validate before committing**: `just check` — runs
  `python3 scripts/validate_repo.py` and
  `python3 -m pytest plugins/wise/tests -q`.

## Conventions

- One version source: `plugins/wise/.claude-plugin/plugin.json`
- Include a LICENSE file for open-source plugins
- Pin external sources to a commit SHA in marketplace.json
