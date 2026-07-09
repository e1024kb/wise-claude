## Harness adaptation note

This skill was authored for Claude Code and adapted for Cursor. Where the steps below reference Claude-specific tools, substitute:

- **Task / subagent dispatch (`subagent_type: wise:<role>`)** — spawn a subagent with the role card at `${WISE_PLUGIN_ROOT}/agents/<role>.md` if this harness supports subagents; otherwise adopt that role yourself and perform the steps sequentially.
- **AskUserQuestion** — ask the user the same question in plain chat and wait for their reply.
- **Skill tool (`/wise-*`)** — open and follow the named skill's `SKILL.md` directly.
- **TodoWrite** — keep a visible checklist in your replies instead.
- **Shared files (`${WISE_PLUGIN_ROOT}`)** — defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/cursor`, where `./install.sh cursor` puts this pack; export `WISE_PLUGIN_ROOT` only to override.{{shared_files_extra}}

### Running workflows on Cursor

- **`${WISE_PLUGIN_ROOT}`** resolves shared files — it defaults to
  this pack's install directory (see the shared-files bullet above);
  export it only to override.
- **Prerequisites** replace `/wise-init`: Python 3 + `pyyaml` +
  `python-ulid`, `git`, and (for PR steps) an authenticated `gh`.
- **Step type → Cursor primitive:**
  - `bash` → run the shell command.
  - `skill` → open and follow `skills/<name>/SKILL.md`.
  - `prompt` (single `agent:`) → Cursor has no first-class subagent
    primitive, so **adopt the role** in the card at
    `${WISE_PLUGIN_ROOT}/agents/<role>.md` and do the step in-context.
  - `prompt` (team list) → work the members **sequentially**, one role
    at a time, then synthesize their outputs yourself as the lead.
  - `ask` / `approval` → ask the user in plain chat and wait.
  - `interactive` → take over the conversation for that step.
  - `supervised-prompt` → run as a plain `prompt` (the background
    watchdog is Claude-only).
- **`model` / `effort`** step fields are advisory here.
- Team steps run sequentially rather than in parallel, so multi-agent
  workflows are slower on Cursor than on Claude — same result, longer
  wall-clock.

When composing a step prompt from a workflow's `prompts/*.md`,
substitute `${WISE_PLUGIN_ROOT}` with its resolved value — the
executing context is fresh and never saw this note.
