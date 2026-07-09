## Harness adaptation note

This skill was authored for Claude Code and adapted for {{harness_name}}. Where the steps below reference Claude-specific tools, substitute:

- **AskUserQuestion** — ask the user the same question in plain chat and wait for their reply.
- **Shared files (`${WISE_PLUGIN_ROOT}`)** — defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/{{harness_id}}`, where `./install.sh {{harness_id}}` puts this pack; export `WISE_PLUGIN_ROOT` only to override.
