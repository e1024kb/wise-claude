# AGENTS.md

Guidance for AI coding agents working in the **wise-claude** marketplace. This
is the repo's [AGENTS.md](https://agents.md) — a free-form project-instructions
file, not a loadable agent registry.

## Working in this repo

- This is a Claude Code plugin marketplace; the plugin lives in
  `harnesses/claude/wise/`. Contributor procedures, conventions, and the workflow-engine
  reference are in [`CONTRIBUTING.md`](./CONTRIBUTING.md) and
  [`docs/wise/`](./docs/wise/). Read those before changing the plugin.
- Validate before committing: `python3 -m py_compile
  harnesses/claude/wise/scripts/workflows.py`, `python3 -m json.tool` the JSON manifests,
  and `bash -n` the shell scripts. There is no build step.

## The wise SDLC agent roster

`wise` ships 13 SDLC role agents under
[`harnesses/claude/wise/agents/`](./harnesses/claude/wise/agents/), one markdown file per role,
catalogued in [`harnesses/claude/wise/AGENTS.md`](./harnesses/claude/wise/AGENTS.md). They are
real Claude Code plugin subagents — auto-discovered when the plugin is
installed; invoke a role as `subagent_type: wise:<name>` (e.g.
`wise:architect`). Frontmatter: `name`, `description`, `tools`, `model: inherit`,
`effort`, `color`. Plugin subagents ignore `hooks` / `mcpServers` /
`permissionMode`.

The workflow engine dispatches `prompt` steps to them (per-step `agent:` field +
workflow `agents:` policy — see
[`docs/wise/workflows.md`](./docs/wise/workflows.md#agents-model-and-effort)).

| Role | Default effort | Role | Default effort |
|---|---|---|---|
| `ceo` | high | `software-engineer` | medium |
| `cto` | high | `qa-engineer` | medium |
| `product-manager` | medium | `security-engineer` | high |
| `engineering-manager` | medium | `devops-engineer` | medium |
| `architect` | high | `sre` | high |
| `ux-designer` | medium | `technical-writer` | low |
| `code-reviewer` | high | | |

Steps run in-conversation (subscription-covered): a step's `model:` is a real
per-step override and `effort:` is a best-effort prompt nudge. A pinned model
that has retired auto-falls-back to its alias with a notice. See
[`docs/wise/workflows.md`](./docs/wise/workflows.md#agents-model-and-effort).

## Adding or editing a role

Edit the **canonical neutral card** at `core/agents/<name>.md` first (name +
description + persona prose, no harness-specific frontmatter), then propagate
to each port: the Claude card `harnesses/claude/wise/agents/<name>.md` adds
the `tools` / `model` / `effort` / `color` frontmatter Claude Code needs,
while the other ports vendor the neutral card verbatim (see
[`core/core-map.yaml`](./core/core-map.yaml) and
[`CONTRIBUTING.md` §10](./CONTRIBUTING.md#10-cross-harness-ports--core-sync)).
Update the table in
[`harnesses/claude/wise/AGENTS.md`](./harnesses/claude/wise/AGENTS.md). Full
procedure: [`CONTRIBUTING.md` §9.10](./CONTRIBUTING.md#910-the-agent-roster).
