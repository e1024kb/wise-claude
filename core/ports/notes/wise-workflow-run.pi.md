## Harness adaptation note

This skill was authored for Claude Code and adapted for Pi. Where the steps below reference Claude-specific tools, substitute:

- **Task / subagent dispatch (`subagent_type: wise:<role>`)** — Pi has no subagents: adopt the role card at `${WISE_PLUGIN_ROOT}/agents/<role>.md` yourself and perform the steps sequentially.
- **AskUserQuestion** — ask the user the same question in plain chat and wait for their reply.
- **Skill tool (`/wise-*`)** — invoke the skill as `/skill:wise-<name>`, or open and follow its `SKILL.md` directly.
- **TodoWrite** — keep a visible checklist in your replies instead.
- **Shared files (`${WISE_PLUGIN_ROOT}`)** — defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/pi`, where `./install.sh pi` puts this pack; export `WISE_PLUGIN_ROOT` only to override.{{shared_files_extra}}

### Running workflows on Pi

- **`${WISE_PLUGIN_ROOT}`** resolves shared files — it defaults to
  this pack's install directory (see the shared-files bullet above);
  export it only to override.
- **Prerequisites** replace `/wise-init`: Python 3 + `pyyaml` +
  `python-ulid`, `git`, and (for PR steps) an authenticated `gh`.
- **Step type → Pi primitive:**
  - `bash` → run the shell command.
  - `skill` → invoke `/skill:<name>`, or open and follow
    `skills/<name>/SKILL.md`.
  - `prompt` (single `agent:`) → Pi ships no subagents, so **adopt the
    role** in the card at `${WISE_PLUGIN_ROOT}/agents/<role>.md` and do
    the step in-context.
  - `prompt` (team list) → work the members **sequentially**, one role
    at a time, then synthesize their outputs yourself as the lead.
  - `ask` / `approval` → ask the user in plain chat and wait.
  - `interactive` → take over the conversation for that step.
  - `supervised-prompt` → run as a plain `prompt` (the background
    watchdog is Claude-only).
- **`model` / `effort`** step fields are advisory here.
- Team steps run sequentially rather than in parallel, so multi-agent
  workflows are slower on Pi than on Claude — same result, longer
  wall-clock.

When composing a step prompt from a workflow's `prompts/*.md`,
substitute `${WISE_PLUGIN_ROOT}` with its resolved value — the
executing context is fresh and never saw this note.
