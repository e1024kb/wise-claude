## Harness adaptation note

This skill was authored for Claude Code and adapted for Nous Research Hermes Agent. Where the steps below reference Claude-specific tools, substitute:

- **Task / subagent dispatch (`subagent_type: wise:<role>`)** — spawn a subagent with the role card at `${WISE_PLUGIN_ROOT}/agents/<role>.md` if this harness supports subagents; otherwise adopt that role yourself and perform the steps sequentially.
- **AskUserQuestion** — ask the user the same question in plain chat and wait for their reply.
- **Skill tool (`/wise-*`)** — open and follow the named skill's `SKILL.md` directly.
- **TodoWrite** — keep a visible checklist in your replies instead.
- **Shared files (`${WISE_PLUGIN_ROOT}`)** — defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/hermes`, where `./install.sh hermes` puts this pack; export `WISE_PLUGIN_ROOT` only to override.{{shared_files_extra}}

When composing a subagent prompt from a workflow's `prompts/*.md`, substitute `${WISE_PLUGIN_ROOT}` with its resolved value — the subagent runs with fresh context and never saw this note.

### Running workflows on Hermes

Same execution model as `/wise-workflow-run`: resolve
`${WISE_PLUGIN_ROOT}` per the shared-files bullet above, ensure the
prerequisites (Python 3 + `pyyaml` + `python-ulid`, `git`, `gh`), and
map each step type to its Hermes
primitive — `prompt` steps spawn isolated subagents (teams run as
parallel subagents), `ask`/`approval` become plain-chat questions.
Resume works because the engine tags runs with a synthetic per-workspace
session id — no Claude transcript required.
