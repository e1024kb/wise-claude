## Harness adaptation note

This skill was authored for Claude Code and adapted for opencode. Where the steps below reference Claude-specific tools, substitute:

- **Task / subagent dispatch (`subagent_type: wise:<role>`)** — spawn an **isolated subagent** via opencode's Task tool or an `@wise-<role>` mention (`./install.sh opencode` registers the role cards in `~/.config/opencode/agents/` under `wise-<role>` names; the pack's own copies stay at `${WISE_PLUGIN_ROOT}/agents/<role>.md`). Parallel dispatch is supported.
- **AskUserQuestion** — ask the user the same question in plain chat and wait for their reply.
- **Skill tool (`/wise-*`)** — open and follow the named skill's `SKILL.md` directly.
- **TodoWrite** — keep a visible checklist in your replies instead.
- **Shared files (`${WISE_PLUGIN_ROOT}`)** — defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/opencode`, where `./install.sh opencode` puts this pack; export `WISE_PLUGIN_ROOT` only to override.

When composing a subagent prompt from a workflow's `prompts/*.md`, substitute `${WISE_PLUGIN_ROOT}` with its resolved value — the subagent runs with fresh context and never saw this note.

### Running workflows on opencode

- **`${WISE_PLUGIN_ROOT}`** resolves per the shared-files bullet above
  (the `./install.sh opencode` default; export only to override) — step
  prompts read shared files via it.
- **Prerequisites** replace `/wise-init`: Python 3 + `pyyaml` +
  `python-ulid`, `git`, and (for PR steps) an authenticated `gh`.
- **Step type → opencode primitive:**
  - `bash` → run the shell command.
  - `skill` → open and follow `skills/<name>/SKILL.md` (or load it via
    opencode's native `skill` tool).
  - `prompt` (single `agent:`) → spawn an **isolated subagent** via the
    Task tool or an `@wise-<role>` mention, using the role card at
    `${WISE_PLUGIN_ROOT}/agents/<role>.md`.
  - `prompt` (team list) → spawn the members as **parallel subagents**
    (opencode runs isolated subagents concurrently), then synthesize
    their outputs as the lead.
  - `ask` / `approval` → ask the user in plain chat and wait.
  - `interactive` → take over the conversation for that step.
  - `supervised-prompt` → run as a plain `prompt` (the background
    watchdog is Claude-only).
- **`model` / `effort`** step fields are advisory here.
