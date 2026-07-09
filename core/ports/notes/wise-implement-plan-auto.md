## Harness adaptation note

This skill was authored for Claude Code and adapted for {{harness_name}}. Where the steps below reference Claude-specific tools, substitute:

- **Task / subagent dispatch (`subagent_type: wise:<role>`)** — spawn a subagent with the role card at `${WISE_PLUGIN_ROOT}/agents/<role>.md` if this harness supports subagents; otherwise adopt that role yourself and perform the steps sequentially.
- **TodoWrite** — keep a visible checklist in your replies instead.
- **Shared files (`${WISE_PLUGIN_ROOT}`)** — defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/{{harness_id}}`, where `./install.sh {{harness_id}}` puts this pack; export `WISE_PLUGIN_ROOT` only to override.{{shared_files_extra}}
