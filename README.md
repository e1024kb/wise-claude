# wise-claude

> A coding copilot for **Claude Code, OpenAI Codex CLI, Cursor, Nous Research Hermes Agent, and opencode** — flat `/wise-*` skills, a multi-agent workflow engine, and autonomous git / PR / ticket-planning pipelines.

![version](https://img.shields.io/badge/version-3.6.0-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![harnesses](https://img.shields.io/badge/harnesses-Claude%20Code%20·%20Codex%20·%20Cursor%20·%20Hermes%20·%20opencode-8A63D2)
![Agent Skills](https://img.shields.io/badge/Agent%20Skills-standard-informational)

`wise-claude` is the home of the **`wise`** copilot: flat `/wise-*` skills,
multi-agent **workflows**, and an SDLC **agent roster** (CEO / CTO /
architect / engineer / QA / security / SRE / …) that take everyday
engineering chores off your plate — drafting commits, opening and
shepherding PRs, planning tickets, authoring PRDs/TRDs, auditing a scope
into an executable backlog. Reach for a single quick command, or hand off
a whole **ticket → merged-PR** pipeline to run unattended.

It's maintained as a harness-neutral **`core/`** plus a generated
**port per harness** under `harnesses/<harness>/wise/`, so the same copilot
installs natively on whichever agent you use. **26 of the 32 skills and all
four workflows** port to every harness; see the
[compatibility matrix](docs/compatibility.md).

## Install

From a clone, the **universal installer** covers every harness:

```
./install.sh <claude|codex|cursor|hermes|opencode>  # user-wide
./install.sh cursor --project ./my-repo             # into one project
```

…or via [`just`](https://just.systems):

```
just install codex
just install cursor project=./my-repo
```

Each harness also has a **canonical install** (and each port's
`harnesses/<harness>/wise/README.md` has the exact steps):

| Harness | Canonical install |
|---|---|
| **Claude Code** | `/plugin marketplace add e1024kb/wise-claude` then `/plugin install wise@wise-claude` |
| **OpenAI Codex CLI** | `./install.sh codex` — uses `codex plugin marketplace add` + `codex plugin install wise` when the CLI is present (catalog: `.agents/plugins/marketplace.json`) |
| **Cursor** | `./install.sh cursor` (skills → `~/.cursor/skills/`; per-project: `--project <dir>`) |
| **Hermes Agent** | `./install.sh hermes` (skills → `~/.hermes/skills/`) |
| **opencode** | `./install.sh opencode` (skills → `~/.config/opencode/skills/`, opencode's discovery dir; plus `/wise-<action>` command wrappers → `~/.config/opencode/commands/` and `wise-<role>` subagent cards → `~/.config/opencode/agents/`) |

On the non-Claude ports the installer lays the whole pack at a **stable
shared root** (`~/.local/share/wise/harness/<harness>`) that skills and
workflows resolve **by default — no env vars needed**; export
`WISE_PLUGIN_ROOT` only to override it. Copying the skills alone is not a
working install — the port READMEs show the exact two-step manual
alternative. On every harness, run the `wise-init` skill once to probe
dependencies, and `/wise` to print the full command catalog.

## What you get

Availability varies per harness — see the
[compatibility matrix](docs/compatibility.md).

### Skills

- **Git & commits** — `/wise-commit-message` (draft only), `/wise-commit`
  (draft + commit), `/wise-commit-push` (draft + commit + push), all
  Conventional-Commits aware.
- **Pull requests** — `/wise-pr-create`, `/wise-pr-add-reviewers`,
  `/wise-pr-watch` (drive CI + review comments to green).
- **Planning & docs** — interactive ticket planning, `/wise-grill` (deep-
  research a ticket / doc / prompt into a plan, blueprint, or answer),
  `/wise-revise` (audit a scope and write executable improvement plans
  into `docs/plans/`), the model-invoked `wise-prd-architect` /
  `wise-trd-architect` document authors, and the `wise-estimation`
  story-point reference.
- **Authoring** — `/wise-skills-create`, `/wise-skills-edit`, and the
  `/wise-workflow-*` family for building and running your own workflows.
- **The `/wise` helper** — type `/wise <free-form text>` (e.g. `/wise open a
  PR`) and it classifies the request and offers the matching command.
- **Autonomous `-auto` building blocks** — decision-free, prompt-free
  variants (`/wise-pr-create-auto`, `/wise-implement-plan-auto`,
  `/wise-code-review-auto`, …) used by the unattended pipelines.

### Workflows (multi-step, multi-agent)

- **`ticket-auto`** — autonomous ticket → plan → implement → review → PR →
  watch CI → resolve review bots → merge, with no prompts.
- **`impl-plan-auto`** — same autonomous pipeline, but fed a ready
  `PLAN-*.md` (e.g. one `/wise-revise` wrote): re-plan from the file →
  implement → review → PR → watch → merge.
- **`ticket-plan`** — autonomous planning you review and adjust before you
  implement.

The workflow **engine** runs on every harness; the **conductor** maps each
step to that harness's primitives (parallel subagents on Claude / Hermes /
opencode, sequential on Cursor, subagents-where-available on Codex — see each port's
`/wise-workflow-run` execution note).

See the [Claude port's plugin README](harnesses/claude/wise/README.md) for
the full command reference and [`docs/wise/`](docs/wise/) for the workflow
engine, the `/wise` dispatcher, and the skill-authoring guides.

## Requirements

- **A supported harness** — Claude Code, OpenAI Codex CLI, Cursor,
  Nous Research Hermes Agent, or opencode.
- **`git`**, and an authenticated **`gh` CLI** for the PR skills.
- **Python 3** (with `pyyaml` + `python-ulid`) for the workflow engine.
- The `wise-init` skill (all harnesses) probes these and walks you through
  anything missing; each port README also lists its prerequisites.

## Repository layout

The repo is organized by harness (a v3.0.0 layout change — see the
migration note). `core/` is the canonical harness-neutral source; each
`harnesses/<harness>/wise/` folder is an independently installable port
generated from it (and from the Claude port's skills) by
`scripts/build_ports.py`, with the generated output committed.

```
wise-claude/
├── .claude-plugin/marketplace.json      # Claude Code marketplace index → harnesses/claude/wise
├── .agents/plugins/marketplace.json     # Codex marketplace catalog → harnesses/codex/wise
├── core/                                # canonical harness-neutral source (references, agents, workflows, engine, port inputs)
├── harnesses/
│   ├── claude/wise/                     # Claude Code plugin
│   ├── codex/wise/                      # OpenAI Codex CLI port
│   ├── cursor/wise/                     # Cursor port
│   ├── hermes/wise/                     # Hermes Agent port
│   └── opencode/wise/                   # opencode port
├── install.sh · justfile                # universal installer
├── docs/wise/                           # workflow engine + authoring reference
├── docs/compatibility.md                # skill × harness matrix
└── CONTRIBUTING.md                      # full contributor manual (§10 = ports & the generator)
```

## Migrating from v2.x → v3.0.0

v3.0.0 moves the Claude Code plugin from `plugins/wise/` to
`harnesses/claude/wise/` (a backward-incompatible **layout** change; the
plugin's commands and behaviour are unchanged). The marketplace still
lives at the repo root, and its plugin `source` now points at the new
path.

- **Fresh installs** — nothing to do; `/plugin install wise@wise-claude`
  works as before.
- **Existing installs** — refresh the marketplace so it re-reads the new
  source path:
  ```
  /plugin marketplace update wise-claude
  /plugin install wise@wise-claude
  ```
  If commands still don't resolve, remove and re-add the marketplace
  (`/plugin marketplace remove wise-claude`, then the two Install
  commands above) and start a fresh session.

## Troubleshooting

- **Skills / commands don't show up** — confirm the install step for your
  harness ran (Claude: `/plugin install`; Codex: `codex plugin install`;
  Cursor / Hermes / opencode: the skills copied into `~/.cursor/skills` /
  `~/.hermes/skills` / `~/.config/opencode/skills`), then start a fresh
  session.
- **A skill or workflow can't find its shared files** (non-Claude ports) —
  the shared root is missing: run `./install.sh <harness>` (it lays the
  whole pack at `~/.local/share/wise/harness/<harness>`, the path skills
  resolve by default), or export `WISE_PLUGIN_ROOT` to wherever you put
  the pack.
- **PR / workflow steps fail on auth** — run the `wise-init` skill;
  everywhere, make sure `gh auth status` is green and an `origin` remote
  exists.
- **`/wise` can't classify a request** — type the `/wise-` prefix to browse
  every command in the menu.

## Contributing

Issues and PRs are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the
conventions, local-install steps, validation checks, and the **§10 port
generator model** (edit `core/`, the Claude skills, or `core/ports/`,
then regenerate with `python3 scripts/build_ports.py` — the other ports
are generated, never hand-edited). You can also file feedback from
inside the agent with `/wise-feedback`.

## License

[MIT](LICENSE) © e1024kb
