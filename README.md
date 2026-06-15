# wise-claude

> A Claude Code copilot — flat `/wise-*` slash commands, a workflow engine, and autonomous git / PR / ticket-planning pipelines.

![version](https://img.shields.io/badge/version-1.0.0-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![Claude Code](https://img.shields.io/badge/Claude%20Code-plugin-8A63D2)

`wise-claude` is the marketplace home of the **`wise`** plugin: a set of
tech-neutral slash commands and multi-agent workflows that take everyday
engineering chores — drafting commits, opening and shepherding PRs, planning
tickets, authoring PRDs/TRDs, scaffolding new skills — off your plate. Reach
for a single quick command, or hand off a whole ticket → merged-PR pipeline
to run unattended.

## Install

In Claude Code:

```
/plugin marketplace add e1024kb/wise-claude
/plugin install wise@wise-claude
```

(Or run `/plugins`, choose **Add marketplace**, and enter `e1024kb/wise-claude`.)

Then run `/wise-init` once to probe dependencies, and `/wise` to print the
full command catalog.

## What you get

### Slash commands

- **Git & commits** — `/wise-commit-message` (draft only), `/wise-commit`
  (draft + commit), `/wise-commit-push` (draft + commit + push), all
  Conventional-Commits aware.
- **Pull requests** — `/wise-pr-create`, `/wise-pr-add-reviewers`,
  `/wise-pr-watch` (drive CI + review comments to green).
- **Planning & docs** — interactive ticket planning, `/wise-revise`
  (audit a scope and write executable improvement plans into
  `docs/plans/`), the model-invoked `wise-prd-architect` /
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
- **`implement-plan-auto`** — same autonomous pipeline, but fed a ready
  `PLAN-*.md` (e.g. one `/wise-revise` wrote): re-plan from the file →
  implement → review → PR → watch → merge.
- **`ticket-plan`** — autonomous planning you review and adjust before you
  implement.

To drive a single PR's CI + review queues to green interactively, use the
standalone `/wise-pr-watch` command.

See the [`wise` plugin README](plugins/wise/README.md) for the full command
reference, and [`docs/wise/`](docs/wise/) for the workflow engine, the `/wise`
dispatcher, and the skill-authoring guides.

## Requirements

- Claude Code
- Python 3, Node ≥ 22, and the GitHub `gh` CLI (authenticated) — `/wise-init`
  probes for these and walks you through anything missing.

## Repository layout

```
wise-claude/
├── .claude-plugin/marketplace.json   # marketplace index
├── plugins/wise/                     # the wise plugin (skills, workflows, scripts)
├── docs/wise/                        # workflow engine + authoring reference
└── CONTRIBUTING.md                   # full contributor manual
```

## Troubleshooting

- **Commands don't show up** — confirm `/plugin install wise@wise-claude` ran,
  then start a fresh Claude Code session.
- **PR / workflow steps fail on auth** — run `/wise-init`; make sure
  `gh auth status` is green and an `origin` remote exists.
- **`/wise` can't classify a request** — type the `/wise-` prefix to browse
  every command in the slash menu.

## Contributing

Issues and PRs are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the
conventions, local-install steps, and validation checks. You can also file
feedback without leaving Claude Code: `/wise-feedback`.

## License

[MIT](LICENSE) © e1024kb
