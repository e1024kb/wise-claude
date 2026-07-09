## Harness adaptation note

This skill was authored for Claude Code and adapted for OpenAI Codex CLI. Where the steps below reference Claude-specific tools, substitute:

- **Task / subagent dispatch (`subagent_type: wise:<role>`)** — spawn a subagent with the role card at `${WISE_PLUGIN_ROOT}/agents/<role>.md` if this harness supports subagents; otherwise adopt that role yourself and perform the steps sequentially.
- **AskUserQuestion** — ask the user the same question in plain chat and wait for their reply.
- **Skill tool (`/wise-*`)** — open and follow the named skill's `SKILL.md` directly.
- **TodoWrite** — keep a visible checklist in your replies instead.
- **Shared files (`${WISE_PLUGIN_ROOT}`)** — defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/codex`, where `./install.sh codex` puts this pack; export `WISE_PLUGIN_ROOT` only to override.{{shared_files_extra}}

### Running workflows on Codex

- **`${WISE_PLUGIN_ROOT}`** resolves per the shared-files bullet above
  (the install default; export only to override — see the pack README).
  The engine (`scripts/workflows.py`) locates itself, but step prompts
  read shared files via that variable.
- **Prerequisites** replace `/wise-init`: Python 3 + `pyyaml` +
  `python-ulid`, `git`, and (for PR steps) an authenticated `gh`. There
  is no `bootstrap-deps.sh` protocol here — install these once.
- **Step type → Codex primitive:**
  - `bash` → run the shell command.
  - `skill` → open and follow `skills/<name>/SKILL.md`.
  - `prompt` (single `agent:`) → dispatch a subagent with the role card
    at `${WISE_PLUGIN_ROOT}/agents/<role>.md` if subagents are available,
    else adopt the role and do the step in-context.
  - `prompt` (a team list) → run the members as parallel subagents where
    supported, otherwise sequentially, then synthesize their outputs
    yourself as the lead.
  - `ask` / `approval` → ask the user in plain chat and wait.
  - `interactive` → take over the conversation for that step (already
    harness-neutral prose).
  - `supervised-prompt` → run as a plain `prompt`; the background-watchdog
    supervision is Claude-only (no `wise-supervise` here).
- **`model` / `effort`** step fields are advisory on Codex — use your
  harness's model selection; ignore where unsupported.

When composing a subagent or step prompt from a workflow's
`prompts/*.md`, substitute `${WISE_PLUGIN_ROOT}` with its resolved
value — the executing context is fresh and never saw this note.
