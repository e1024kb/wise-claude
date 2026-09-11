# AGENTS.md

Guidance for AI coding agents working in the **wise-claude** marketplace. This
is the repo's [AGENTS.md](https://agents.md) — a free-form project-instructions
file, not a loadable agent registry.

## Working in this repo

- This is a Claude Code plugin marketplace; the plugin lives in
  `plugins/wise/`. Contributor procedures, conventions, and the workflow-engine
  reference are in [`CONTRIBUTING.md`](./CONTRIBUTING.md) and
  [`docs/wise/`](./docs/wise/). Read those before changing the plugin.
- The plugin is hand-edited directly — there is no build step and nothing in
  the repo is generated.
- Validate before committing: `just check` (runs
  repository validation, pytest, Ruff and mypy in the pinned Python development
  environment); Python compilation, `python3 -m json.tool` on JSON manifests,
  and `bash -n` the shell scripts also catch syntax slips.

## The wise SDLC agent roster

`wise` ships 13 SDLC role agents under
[`plugins/wise/agents/`](./plugins/wise/agents/), one markdown file per role,
catalogued in [`plugins/wise/AGENTS.md`](./plugins/wise/AGENTS.md). They are
real Claude Code plugin subagents — auto-discovered when the plugin is
installed; invoke a role as `subagent_type: wise:<name>` (e.g.
`wise:architect`). Frontmatter: `name`, `description`, `tools`, `model: inherit`,
`effort`, `color`. Plugin subagents ignore `hooks` / `mcpServers` /
`permissionMode`.

V2 workflows run `agent` steps through headless provider CLIs. Claude children
can adopt or delegate to these role cards. Model and effort resolution belongs
to the canonical Python engine; see [the workflow reference](docs/wise/workflows.md).

## Adding or editing a role

Edit the card at `plugins/wise/agents/<name>.md` directly (frontmatter +
persona prose), then update the table in
[`plugins/wise/AGENTS.md`](./plugins/wise/AGENTS.md). Full
procedure: [`CONTRIBUTING.md` §9.10](./CONTRIBUTING.md#910-the-agent-roster).
