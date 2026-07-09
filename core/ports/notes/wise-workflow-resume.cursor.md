## Harness adaptation note

This skill was authored for Claude Code and adapted for Cursor. Where the steps below reference Claude-specific tools, substitute:

- **Task / subagent dispatch (`subagent_type: wise:<role>`)** — spawn a subagent with the role card at `${WISE_PLUGIN_ROOT}/agents/<role>.md` if this harness supports subagents; otherwise adopt that role yourself and perform the steps sequentially.
- **AskUserQuestion** — ask the user the same question in plain chat and wait for their reply.
- **Skill tool (`/wise-*`)** — open and follow the named skill's `SKILL.md` directly.
- **TodoWrite** — keep a visible checklist in your replies instead.
- **Shared files (`${WISE_PLUGIN_ROOT}`)** — defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/cursor`, where `./install.sh cursor` puts this pack; export `WISE_PLUGIN_ROOT` only to override.{{shared_files_extra}}

### Running workflows on Cursor

Same execution model as `/wise-workflow-run`: resolve
`${WISE_PLUGIN_ROOT}` (it defaults to this pack's install directory —
see the shared-files bullet above), ensure the prerequisites (Python 3 +
`pyyaml` + `python-ulid`, `git`, `gh`), and map each step type to its
Cursor primitive — Cursor has no subagent primitive, so `prompt` steps
are done by adopting the role in-context and team steps run
sequentially. `ask`/`approval` become plain-chat questions. Resume works
because the engine tags runs with a synthetic per-workspace session id —
no Claude transcript required.

When composing a step prompt from a workflow's `prompts/*.md`,
substitute `${WISE_PLUGIN_ROOT}` with its resolved value — the
executing context is fresh and never saw this note.
