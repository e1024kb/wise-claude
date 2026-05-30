# wise-claude

Community marketplace for Claude Code plugins, skills, agents, and hooks.

## Structure

```
wise-claude/
├── .claude-plugin/
│   └── marketplace.json      # Marketplace index — lists all available plugins
├── plugins/
│   └── <plugin-name>/        # Each plugin is a self-contained directory
│       ├── .claude-plugin/
│       │   └── plugin.json   # Plugin metadata (required)
│       ├── skills/           # Skills and slash commands
│       ├── agents/           # Custom agents
│       ├── hooks/            # Event-driven hooks
│       ├── .mcp.json         # MCP server config
│       └── README.md         # Plugin docs
├── CLAUDE.md                 # This file
└── README.md                 # Repo docs
```

## Adding a New Plugin

1. Create `plugins/<name>/` with at least `.claude-plugin/plugin.json`
2. Add skills, agents, hooks, or MCP configs as needed
3. Register the plugin in `.claude-plugin/marketplace.json`
4. Add a `README.md` to the plugin directory

## Conventions

- Plugin names: lowercase, kebab-case
- One clear purpose per plugin
- Include a LICENSE file for open-source plugins
- Pin external sources to a commit SHA in marketplace.json
