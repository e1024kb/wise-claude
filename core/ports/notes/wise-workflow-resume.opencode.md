## Harness adaptation note

This skill was authored for Claude Code and adapted for opencode. Where the steps below reference Claude-specific tools, substitute:

- **Task / subagent dispatch (`subagent_type: wise:<role>`)** — spawn an **isolated subagent** via opencode's Task tool or an `@wise-<role>` mention (`./install.sh opencode` registers the role cards in `~/.config/opencode/agents/` under `wise-<role>` names; the pack's own copies stay at `${WISE_PLUGIN_ROOT}/agents/<role>.md`). Parallel dispatch is supported.
- **AskUserQuestion** — ask the user the same question in plain chat and wait for their reply.
- **Skill tool (`/wise-*`)** — open and follow the named skill's `SKILL.md` directly.
- **TodoWrite** — keep a visible checklist in your replies instead.
- **Shared files (`${WISE_PLUGIN_ROOT}`)** — defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/opencode`, where `./install.sh opencode` puts this pack; export `WISE_PLUGIN_ROOT` only to override.

When composing a subagent prompt from a workflow's `prompts/*.md`, substitute `${WISE_PLUGIN_ROOT}` with its resolved value — the subagent runs with fresh context and never saw this note.

### Running workflows on opencode

Same execution model as `/wise-workflow-run`: resolve
`${WISE_PLUGIN_ROOT}` per the shared-files bullet above, ensure the
prerequisites (Python 3 + `pyyaml` + `python-ulid`, `git`, `gh`), and
map each step type to its opencode primitive — `prompt` steps spawn
isolated subagents via the Task tool or an `@wise-<role>` mention
(teams run as parallel subagents), `ask`/`approval` become plain-chat
questions.
Resume works because the engine tags runs with a synthetic per-workspace
session id — no Claude transcript required.
