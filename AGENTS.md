# AGENTS.md

Shared contributor guidance for every coding harness working in `wise-claude`.
This file contains project instructions, not a loadable agent registry.
Keep harness-specific instructions in that harness's configuration file.

## Repository and runtime

- `plugins/wise/` is the canonical, hand-edited Wise plugin. The marketplace
  index at `.claude-plugin/marketplace.json` points to it. There is no build
  or generation step.
- The workflow engine is Python 3.11+ and executes YAML v2 workflows. Its
  provider adapters support Claude, Codex, Cursor, Gemini and Grok. The main
  conversation's client and the provider running a child are separate choices.
- Read [CONTRIBUTING.md](CONTRIBUTING.md) and the relevant
  [workflow documentation](docs/wise/) before changing the plugin. Consult
  the instructions within the affected subtree as well.

## Code map

- `plugins/wise/skills/`: skill entrypoints and skill-local resources.
- `plugins/wise/references/`: shared procedures. Change shared behavior here
  rather than duplicating instructions across skills.
- `plugins/wise/agents/`: SDLC role cards, catalogued in
  [the plugin agent index](plugins/wise/AGENTS.md).
- `plugins/wise/workflows/`: bundled workflow definitions, prompts and READMEs.
- `plugins/wise/engine/wise_engine/`: schema (`defs.py`), scheduling,
  execution, persisted state, preflight and provider adapters.
- `plugins/wise/engine/tests/` and `plugins/wise/tests/`: automated tests.
- `plugins/wise/scripts/` and `plugins/wise/hooks/`: helpers and plugin hooks.
- `scripts/validate_repo.py`: repository structure and documentation checks.
- `docs/wise/`: user and contributor reference documentation.

## Workflow and interaction contracts

- The engine owns workflow scheduling, model/effort resolution and persisted
  state. The main harness calls its MCP tools or CLI, not workflow steps itself.
- Follow [workflow host control](plugins/wise/references/workflow-host-control.md)
  for installation discovery, initialization, question routing and gates.
  Identify the active main client, its GUI/TUI surface and available question
  tools rather than inferring capabilities from the provider name.
- The main harness owns user interaction: prefer its permitted native control,
  then a usable rendered MCP form, then the documented main-harness text
  fallback. Never launch a separate terminal or system dialog as a fallback.
  Defaults and asynchronous display acknowledgements are not user answers.
- Children relay questions through Wise or their parent. Pass applicable
  project instructions and task constraints to delegated children; do not
  assume they inherit the main conversation. Preserve autonomous no-prompt
  rules and explicit consent gates defined by the invoked skill.

## Editing and validation

- Keep `plugins/wise/.claude-plugin/plugin.json` as the plugin version source.
  Follow CONTRIBUTING's versioning rules when changing shipped plugin files.
- MUST: every plugin version bump updates the version badge in the root
  `README.md` (`badge/version-<x.y.z>-blue`) to the same version in the same
  change. `scripts/validate_repo.py` fails when they differ.
- Keep workflow READMEs synchronized with their YAML and prompts. When editing
  `plugins/wise/agents/<name>.md`, update the plugin agent index in the same
  change. Role cards, not a duplicated roster, are the source of truth.
- Pin external marketplace sources to commit SHAs. Retain the plugin license.
- Run `just install` when the pinned development environment is missing.
  Run `just check` before committing: repository validation, pytest, mypy,
  Ruff, formatting checks, Python compilation, JSON and shell syntax checks.
