## Harness adaptation note

This skill was authored for Claude Code and adapted for Nous Research Hermes Agent. Where the steps below reference Claude-specific tools, substitute:

- **Task / subagent dispatch (`subagent_type: wise:<role>`)** — spawn a subagent with the role card at `${WISE_PLUGIN_ROOT}/agents/<role>.md` if this harness supports subagents; otherwise adopt that role yourself and perform the steps sequentially.
- **AskUserQuestion** — ask the user the same question in plain chat and wait for their reply.
- **Skill tool (`/wise-*`)** — open and follow the named skill's `SKILL.md` directly.
- **TodoWrite** — keep a visible checklist in your replies instead.
- **Shared files (`${WISE_PLUGIN_ROOT}`)** — defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/hermes`, where `./install.sh hermes` puts this pack; export `WISE_PLUGIN_ROOT` only to override.

When composing a subagent prompt from a workflow's `prompts/*.md`, substitute `${WISE_PLUGIN_ROOT}` with its resolved value — the subagent runs with fresh context and never saw this note.

### Running workflows on Hermes

- **`${WISE_PLUGIN_ROOT}`** resolves per the shared-files bullet above
  (the `./install.sh hermes` default; export only to override) — step
  prompts read shared files via it.
- **Prerequisites** replace `/wise-init`: Python 3 + `pyyaml` +
  `python-ulid`, `git`, and (for PR steps) an authenticated `gh`.
- **Step type → Hermes primitive:**
  - `bash` → run the shell command.
  - `skill` → open and follow `skills/<name>/SKILL.md` (or `/<name>`).
  - `prompt` (single `agent:`) → spawn an **isolated subagent** with the
    role card at `${WISE_PLUGIN_ROOT}/agents/<role>.md`.
  - `prompt` (team list) → spawn the members as **parallel subagents**
    (Hermes runs isolated subagents concurrently), then synthesize their
    outputs as the lead.
  - `ask` / `approval` → ask the user in plain chat and wait.
  - `interactive` → take over the conversation for that step.
  - `supervised-prompt` → run as a plain `prompt` (the background
    watchdog is Claude-only).
- **`model` / `effort`** step fields are advisory here.
