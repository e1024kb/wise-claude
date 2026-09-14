@AGENTS.md

## Claude Code integration

- Installed role cards in `plugins/wise/agents/` are Claude Code plugin
  subagents. Invoke them as `subagent_type: wise:<name>` when delegation is
  authorized. Their frontmatter supplies the persona, tools, inherited model
  and default effort. Plugin subagents do not support `hooks`, `mcpServers`
  or `permissionMode` fields.
- A Claude workflow child can adopt a role card or delegate through its
  `Task` / `Agent` tool when permitted by the step's `allowed_tools`.
  Do not assume a headless child inherits the main conversation's context
  or user-interaction tools.
- For user questions, use Claude's `AskUserQuestion` when available and
  permitted in the active client. Follow the shared host-control reference
  linked from `AGENTS.md` for routing and fallback behavior.
