## Harness adaptation note

This skill was authored for Claude Code and adapted for OpenAI Codex CLI. Where the steps below reference Claude-specific tools, substitute:

- **Task / subagent dispatch (`subagent_type: wise:<role>`)** — spawn a subagent with the role card at `${WISE_PLUGIN_ROOT}/agents/<role>.md` if this harness supports subagents; otherwise adopt that role yourself and perform the steps sequentially.
- **AskUserQuestion** — ask the user the same question in plain chat and wait for their reply.
- **Skill tool (`/wise-*`)** — open and follow the named skill's `SKILL.md` directly.
- **TodoWrite** — keep a visible checklist in your replies instead.
- **Shared files (`${WISE_PLUGIN_ROOT}`)** — defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/codex`, where `./install.sh codex` puts this pack; export `WISE_PLUGIN_ROOT` only to override.

### Running workflows on Codex

Same execution model as `/wise-workflow-run`: resolve
`${WISE_PLUGIN_ROOT}` per the shared-files bullet above, ensure the
workflow prerequisites (Python 3 + `pyyaml` + `python-ulid`, `git`,
`gh`), and map each step type to its Codex primitive (subagents where
available, else in-context role adoption; plain-chat questions for
`ask`/`approval`). Resume works off Codex because the engine tags runs
with a synthetic per-workspace session id — no Claude transcript is
required. When composing a subagent or step prompt from a workflow's
`prompts/*.md`, substitute `${WISE_PLUGIN_ROOT}` with its resolved
value — the executing context is fresh and never saw this note.
