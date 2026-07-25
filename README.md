# wise-claude

> A coding copilot for **Claude Code** — flat `/wise-*` skills, a
> multi-agent workflow engine, and autonomous git / PR / ticket-planning
> pipelines.

![version](https://img.shields.io/badge/version-4.0.0-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![Agent Skills](https://img.shields.io/badge/Agent%20Skills-standard-informational)

`wise-claude` is the Claude Code plugin marketplace that hosts the
**`wise`** copilot: flat `/wise-*` skills, multi-agent **workflows**, and
an SDLC **agent roster** (CEO / CTO / architect / engineer / QA /
security / SRE / …) that take everyday engineering chores off your
plate — drafting commits, opening and shepherding PRs, planning tickets,
authoring PRDs/TRDs, auditing a scope into an executable backlog. Reach
for a single quick command, or hand off a whole **ticket → merged-PR**
pipeline to run unattended.

## Install

```
/plugin marketplace add e1024kb/wise-claude
/plugin install wise@wise-claude
```

Then run the `wise-init` skill once to probe dependencies, and `/wise`
to print the full command catalog.

## What you get

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

See the [plugin README](plugins/wise/README.md) for the full command
reference and [`docs/wise/`](docs/wise/) for the workflow engine, the
`/wise` dispatcher, and the skill-authoring guides.

## Requirements

- **Claude Code**.
- **`git`**, and an authenticated **`gh` CLI** for the PR skills.
- **Python 3** (with `pyyaml` + `python-ulid`) for the workflow engine.
- The `wise-init` skill probes these and walks you through anything
  missing.

## Repository layout

```
wise-claude/
├── .claude-plugin/marketplace.json      # Claude Code marketplace index → plugins/wise
├── plugins/wise/                        # the wise plugin (skills, agents, workflows, engine)
├── scripts/validate_repo.py             # structural validation
├── docs/wise/                           # workflow engine + authoring reference
├── justfile                             # task runner (validate / test / check)
└── CONTRIBUTING.md                      # full contributor manual
```

## Migrating to v4.0.0

**v4.0.0 is Claude Code only.** The multi-harness ports introduced in
v3.0.0 (OpenAI Codex CLI, Cursor, Hermes Agent, opencode, Pi) were
dropped, along with `core/`, the port generator, and `install.sh`. The
last multi-harness release is **v3.8.1** — check out that tag if you
need one of the removed ports. The plugin path moved from
`harnesses/claude/wise` back to `plugins/wise`.

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

- **Skills / commands don't show up** — confirm `/plugin install
  wise@wise-claude` ran, then start a fresh session.
- **PR / workflow steps fail on auth** — run the `wise-init` skill;
  make sure `gh auth status` is green and an `origin` remote exists.
- **`/wise` can't classify a request** — type the `/wise-` prefix to browse
  every command in the menu.

## Contributing

Issues and PRs are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for
the conventions, local-install steps, and validation checks. You can
also file feedback from inside the agent with `/wise-feedback`.

## License

[MIT](LICENSE) © e1024kb
